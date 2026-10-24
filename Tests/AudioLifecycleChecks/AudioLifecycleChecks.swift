import Foundation

// Compile this file with the production SerialAudioControl.swift and
// RecordingSession.swift, excluding the real audio recorders. These fakes use
// blocking gates and never open a device, request permission, or capture media.
private final class Probe: @unchecked Sendable {
    let startGate = DispatchSemaphore(value: 0)
    let stopGate = DispatchSemaphore(value: 0)
    private let lock = NSLock()
    private var recordedEvents: [String] = []

    func record(_ event: String) { lock.withLock { recordedEvents.append(event) } }
    var events: [String] { lock.withLock { recordedEvents } }

    func waitForStartRelease() {
        precondition(startGate.wait(timeout: .now() + 10) == .success, "Start gate timed out")
    }
    func waitForStopRelease() {
        precondition(stopGate.wait(timeout: .now() + 10) == .success, "Stop gate timed out")
    }
}

@MainActor
private enum Fixture {
    static var probe = Probe()
}

@MainActor
final class MicRecorder {
    var captureTimeline: RecordingCaptureTimeline?
    private let probe = Fixture.probe
    let firstBufferAt: Date? = nil
    var onStatusChange: (@MainActor @Sendable (MicCaptureStatus) -> Void)?
    var captureReport: [String: Any] = ["coverage_complete": true]

    func start(writingTo url: URL) throws { probe.record("mic-start") }
    func stop() { probe.record("mic-stop") }
}

@MainActor
final class SystemAudioRecorder {
    var captureTimeline: RecordingCaptureTimeline?
    private let probe: Probe
    private let control = SerialAudioControl(label: "quill.test.audio-control")
    let firstBufferAt: Date? = nil
    let captureFailure: String? = nil
    let hasCapturedAudio = true
    var onStatusChange: (@MainActor @Sendable (SystemAudioCaptureStatus) -> Void)?
    private var stopRequested = false
    var captureReport: [String: Any] { ["received_audio": true, "coverage_complete": true] }

    @MainActor
    init() { probe = Fixture.probe }

    @MainActor
    func start(writingTo url: URL) async throws {
        try await control.perform { [probe] in
            probe.record("system-start-enter")
            probe.waitForStartRelease()
            probe.record("system-start-return")
        }
    }

    func requestStop() {
        guard !stopRequested else { return }
        stopRequested = true
        probe.record("system-stop-request")
    }

    @MainActor
    func stop() async {
        await control.finish { [probe] in
            probe.record("system-stop-enter")
            probe.waitForStopRelease()
            probe.record("system-stop-return")
        }
    }
}

@main
private struct AudioLifecycleChecks {
    enum CheckError: Error { case invalidated }

    @MainActor
    static func waitUntil(_ condition: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(3)
        while !condition() {
            precondition(Date() < deadline, "Expected lifecycle event timed out")
            try await Task.sleep(for: .milliseconds(5))
        }
    }

    @MainActor
    static func withSession(_ check: (RecordingSession, Probe) async throws -> Void) async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("quill-audio-check-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let probe = Probe()
        Fixture.probe = probe
        try await check(RecordingSession(root: root), probe)
    }

    @MainActor
    static func blockedStartAndConcurrentStop() async throws {
        try await withSession { session, probe in
            let start = Task { @MainActor in try await session.start() }
            try await waitUntil { probe.events.contains("system-start-enter") }

            // This code can run only if the main actor was released while the
            // native start is blocked. Stop must still wait behind that start.
            probe.record("main-actor-responsive")
            var firstStopFinished = false
            var secondStopFinished = false
            let firstStop = Task { @MainActor in
                await session.stopTracks()
                firstStopFinished = true
            }
            let secondStop = Task { @MainActor in
                await session.stopTracks()
                secondStopFinished = true
            }
            try await waitUntil { probe.events.contains("mic-stop") }
            precondition(!probe.events.contains("system-stop-enter"), "Stop overtook blocked start")

            probe.startGate.signal()
            try await waitUntil { probe.events.contains("system-stop-enter") }
            precondition(!firstStopFinished && !secondStopFinished, "Stop returned before cleanup")
            precondition(!probe.events.contains("mic-start"), "Mic started after stop was requested")

            let finalize = Task { @MainActor in try await session.finalize() }
            await Task.yield()
            precondition(!FileManager.default.fileExists(atPath: session.dir.appendingPathComponent("meta.json").path),
                         "Metadata was published before cleanup")
            probe.stopGate.signal()
            await firstStop.value
            await secondStop.value
            try await finalize.value
            do {
                try await start.value
                preconditionFailure("Stopped start unexpectedly succeeded")
            } catch is CancellationError {}
            await session.stopTracks()

            precondition(probe.events.filter { $0 == "system-stop-enter" }.count == 1,
                         "Concurrent cleanup was duplicated")
            precondition(probe.events.firstIndex(of: "system-start-return")!
                         < probe.events.firstIndex(of: "system-stop-enter")!, "Control order changed")
            precondition(FileManager.default.fileExists(atPath: session.dir.appendingPathComponent("meta.json").path))
            let metadata = try JSONSerialization.jsonObject(with:
                Data(contentsOf: session.dir.appendingPathComponent("meta.json"))) as! [String: Any]
            precondition(metadata["transcription_requested"] as? Bool == false)
            precondition(metadata["screen_recording_requested"] as? Bool == false)
            precondition(metadata["screen_recording_started"] as? Bool == false)
        }
    }

