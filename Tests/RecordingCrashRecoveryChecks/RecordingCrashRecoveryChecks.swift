import AVFoundation
import Darwin
import Foundation
import MeetingWorkflow

/// Standalone executable to support a real SIGKILL writer. All audio is
/// generated; no microphone, screen capture, models, hooks or user files run.
@main
private enum RecordingCrashRecoveryChecks {
    static let started = Date(timeIntervalSince1970: 1_700_000_000)
    static let reason = RecordingCrashRecovery.recoveryReason

    static func main() throws {
        if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--child" {
            try child(URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true))
            return
        }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("quill-recovery-\(UUID())")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try checkRecovery(root)
        try checkCompletedStates(root)
        try checkTimelineAndStartup(root)
        try checkInvalidSessions(root)
        try checkActualCrash(root)
        print("Recording crash recovery checks passed (generated local fixtures only).")
    }

    static func snapshot() -> [String: Any] {
        ["started": ISO8601DateFormatter().string(from: started),
         "ended": ISO8601DateFormatter().string(from: started.addingTimeInterval(100)),
         "recording_timezone": "Asia/Singapore", "recording_utc_offset_seconds": 28_800,
         "duration_seconds": 95, "wall_duration_seconds": 100, "paused_duration_seconds": 5.0,
         "files": ["mic": "mic.caf", "system": "system.caf"],
         "capture_storage_format": "pcm_s16", "start_offset_ms": ["mic": 100, "system": 0],
         "transcription_requested": true, "speaker_identification_requested": true,
         "screen_recording_requested": true, "screen_recording_started": true,
         "mic_capture": ["coverage_complete": true, "captured_frames": 999_999,
                         "interruptions": [["reason": "test_gap"]], "errors": ["test_error"]],
         "system_capture": ["coverage_complete": true, "captured_frames": 999_999]]
    }

    static func make(_ root: URL, _ name: String, status: RecordingReviewGate.ReviewStatus = .pending) throws -> (URL, UUID) {
        let directory = root.appendingPathComponent(name)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let id = UUID()
        try RecordingReviewGate.write(.init(sessionID: id, owner: .manual, startedAt: started,
            checkInAt: started.addingTimeInterval(60), stopAt: started.addingTimeInterval(120),
            reviewStatus: status, reasons: ["Existing reason"]), session: directory)
        return (directory, id)
    }

    static func json(_ url: URL) throws -> [String: Any] {
        try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
    }

    static func writeJSON(_ value: [String: Any], _ url: URL) throws {
        try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]).write(to: url)
    }

    static func exists(_ directory: URL, _ name: String) -> Bool {
        FileManager.default.fileExists(atPath: directory.appendingPathComponent(name).path)
    }

    static func check(_ value: @autoclosure () throws -> Bool) rethrows {
        let result = try value()
        precondition(result)
    }

    static func expectThrows(_ body: () throws -> Void) {
        do { try body(); preconditionFailure("Expected rejection") } catch {}
    }

    @discardableResult
    static func pcm(_ url: URL, channels: AVAudioChannelCount = 1, frames: AVAudioFrameCount = 48_000,
                    closeFile: Bool = true) throws -> AVAudioFile? {
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000,
                                   channels: channels, interleaved: false)!
        let file = try AVAudioFile(forWriting: url, settings: [
            AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48_000,
            AVNumberOfChannelsKey: channels, AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false, AVLinearPCMIsNonInterleaved: false,
        ], commonFormat: .pcmFormatFloat32, interleaved: false)
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
        buffer.frameLength = frames
        for channel in 0..<Int(channels) {
            for frame in 0..<Int(frames) { buffer.floatChannelData![channel][frame] = Float(sin(Double(frame) / 24) * 0.2) }
        }
        try file.write(from: buffer)
        return closeFile ? nil : file
    }

    static func abandoned(_ directory: URL, id: UUID) throws {
        let lease = try RecordingCrashRecovery.begin(session: directory, sessionID: id, metadata: snapshot())
        try pcm(directory.appendingPathComponent("mic.caf"))
        try pcm(directory.appendingPathComponent("system.caf"), channels: 2, frames: 96_000)
        try Data("unfinalized video".utf8).write(to: directory.appendingPathComponent("screen.mp4"))
        withExtendedLifetime(lease) {}
    }

    static func checkRecovery(_ root: URL) throws {
        let scope = root.appendingPathComponent("recovery")
        let (directory, id) = try make(scope, "normal")
        try abandoned(directory, id: id)
        let beforeMic = try Data(contentsOf: directory.appendingPathComponent("mic.caf"))
        let summary = RecordingCrashRecovery.recoverPending(root: scope)
        precondition(summary.recovered == 1 && summary.failed == 0)
        let meta = try json(directory.appendingPathComponent("meta.json"))
        precondition(meta["duration_seconds"] as? Double == 2 && meta["wall_duration_seconds"] as? Double == 7)
        precondition(meta["ended"] as? String == ISO8601DateFormatter().string(from: started.addingTimeInterval(7)))
        precondition(meta["recording_timezone"] as? String == "Asia/Singapore")
        precondition(meta["transcription_requested"] as? Bool == true)
        let files = meta["files"] as! [String: String]
        precondition(files == ["mic": "mic.caf", "system": "system.caf"])
        precondition(meta["screen_recording_started"] as? Bool == true && meta["screen_error"] is String)
        let mic = meta["mic_capture"] as! [String: Any]
        precondition(mic["captured_frames"] as? Int == 48_000 && mic["coverage_complete"] as? Bool == false)
        precondition(mic["errors"] as? [String] == ["test_error"] && mic["interruptions"] != nil)
        let gate = try RecordingReviewGate.read(session: directory)!
        precondition(gate.phase == .finished && gate.reviewStatus == .pending)
        precondition(gate.reasons.contains("Existing reason") && gate.reasons.contains(reason))
        precondition(RecordingReviewGate.blocksProcessing(session: directory))
        precondition(TranscriptionRecovery.pendingSessions(root: scope, waitingForTranscript: []).isEmpty)
        precondition(!exists(directory, RecordingCrashRecovery.journalFilename))
        precondition(exists(directory, RecordingReviewGate.processingLeaseMarkerFilename))
        precondition(exists(directory, "screen.mp4"))
        try check(try Data(contentsOf: directory.appendingPathComponent("mic.caf")) == beforeMic)
        let saved = try Data(contentsOf: directory.appendingPathComponent("meta.json"))
        precondition(RecordingCrashRecovery.recoverPending(root: scope).recovered == 0)
        try check(try Data(contentsOf: directory.appendingPathComponent("meta.json")) == saved)
        precondition(RecordingReviewGate.pendingSessions(root: scope).map(\.lastPathComponent).contains(directory.lastPathComponent))
        try RecordingReviewGate.decide(session: directory, sessionID: id, status: .approved)
        precondition(!RecordingReviewGate.blocksProcessing(session: directory))
        precondition(TranscriptionRecovery.pendingSessions(root: scope, waitingForTranscript: []).map(\.lastPathComponent).contains(directory.lastPathComponent))
        print("PASS recovery uses decoded PCM length, retains media/reasons/intents, holds processing, and is idempotent until explicit approval")

        let (active, activeID) = try make(scope, "active")
        var lease: RecordingCrashRecovery.CaptureLease? = try RecordingCrashRecovery.begin(session: active, sessionID: activeID, metadata: snapshot())
        try pcm(active.appendingPathComponent("mic.caf"))
        try pcm(active.appendingPathComponent("system.caf"))
        precondition(RecordingCrashRecovery.recoverPending(root: scope).skipped == 1 && !exists(active, "meta.json"))
        var updated = snapshot()
        updated["ended"] = ISO8601DateFormatter().string(from: started.addingTimeInterval(101))
        try lease!.checkpoint(metadata: updated)
        try check(try (json(active.appendingPathComponent(RecordingCrashRecovery.journalFilename))["metadata"] as! [String: Any])["ended"] as? String == updated["ended"] as? String)
        expectThrows { try lease!.complete() }
        try Data("{}".utf8).write(to: active.appendingPathComponent(RecordingCrashRecovery.journalFilename))
        expectThrows { try lease!.checkpoint(metadata: updated) }
        try check(try Data(contentsOf: active.appendingPathComponent(RecordingCrashRecovery.journalFilename)) == Data("{}".utf8))
        lease = nil
        print("PASS live capture is skipped; checkpoints are atomic and reject replaced journals; premature completion is refused")
    }

    static func checkCompletedStates(_ root: URL) throws {
        let scope = root.appendingPathComponent("states")
        for status: RecordingReviewGate.ReviewStatus in [.pending, .keptLocal] {
            let (directory, id) = try make(scope, "salvage-\(status.rawValue)", status: status)
            try abandoned(directory, id: id)
            precondition(RecordingCrashRecovery.recoverPending(root: scope).recovered == 1)
            try check(try RecordingReviewGate.read(session: directory)!.reviewStatus == status)
        }
        for status: RecordingReviewGate.ReviewStatus in [.approved, .pending, .keptLocal] {
            let (directory, id) = try make(scope, "complete-\(status.rawValue)")
            var lease: RecordingCrashRecovery.CaptureLease? = try RecordingCrashRecovery.begin(session: directory, sessionID: id, metadata: snapshot())
            try writeJSON(snapshot(), directory.appendingPathComponent("meta.json"))
            try RecordingReviewGate.finish(session: directory, sessionID: id)
            if status != .pending { try RecordingReviewGate.decide(session: directory, sessionID: id, status: status) }
            let safety = try Data(contentsOf: directory.appendingPathComponent(RecordingReviewGate.filename))
            let metadata = try Data(contentsOf: directory.appendingPathComponent("meta.json"))
            try Data("preserved transcript".utf8).write(to: directory.appendingPathComponent("transcript.json"))
            lease = nil // crash after gate.finish, before complete
            precondition(lease == nil)
            precondition(RecordingCrashRecovery.recoverPending(root: scope).recovered == 0)
            precondition(!exists(directory, RecordingCrashRecovery.journalFilename))
            try check(try Data(contentsOf: directory.appendingPathComponent(RecordingReviewGate.filename)) == safety)
            try check(try Data(contentsOf: directory.appendingPathComponent("meta.json")) == metadata)
            try check(try String(contentsOf: directory.appendingPathComponent("transcript.json"), encoding: .utf8) == "preserved transcript")
        }
        let (published, id) = try make(scope, "meta-before-gate-finish")
        try abandoned(published, id: id)
        try writeJSON(snapshot(), published.appendingPathComponent("meta.json"))
        let saved = try Data(contentsOf: published.appendingPathComponent("meta.json"))
        precondition(RecordingCrashRecovery.recoverPending(root: scope).recovered == 1)
        try check(try Data(contentsOf: published.appendingPathComponent("meta.json")) == saved)
        try check(try RecordingReviewGate.read(session: published)!.reviewStatus == .pending)
        try check(try RecordingReviewGate.read(session: published)!.reasons.contains(reason))
        let (clean, cleanID) = try make(scope, "clean-complete")
        let cleanLease = try RecordingCrashRecovery.begin(session: clean, sessionID: cleanID, metadata: snapshot())
        try writeJSON(snapshot(), clean.appendingPathComponent("meta.json"))
        try RecordingReviewGate.finish(session: clean, sessionID: cleanID)
        try cleanLease.complete()
        try cleanLease.complete()
        precondition(!exists(clean, RecordingCrashRecovery.journalFilename))
        withExtendedLifetime(cleanLease) {}
        print("PASS Keep Local, already completed decisions, existing transcript, crash between metadata/gate finish, and clean completion remain intact")
    }

    static func checkInvalidSessions(_ root: URL) throws {
        let scope = root.appendingPathComponent("invalid")
        let (legacy, _) = try make(scope, "legacy")
        try pcm(legacy.appendingPathComponent("mic.caf"))
        let legacyInventory = try FileManager.default.contentsOfDirectory(atPath: legacy.path).sorted()
        let (missing, missingID) = try make(scope, "missing-system")
        do {
            let lease = try RecordingCrashRecovery.begin(session: missing, sessionID: missingID, metadata: snapshot())
            try pcm(missing.appendingPathComponent("mic.caf"))
            withExtendedLifetime(lease) {}
        }
        let (deleted, deletedID) = try make(scope, "deleted")
        try abandoned(deleted, id: deletedID)
        try Data("{}".utf8).write(to: deleted.appendingPathComponent(RecordingReviewGate.deletionFilename))
        let (symlink, symlinkID) = try make(scope, "symlink-track")
        try abandoned(symlink, id: symlinkID)
        try FileManager.default.removeItem(at: symlink.appendingPathComponent("system.caf"))
        try FileManager.default.createSymbolicLink(at: symlink.appendingPathComponent("system.caf"), withDestinationURL: legacy.appendingPathComponent("mic.caf"))
        let (malformed, malformedID) = try make(scope, "malformed")
        try abandoned(malformed, id: malformedID)
        try Data("{}".utf8).write(to: malformed.appendingPathComponent(RecordingCrashRecovery.journalFilename))
        let (oversized, oversizedID) = try make(scope, "oversized")
        try abandoned(oversized, id: oversizedID)
        try Data(repeating: 32, count: 100_001).write(to: oversized.appendingPathComponent(RecordingCrashRecovery.journalFilename))
        let (identity, identityID) = try make(scope, "identity")
        try abandoned(identity, id: identityID)
        var wrong = try json(identity.appendingPathComponent(RecordingCrashRecovery.journalFilename))
        wrong["session_id"] = UUID().uuidString
        try writeJSON(wrong, identity.appendingPathComponent(RecordingCrashRecovery.journalFilename))
        let (existing, existingID) = try make(scope, "invalid-existing-meta")
        try abandoned(existing, id: existingID)
        try Data("{}".utf8).write(to: existing.appendingPathComponent("meta.json"))
        let (transcript, transcriptID) = try make(scope, "orphan-transcript")
        try abandoned(transcript, id: transcriptID)
        try Data("preserve".utf8).write(to: transcript.appendingPathComponent("transcript.json"))
        let (busy, busyID) = try make(scope, "busy-file-lock")
        try abandoned(busy, id: busyID)
        let busyLock = open(busy.appendingPathComponent(RecordingReviewGate.processingLockFilename).path, O_RDWR)
        precondition(busyLock >= 0 && flock(busyLock, LOCK_SH | LOCK_NB) == 0)
        defer { close(busyLock) }
        let (fifo, fifoID) = try make(scope, "fifo-journal")
        try abandoned(fifo, id: fifoID)
        try FileManager.default.removeItem(at: fifo.appendingPathComponent(RecordingCrashRecovery.journalFilename))
        precondition(mkfifo(fifo.appendingPathComponent(RecordingCrashRecovery.journalFilename).path, 0o600) == 0)
        let (linked, linkedID) = try make(scope, "symlink-journal")
        try abandoned(linked, id: linkedID)
        try FileManager.default.removeItem(at: linked.appendingPathComponent(RecordingCrashRecovery.journalFilename))
        try FileManager.default.createSymbolicLink(at: linked.appendingPathComponent(RecordingCrashRecovery.journalFilename),
            withDestinationURL: missing.appendingPathComponent(RecordingCrashRecovery.journalFilename))
        let (badPCM, badPCMID) = try make(scope, "truncated-caf")
        try abandoned(badPCM, id: badPCMID)
        try Data("caff invalid".utf8).write(to: badPCM.appendingPathComponent("system.caf"))
        _ = RecordingCrashRecovery.recoverPending(root: scope)
        for directory in [legacy, missing, deleted, symlink, malformed, oversized, identity, transcript, busy, fifo, linked, badPCM] {
            precondition(!exists(directory, "meta.json"), "Unexpected metadata for \(directory.lastPathComponent)")
        }
        try check(try Data(contentsOf: existing.appendingPathComponent("meta.json")) == Data("{}".utf8))
        try check(try FileManager.default.contentsOfDirectory(atPath: legacy.path).sorted() == legacyInventory)
        try check(try RecordingReviewGate.read(session: missing)!.reasons.contains(reason))
        try check(try RecordingReviewGate.read(session: missing)!.phase == .active)
        precondition(exists(missing, RecordingCrashRecovery.journalFilename))
        try check(try RecordingReviewGate.read(session: deleted)!.reasons == ["Existing reason"])
        try check(try RecordingReviewGate.read(session: malformed)!.reasons == ["Existing reason"])
        for key in ["duration_seconds", "paused_duration_seconds", "transcription_requested", "started", "capture_storage_format"] {
            let (bad, id) = try make(scope, "bad-field-\(key)")
            var value = snapshot()
            value[key] = key == "started" ? "bad-date" : "wrong-type"
            expectThrows { _ = try RecordingCrashRecovery.begin(session: bad, sessionID: id, metadata: value) }
            precondition(!exists(bad, RecordingCrashRecovery.journalFilename))
        }
        print("PASS legacy, missing track, deletion, symlink, corrupt/oversized/foreign journals, existing metadata/transcript and busy deletion leases fail closed")
    }

    static func checkTimelineAndStartup(_ root: URL) throws {
        let scope = root.appendingPathComponent("timelines")
        let (directory, id) = try make(scope, "backward-clock-and-permission-delay")
        var initial = snapshot()
        initial["duration_seconds"] = 0
        initial["wall_duration_seconds"] = 0
        initial["paused_duration_seconds"] = 0
        initial["mic_capture"] = [:] as [String: Any]
        initial["system_capture"] = [:] as [String: Any]
        var lease: RecordingCrashRecovery.CaptureLease? = try RecordingCrashRecovery.begin(session: directory, sessionID: id, metadata: initial)
        try pcm(directory.appendingPathComponent("mic.caf"))
        try pcm(directory.appendingPathComponent("system.caf"), channels: 2, frames: 96_000)
        var movedClock = snapshot()
        movedClock["ended"] = ISO8601DateFormatter().string(from: started.addingTimeInterval(-3_600))
        movedClock["capture_start_offset_seconds"] = 30.0
        movedClock["system_capture"] = ["captured_frames": 1_000_000, "silence_frames": 12_000]
        try lease!.checkpoint(metadata: movedClock)
        lease = nil
        precondition(lease == nil)
        precondition(RecordingCrashRecovery.recoverPending(root: scope).recovered == 1)
        let recovered = try json(directory.appendingPathComponent("meta.json"))
        precondition(recovered["duration_seconds"] as? Double == 2)
        precondition(recovered["wall_duration_seconds"] as? Double == 37)
        precondition(recovered["ended"] as? String == ISO8601DateFormatter().string(from: started.addingTimeInterval(37)))
        let system = recovered["system_capture"] as! [String: Any]
        precondition(system["captured_frames"] as? Int == 84_000 && system["silence_frames"] as? Int == 12_000)
        precondition(system["decoded_frames"] as? Int == 96_000)
        for status: RecordingReviewGate.ReviewStatus in [.pending, .keptLocal] {
            let (finished, finishedID) = try make(scope, "failed-start-\(status.rawValue)", status: status)
            try abandoned(finished, id: finishedID)
            try RecordingReviewGate.finish(session: finished, sessionID: finishedID, reasons: ["Failed start"])
            precondition(RecordingCrashRecovery.recoverPending(root: scope).recovered == 1)
            try check(try RecordingReviewGate.read(session: finished)!.reviewStatus == status)
        }
        let (clean, cleanID) = try make(scope, "clean-stop-backward-clock")
        let cleanLease = try RecordingCrashRecovery.begin(session: clean, sessionID: cleanID, metadata: initial)
        var final = snapshot()
        final["ended"] = movedClock["ended"]
        final["duration_seconds"] = -3_600
        final["wall_duration_seconds"] = -3_600
        try writeJSON(final, clean.appendingPathComponent("meta.json"))
        try RecordingReviewGate.finish(session: clean, sessionID: cleanID)
        try cleanLease.complete()
        precondition(!exists(clean, RecordingCrashRecovery.journalFilename))
        withExtendedLifetime(cleanLease) {}
        print("PASS empty initial snapshot, backward wall clock, capture startup delay, silence-frame contract and finished failed-start journals")
    }

    static func child(_ directory: URL) throws {
        let (_, id) = try make(directory.deletingLastPathComponent(), directory.lastPathComponent)
        let lease = try RecordingCrashRecovery.begin(session: directory, sessionID: id, metadata: snapshot())
        let mic = try pcm(directory.appendingPathComponent("mic.caf"), closeFile: false)
        let system = try pcm(directory.appendingPathComponent("system.caf"), channels: 2, frames: 96_000, closeFile: false)
        try lease.checkpoint(metadata: snapshot())
        try Data("ready".utf8).write(to: directory.appendingPathComponent("ready"))
        withExtendedLifetime((lease, mic, system)) { while true { pause() } }
    }

    static func checkActualCrash(_ root: URL) throws {
        let scope = root.appendingPathComponent("actual-crash")
        try FileManager.default.createDirectory(at: scope, withIntermediateDirectories: true)
        let directory = scope.appendingPathComponent("killed")
        let child = Process()
        child.executableURL = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
        child.arguments = ["--child", directory.path]
        child.standardInput = FileHandle.nullDevice
        child.standardOutput = FileHandle.nullDevice
        try child.run()
        defer { if child.isRunning { kill(child.processIdentifier, SIGKILL); child.waitUntilExit() } }
        let deadline = Date().addingTimeInterval(10)
        while !exists(directory, "ready") && child.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.01) }
        precondition(child.isRunning && exists(directory, "ready"))
        precondition(RecordingCrashRecovery.recoverPending(root: scope).skipped == 1)
        precondition(kill(child.processIdentifier, SIGKILL) == 0)
        child.waitUntilExit()
        precondition(child.terminationReason == .uncaughtSignal && child.terminationStatus == SIGKILL)
        let mic = try AVAudioFile(forReading: directory.appendingPathComponent("mic.caf"))
        let system = try AVAudioFile(forReading: directory.appendingPathComponent("system.caf"))
        let actualDuration = max(Double(mic.length) / mic.fileFormat.sampleRate + 0.1,
                                 Double(system.length) / system.fileFormat.sampleRate)
        precondition(RecordingCrashRecovery.recoverPending(root: scope).recovered == 1)
        let meta = try json(directory.appendingPathComponent("meta.json"))
        precondition(meta["duration_seconds"] as? Double == actualDuration)
        precondition((meta["system_capture"] as! [String: Any])["captured_frames"] as? Int64 == system.length)
        precondition(RecordingReviewGate.blocksProcessing(session: directory))
        precondition(TranscriptionRecovery.pendingSessions(root: scope, waitingForTranscript: []).isEmpty)
        let saved = try Data(contentsOf: directory.appendingPathComponent("meta.json"))
        precondition(RecordingCrashRecovery.recoverPending(root: scope).recovered == 0)
        try check(try Data(contentsOf: directory.appendingPathComponent("meta.json")) == saved)
        let gate = try RecordingReviewGate.read(session: directory)!
        try RecordingReviewGate.decide(session: directory, sessionID: gate.sessionID, status: .approved)
        precondition(!RecordingReviewGate.blocksProcessing(session: directory))
        precondition(TranscriptionRecovery.pendingSessions(root: scope, waitingForTranscript: []).map(\.lastPathComponent).contains(directory.lastPathComponent))
        print("PASS actual SIGKILL: unlocked surviving PCM is held, idempotently recovered with decoded frame counts, and only eligible after approval")
    }
}