    @MainActor
    static func cancelledStartAwaitsCleanup() async throws {
        try await withSession { session, probe in
            var startFinished = false
            var validated = false
            let start = Task { @MainActor in
                defer { startFinished = true }
                try await session.start { validated = true }
            }
            try await waitUntil { probe.events.contains("system-start-enter") }
            start.cancel()
            probe.startGate.signal()
            try await waitUntil { probe.events.contains("system-stop-enter") }
            precondition(!startFinished, "Cancellation abandoned cleanup")
            precondition(!validated, "Cancelled start reached validation")
            precondition(!probe.events.contains("mic-start"), "Cancelled start opened mic")
            probe.stopGate.signal()
            do {
                try await start.value
                preconditionFailure("Cancelled start unexpectedly succeeded")
            } catch is CancellationError {}
            precondition(probe.events.last == "system-stop-return")
        }
    }

    @MainActor
    static func validationFailureAwaitsCleanup() async throws {
        try await withSession { session, probe in
            var allowed = true
            var validated = false
            var startFinished = false
            let start = Task { @MainActor in
                defer { startFinished = true }
                try await session.start {
                    validated = true
                    if !allowed { throw CheckError.invalidated }
                }
            }
            try await waitUntil { probe.events.contains("system-start-enter") }
            allowed = false
            probe.startGate.signal()
            try await waitUntil { probe.events.contains("system-stop-enter") }
            precondition(validated && !startFinished)
            precondition(!probe.events.contains("mic-start"), "Invalidated start opened mic")
            probe.stopGate.signal()
            do {
                try await start.value
                preconditionFailure("Invalidated start unexpectedly succeeded")
            } catch CheckError.invalidated {}
            precondition(probe.events.last == "system-stop-return")
        }
    }

    @MainActor
    static func successfulStartValidatesBeforeMic() async throws {
        try await withSession { session, probe in
            probe.startGate.signal()
            try await session.start { probe.record("validated") }
            precondition(probe.events == ["system-start-enter", "system-start-return", "validated", "mic-start"])
            probe.stopGate.signal()
            await session.stopTracks()
            await session.stopTracks()
            precondition(probe.events.filter { $0 == "system-stop-enter" }.count == 1)
            try await session.finalize(transcriptionRequested: true, screenRequested: true, screenStarted: false)
            let metadata = try JSONSerialization.jsonObject(with:
                Data(contentsOf: session.dir.appendingPathComponent("meta.json"))) as! [String: Any]
            precondition(metadata["transcription_requested"] as? Bool == true)
            precondition(metadata["screen_recording_requested"] as? Bool == true)
            precondition(metadata["screen_recording_started"] as? Bool == false)
        }
    }

    @MainActor
    static func sleepStopIsImmediateAndKeepsOriginalEndTime() async throws {
        try await withSession { session, probe in
            probe.startGate.signal()
            try await session.start()
            let stopAt = session.startedAt.addingTimeInterval(60)

            // No task scheduling or await may be required to invalidate the
            // microphone before a system-sleep notification returns.
            session.requestStop(at: stopAt)
            precondition(probe.events.contains("mic-stop"), "Sleep stop deferred microphone shutdown")
            precondition(probe.events.contains("system-stop-request"), "Sleep stop deferred system recovery invalidation")
            precondition(!probe.events.contains("system-stop-enter"), "Cleanup unexpectedly blocked the sleep handler")
            session.requestStop(at: stopAt.addingTimeInterval(3_600))
            precondition(probe.events.filter { $0 == "mic-stop" }.count == 1,
                         "Repeated sleep notifications stopped the microphone twice")
            precondition(probe.events.filter { $0 == "system-stop-request" }.count == 1,
                         "Repeated sleep notifications invalidated system recovery twice")

            let firstStop = Task { @MainActor in await session.stopTracks() }
            let secondStop = Task { @MainActor in await session.stopTracks() }
            try await waitUntil { probe.events.contains("system-stop-enter") }
            let finalize = Task { @MainActor in try await session.finalize() }
            await Task.yield()
            precondition(!FileManager.default.fileExists(atPath: session.dir.appendingPathComponent("meta.json").path),
                         "Sleep published metadata while system cleanup was still pending")

            // A wake or delayed Stop cannot restart the same session or
            // replace its first stop request with the time cleanup finishes.
            do {
                try await session.start()
                preconditionFailure("Sleep-stopped session restarted while cleanup was pending")
            } catch is CancellationError {}
            session.requestStop(at: stopAt.addingTimeInterval(19 * 3_600))
            probe.stopGate.signal()
            await firstStop.value
            await secondStop.value
            try await finalize.value
            session.requestStop(at: stopAt.addingTimeInterval(20 * 3_600))
            await session.stopTracks()
            try await session.finalize()

            let metadata = try JSONSerialization.jsonObject(with:
                Data(contentsOf: session.dir.appendingPathComponent("meta.json"))) as! [String: Any]
            precondition(metadata["ended"] as? String == ISO8601DateFormatter().string(from: stopAt),
                         "Delayed cleanup changed the stop timestamp")
            precondition(metadata["duration_seconds"] as? Int == 60,
                         "Time after sleep was counted as recorded meeting time")
            precondition(probe.events.filter { $0 == "mic-stop" }.count == 1)
            precondition(probe.events.filter { $0 == "system-stop-enter" }.count == 1)
            precondition(probe.events.filter { $0 == "mic-start" }.count == 1)
        }
    }

    @MainActor
    static func sleepDuringPermissionWaitPreventsMicrophoneStart() async throws {
        try await withSession { session, probe in
            var validated = false
            let start = Task { @MainActor in try await session.start { validated = true } }
            try await waitUntil { probe.events.contains("system-start-enter") }
            let stopAt = session.startedAt.addingTimeInterval(5)
            session.requestStop(at: stopAt)
            precondition(probe.events.contains("mic-stop"), "Pending start was not stopped synchronously")
            precondition(probe.events.contains("system-stop-request"), "Pending system recovery was not invalidated synchronously")
            probe.startGate.signal()
            try await waitUntil { probe.events.contains("system-stop-enter") }
            precondition(!validated, "Sleep-cancelled start reached capture validation")
            precondition(!probe.events.contains("mic-start"), "Microphone started after sleep interrupted permission")
            probe.stopGate.signal()
            do {
                try await start.value
                preconditionFailure("Permission completion revived the interrupted session")
            } catch is CancellationError {}
            try await session.finalize()
            let metadata = try JSONSerialization.jsonObject(with:
                Data(contentsOf: session.dir.appendingPathComponent("meta.json"))) as! [String: Any]
            precondition(metadata["ended"] as? String == ISO8601DateFormatter().string(from: stopAt))
            precondition(probe.events.filter { $0 == "mic-stop" }.count == 1)
            precondition(probe.events.filter { $0 == "system-stop-enter" }.count == 1)
        }
    }

    @MainActor
    static func recordingTimezoneSnapshot() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("quill-date-check-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let iso = ISO8601DateFormatter()
        let cases = [
            ("Asia/Singapore", "2026-09-28T05:12:41Z", "2026.09.28-1312", 28800),
            ("America/Los_Angeles", "2026-09-28T05:12:41Z", "2026.09.27-2212", -25200),
            ("America/Los_Angeles", "2026-01-01T08:10:00Z", "2026.01.01-0010", -28800),
        ]
        for (zone, start, folder, offset) in cases {
            let probe = Probe()
            Fixture.probe = probe
            // Inject each recording's start location independently of this
            // process's current location. Never change the system timezone.
            let session = try RecordingSession(root: root, startedAt: iso.date(from: start)!,
                                               timeZone: TimeZone(identifier: zone)!)
            precondition(session.dir.lastPathComponent == folder, "Folder used a stale timezone")
            probe.stopGate.signal()
            try await session.finalize()
            let metadata = try JSONSerialization.jsonObject(with:
                Data(contentsOf: session.dir.appendingPathComponent("meta.json"))) as! [String: Any]
            precondition(metadata["started"] as? String == start)
            precondition(metadata["recording_timezone"] as? String == zone,
                         "Finalization replaced the recording's original timezone")
            precondition(metadata["recording_utc_offset_seconds"] as? Int == offset)
        }
    }

    @MainActor
    static func speakerOptInSnapshot() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("quill-speaker-opt-in-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        for (requested, transcribe) in [(true, true), (true, false), (false, true)] {
            let probe = Probe()
            Fixture.probe = probe
            let session = try RecordingSession(root: root, speakerIdentificationRequested: requested)
            probe.stopGate.signal()
            try await session.finalize(transcriptionRequested: transcribe)
            let metadata = try JSONSerialization.jsonObject(with:
                Data(contentsOf: session.dir.appendingPathComponent("meta.json"))) as! [String: Any]
            precondition(metadata["speaker_identification_requested"] as? Bool == (requested && transcribe))
        }
    }

    @MainActor
    static func main() async throws {
        try await blockedStartAndConcurrentStop()
        try await cancelledStartAwaitsCleanup()
        try await validationFailureAwaitsCleanup()
        try await successfulStartValidatesBeforeMic()
        try await sleepStopIsImmediateAndKeepsOriginalEndTime()
        try await sleepDuringPermissionWaitPreventsMicrophoneStart()
        try await recordingTimezoneSnapshot()
        try await speakerOptInSnapshot()
        print("Audio lifecycle checks passed (8 scenarios; no capture devices opened).")
    }
}
