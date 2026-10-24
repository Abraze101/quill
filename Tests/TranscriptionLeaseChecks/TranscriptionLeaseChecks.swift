import Darwin
import Foundation
import MeetingWorkflow

enum Config {
    private static let settings = TestSettings()
    static func transcriptionEnabled() -> Bool { settings.enabled() }
    static func setTranscriptionEnabled(_ enabled: Bool) { settings.setEnabled(enabled) }
    static func transcriptionEngine() -> String { "parakeet" }
}

private final class TestSettings: @unchecked Sendable {
    private let lock = NSLock()
    private var value = true
    func enabled() -> Bool { lock.withLock { value } }
    func setEnabled(_ enabled: Bool) { lock.withLock { value = enabled } }
}

private final class StatusProbe: @unchecked Sendable {
    private let lock = NSLock()
    private var latest: TranscriptionCoordinator.Status = .idle
    func update(_ status: TranscriptionCoordinator.Status) { lock.withLock { latest = status } }
    func read() -> TranscriptionCoordinator.Status { lock.withLock { latest } }
}

func notifyUser(title: String, body: String) {}

private actor EngineProbe {
    static let shared = EngineProbe()
    var calls = 0
    var allowed = false
    var cancellationCleanup: CheckedContinuation<Void, Never>?
    var cancellations = 0
    var returnAfterCancellation = false
    var preparationAllowed = true
    var preparations = 0
    func prepare() async throws {
        preparations += 1
        while !preparationAllowed { try await Task.sleep(for: .milliseconds(10)) }
    }
    func holdPreparation() { preparationAllowed = false }
    func releasePreparation() { preparationAllowed = true }
    func preparationCount() -> Int { preparations }
    func transcribe() async throws -> [TranscriptSegment] {
        calls += 1
        do {
            while !allowed { try await Task.sleep(for: .milliseconds(10)) }
        } catch {
            cancellations += 1
            await withCheckedContinuation { cancellationCleanup = $0 }
            if returnAfterCancellation {
                returnAfterCancellation = false
                return [.init(start: 0, end: 1, text: "Cancelled output must be discarded")]
            }
            throw error
        }
        return [.init(start: 0, end: 1, text: "Synthetic lease check")]
    }
    func releaseSamples() { allowed = true }
    func holdSamples() { allowed = false }
    func cancelledCount() -> Int { cancellations }
    func finishCancellation(returningSamples: Bool = false) {
        returnAfterCancellation = returningSamples
        cancellationCleanup?.resume()
        cancellationCleanup = nil
    }
    func callCount() -> Int { calls }
}

actor ParakeetEngine: TranscriptionEngine {
    nonisolated let name = "synthetic"
    nonisolated let model = "no-model"
    var speakerStatus: SpeakerIdentificationState = .disabled
    func prepare() async throws { try await EngineProbe.shared.prepare() }
    func transcribe(_ audio: URL) async throws -> [TranscriptSegment] {
        speakerStatus = .disabled
        return try await EngineProbe.shared.transcribe()
    }
    func transcribe(_ audio: URL, identifySpeakers: Bool) async throws -> [TranscriptSegment] {
        guard identifySpeakers else { return try await transcribe(audio) }
        let fixture = audio.deletingLastPathComponent().lastPathComponent
        if fixture == "speakers-skipped-track", audio.lastPathComponent == "system.caf" {
            throw CocoaError(.fileReadCorruptFile)
        }
        if ["speakers-missing-track", "speakers-skipped-track"].contains(fixture) {
            speakerStatus = .completed
            return [.init(start: 0, end: 1, text: "My name is Alex.", speakerID: "S1")]
        }
        if audio.deletingLastPathComponent().lastPathComponent.contains("unavailable") {
            speakerStatus = .unavailable
            return try await EngineProbe.shared.transcribe()
        }
        speakerStatus = .partial
        let name = audio.lastPathComponent == "mic.caf" ? "Alex" : "Max"
        return [.init(start: 0, end: 1, text: "My name is \(name).", speakerID: "S1"),
                .init(start: 1, end: 2, text: "A different voice.", speakerID: "S2"),
                .init(start: 2, end: 3, text: "Overlapping speech.")]
    }
    func speakerIdentificationStatus() async -> SpeakerIdentificationState { speakerStatus }
    func release() async {}
}

@main
struct TranscriptionLeaseChecks {
    static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try checkLeaseEnrollment(root: root)
        let coordinator = TranscriptionCoordinator(journal: CompletionJournal(directory: root.appendingPathComponent("test-journal")))

        let session = try fixture("active", root: root)
        await coordinator.enqueue(session)
        let startedBy = Date().addingTimeInterval(3)
        while await EngineProbe.shared.callCount() == 0, Date() < startedBy {
            try await Task.sleep(for: .milliseconds(10))
        }
        let startedCalls = await EngineProbe.shared.callCount()
        precondition(startedCalls > 0, "Synthetic transcription did not start")
        let fd = open(session.appendingPathComponent(RecordingReviewGate.processingLockFilename).path,
            O_RDWR | O_NOFOLLOW)
        precondition(fd >= 0)
        defer { close(fd) }
        precondition(flock(fd, LOCK_EX | LOCK_NB) != 0 && errno == EWOULDBLOCK,
            "Transcription must retain its shared lease while the model suspends")
        await EngineProbe.shared.releaseSamples()
        let completedBy = Date().addingTimeInterval(3)
        while !FileManager.default.fileExists(atPath: session.appendingPathComponent("transcript.json").path), Date() < completedBy {
            try await Task.sleep(for: .milliseconds(10))
        }
        let contents = try String(contentsOf: session.appendingPathComponent("transcript.md"), encoding: .utf8)
        precondition(contents.contains("Synthetic lease check"))
        precondition(contents.contains("Microphone and system audio identify audio sources, not individual speakers."))
        precondition(contents.contains("] Microphone:**") && contents.contains("] System audio:**"),
                     "Readable transcript must label sources rather than imply individual people")
        precondition(!contents.contains("] me:**") && !contents.contains("] them:**"))
        let transcript = try JSONSerialization.jsonObject(with: Data(contentsOf: session.appendingPathComponent("transcript.json"))) as! [String: Any]
        precondition(transcript["speaker_labels"] as? String == "audio_source")
        let sourceTracks = (transcript["segments"] as! [[String: Any]]).compactMap { $0["speaker"] as? String }
        precondition(Set(sourceTracks) == Set(["me", "them"]), "Canonical source keys must remain compatible")
        let log = try String(contentsOf: session.appendingPathComponent("transcribe.log"), encoding: .utf8)
        precondition(log.contains("done — 2 segments"))
        var exclusive = flock(fd, LOCK_EX | LOCK_NB) == 0
        while !exclusive, Date() < completedBy {
            try await Task.sleep(for: .milliseconds(10))
            exclusive = flock(fd, LOCK_EX | LOCK_NB) == 0
        }
        precondition(exclusive, "Transcription lease was not released after writes completed")
        flock(fd, LOCK_UN)

        // Cancelling an active transcript must wait for its model to release
        // the audio, then let the next unrelated meeting proceed normally.
        await EngineProbe.shared.holdSamples()
        let cancelled = try fixture("cancel-active", root: root)
        let following = try fixture("following", root: root)
        let beforeCancellation = await EngineProbe.shared.callCount()
        await coordinator.enqueue(cancelled)
        await coordinator.enqueue(following)
        let activeBy = Date().addingTimeInterval(3)
        while await EngineProbe.shared.callCount() == beforeCancellation, Date() < activeBy {
            try await Task.sleep(for: .milliseconds(10))
        }
        let cancelFD = open(cancelled.appendingPathComponent(RecordingReviewGate.processingLockFilename).path,
                            O_RDWR | O_NOFOLLOW)
        precondition(cancelFD >= 0)
        defer { close(cancelFD) }
        let logBeforeCancel = try Data(contentsOf: cancelled.appendingPathComponent("transcribe.log"))
        try Data("deleted".utf8).write(to: cancelled.appendingPathComponent(RecordingReviewGate.deletionFilename))
        let cancelBy = Date().addingTimeInterval(3)
        while await EngineProbe.shared.cancelledCount() == 0, Date() < cancelBy {
            try await Task.sleep(for: .milliseconds(10))
        }
        let cancelledCount = await EngineProbe.shared.cancelledCount()
        precondition(cancelledCount == 1, "The active model never received cancellation")
        precondition(flock(cancelFD, LOCK_EX | LOCK_NB) != 0 && errno == EWOULDBLOCK,
                     "Deletion must wait for cancelled model cleanup")
        await EngineProbe.shared.finishCancellation()
        let nextBy = Date().addingTimeInterval(3)
        while await EngineProbe.shared.callCount() < beforeCancellation + 2, Date() < nextBy {
            try await Task.sleep(for: .milliseconds(10))
        }
        let callsAfterCancel = await EngineProbe.shared.callCount()
        precondition(callsAfterCancel == beforeCancellation + 2, "Cancellation blocked the following meeting")
        precondition(flock(cancelFD, LOCK_EX | LOCK_NB) == 0, "Cancelled transcription kept its lease")
        flock(cancelFD, LOCK_UN)
        precondition(!FileManager.default.fileExists(atPath: cancelled.appendingPathComponent("transcript.json").path))
        let logAfterCancel = try Data(contentsOf: cancelled.appendingPathComponent("transcribe.log"))
        precondition(logAfterCancel == logBeforeCancel, "Cancellation appended output to a removed meeting")
        await EngineProbe.shared.releaseSamples()
        let followingBy = Date().addingTimeInterval(3)
        while !FileManager.default.fileExists(atPath: following.appendingPathComponent("transcript.json").path), Date() < followingBy {
            try await Task.sleep(for: .milliseconds(10))
        }
        precondition(FileManager.default.fileExists(atPath: following.appendingPathComponent("transcript.md").path))

        // Some engines return a final buffer even after cancellation. It is
        // still unsafe to publish that text or proceed to the second track.
        await EngineProbe.shared.holdSamples()
        let returningCancelled = try fixture("cancel-returned-text", root: root)
        let beforeReturnedCancellation = await EngineProbe.shared.callCount()
        let previousCancellations = await EngineProbe.shared.cancelledCount()
        await coordinator.enqueue(returningCancelled)
        let returningBy = Date().addingTimeInterval(3)
        while await EngineProbe.shared.callCount() == beforeReturnedCancellation, Date() < returningBy {
            try await Task.sleep(for: .milliseconds(10))
        }
        let returningFD = open(returningCancelled.appendingPathComponent(RecordingReviewGate.processingLockFilename).path,
                               O_RDWR | O_NOFOLLOW)
        precondition(returningFD >= 0)
        defer { close(returningFD) }
        let returningLog = try Data(contentsOf: returningCancelled.appendingPathComponent("transcribe.log"))
        try Data("deleted".utf8).write(to: returningCancelled.appendingPathComponent(RecordingReviewGate.deletionFilename))
        let returnedCancelBy = Date().addingTimeInterval(3)
        while await EngineProbe.shared.cancelledCount() == previousCancellations, Date() < returnedCancelBy {
            try await Task.sleep(for: .milliseconds(10))
        }
        let returningCancellations = await EngineProbe.shared.cancelledCount()
        precondition(returningCancellations == previousCancellations + 1)
        precondition(flock(returningFD, LOCK_EX | LOCK_NB) != 0 && errno == EWOULDBLOCK)
        await EngineProbe.shared.finishCancellation(returningSamples: true)
        let returnedCleanupBy = Date().addingTimeInterval(3)
        var returnedExclusive = flock(returningFD, LOCK_EX | LOCK_NB) == 0
        while !returnedExclusive, Date() < returnedCleanupBy {
            try await Task.sleep(for: .milliseconds(10))
            returnedExclusive = flock(returningFD, LOCK_EX | LOCK_NB) == 0
        }
        precondition(returnedExclusive, "Cancelled model return did not release the lease after cleanup")
        flock(returningFD, LOCK_UN)
        let callsAfterReturnedCancellation = await EngineProbe.shared.callCount()
        precondition(callsAfterReturnedCancellation == beforeReturnedCancellation + 1,
                     "Cancelled model return started the next audio track")
        precondition(!FileManager.default.fileExists(atPath: returningCancelled.appendingPathComponent("transcript.json").path))
        precondition(!FileManager.default.fileExists(atPath: returningCancelled.appendingPathComponent("transcript.md").path))
        let returningLogAfter = try Data(contentsOf: returningCancelled.appendingPathComponent("transcribe.log"))
        precondition(returningLogAfter == returningLog, "Cancelled model return produced new output")
        await EngineProbe.shared.releaseSamples()

        let blocked = try fixture("deleting", root: root)
        let busy = open(blocked.appendingPathComponent(RecordingReviewGate.processingLockFilename).path,
            O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)
        precondition(busy >= 0 && flock(busy, LOCK_EX | LOCK_NB) == 0)
        defer { close(busy) }
        let callsBeforeDeletion = await EngineProbe.shared.callCount()
        await coordinator.enqueue(blocked)
        try await Task.sleep(for: .milliseconds(100))
        try Data("deleted".utf8).write(to: blocked.appendingPathComponent(RecordingReviewGate.deletionFilename))
        flock(busy, LOCK_UN)
        try await Task.sleep(for: .milliseconds(2200))
        let callsAfterDeletion = await EngineProbe.shared.callCount()
        precondition(callsAfterDeletion == callsBeforeDeletion, "A deleted queued folder reached the model")
        precondition(!FileManager.default.fileExists(atPath: blocked.appendingPathComponent("transcribe.log").path),
            "Lease denial must not create a session error log")
        precondition(!FileManager.default.fileExists(atPath: blocked.appendingPathComponent("transcript.json").path))

        let missing = root.appendingPathComponent("missing")
        await coordinator.enqueue(missing)
        precondition(!FileManager.default.fileExists(atPath: missing.path), "Missing recording was recreated")
        try await checkCanonicalSpeakers(root: root, coordinator: coordinator)
        try await checkFailedWritesRemainPending(root: root, coordinator: coordinator)
        try await checkCaptureQuality(root: root, coordinator: coordinator)
        try await checkManualRetry(root: root.appendingPathComponent("retry-checks"))
        print("Transcription lease checks passed: held-session enrollment without approval, active/unfinished/legacy/link refusals, source labels, canonical speakers and names, missing/failed-track status, awaited model work, completed writes, active cancellation, swallowed cancellation, cleanup lease, unrelated continuation, busy deletion, tombstone rejection, no denial logs, missing folder, atomic completion ordering, retry after failed Markdown or JSON writes, capture caveats and malformed reports.")
    }

    private static func waitUntil(_ message: String, condition: @Sendable () async -> Bool) async throws {
        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline {
            if await condition() { return }
            try await Task.sleep(for: .milliseconds(10))
        }
        preconditionFailure(message)
    }

    private static func changeIntent(_ session: URL, to value: Bool?) throws {
        let url = session.appendingPathComponent("meta.json")
        var metadata = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        metadata["transcription_requested"] = value
        try JSONSerialization.data(withJSONObject: metadata).write(to: url)
    }

    private static func checkManualRetry(root: URL) async throws {
        let fm = FileManager.default
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        let recordings = root.appendingPathComponent("recordings")
        let journal = CompletionJournal(directory: root.appendingPathComponent("journal"))
        let coordinator = TranscriptionCoordinator(journal: journal)
        let statuses = StatusProbe()
        await coordinator.setStatusHandler { statuses.update($0) }
        let active = try fixture("01-active", root: recordings)

        Config.setTranscriptionEnabled(false)
        let disabled = await coordinator.retryPending(root: recordings)
        precondition(disabled == .disabled)
        precondition(!fm.fileExists(atPath: journal.directory.path), "Disabled retry touched the hook journal")
        precondition(!fm.fileExists(atPath: active.appendingPathComponent(RecordingReviewGate.processingLockFilename).path))
        Config.setTranscriptionEnabled(true)

        // Hold preparation to exercise the earliest actor suspension, before
        // the engine has emitted a transcribing status or any audio calls.
        await EngineProbe.shared.holdPreparation()
        let preparations = await EngineProbe.shared.preparationCount()
        let before = await EngineProbe.shared.callCount()
        let first = await coordinator.retryPending(root: recordings)
        precondition(first == .queued(1))
        try await waitUntil("Retry did not start preparing") {
            await EngineProbe.shared.preparationCount() > preparations
        }
        await coordinator.enqueue(active.appendingPathComponent("."))
        let repeatPreparing = await coordinator.retryPending(root: recordings)
        precondition(repeatPreparing == .queued(0), "Preparing session was queued twice")
        let receiptCommand = "printf 'once\\n' >> \"$0/receipt.txt\"; :"
        let activeID = try journal.register(session: active, command: receiptCommand, waitingForTranscript: true)
        await coordinator.enqueue(active.appendingPathComponent("."), completion: activeID)

        let queued = try fixture("02-completed-while-queued", root: recordings)
        let queuedID = try journal.register(session: queued, command: "exit 99", waitingForTranscript: true)
        let legacy = try fixture("03-registered-legacy", root: recordings)
        try changeIntent(legacy, to: nil)
        let legacyID = try journal.register(session: legacy,
            command: receiptCommand, waitingForTranscript: true)
        let delivered = try fixture("04-already-delivered", root: recordings)
        let deliveredID = try journal.register(session: delivered, command: "exit 98", waitingForTranscript: false)
        let claimed = try journal.claim()
        precondition(claimed?.id == deliveredID)
        try journal.finish(deliveredID, exitCode: 0)
        let unregistered = try fixture("old-unregistered", root: recordings)
        try changeIntent(unregistered, to: nil)
        let optedOut = try fixture("opted-out", root: recordings)
        try changeIntent(optedOut, to: false)
        _ = try journal.register(session: optedOut, command: "exit 97", waitingForTranscript: true)
        let held = try fixture("held", root: recordings)
        let start = Date()
        try RecordingReviewGate.write(.init(sessionID: UUID(), owner: .manual, phase: .finished,
            startedAt: start, checkInAt: start.addingTimeInterval(60), stopAt: start.addingTimeInterval(120), reviewStatus: .pending,
            reasons: ["Synthetic review hold"]), session: held)
        let deleted = try fixture("deleted", root: recordings)
        try Data("deleted".utf8).write(to: deleted.appendingPathComponent(RecordingReviewGate.deletionFilename))
        let laterOptOut = try fixture("05-opt-out-after-scan", root: recordings)
        let laterDeleted = try fixture("06-delete-after-scan", root: recordings)
        let laterMissing = try fixture("07-removed-after-scan", root: recordings)
        let lateRegistration = try fixture("08-registered-after-scan", root: recordings)

        let additional = await coordinator.retryPending(root: recordings)
        precondition(additional == .queued(7), "Retry included excluded sessions or missed registered legacy work")
        let lateID = try journal.register(session: lateRegistration, command: receiptCommand, waitingForTranscript: true)
        await coordinator.enqueue(lateRegistration.appendingPathComponent("."), completion: lateID)
        await coordinator.enqueue(queued.appendingPathComponent("."), completion: queuedID)
        // Start delivery recovery only after the delivered-history fixture
        // has been claimed and finished, so its setup cannot race the worker.
        await coordinator.resumePending(root: recordings)
        await coordinator.resumePending(root: recordings)
        let repeated = await coordinator.retryPending(root: recordings)
        precondition(repeated == .queued(0), "Repeated startup/manual scans duplicated queued work")
        let canonical = Data("{\"preserve\":true}".utf8)
        try canonical.write(to: queued.appendingPathComponent("transcript.json"))
        try Data("Existing readable transcript".utf8).write(to: queued.appendingPathComponent("transcript.md"))
        try changeIntent(laterOptOut, to: false)
        try Data("deleted".utf8).write(to: laterDeleted.appendingPathComponent(RecordingReviewGate.deletionFilename))
        try fm.removeItem(at: laterMissing)
        await EngineProbe.shared.holdSamples()
        await EngineProbe.shared.releasePreparation()
        try await waitUntil("Synthetic retry did not reach the model") {
            await EngineProbe.shared.callCount() > before
        }
        let during = await coordinator.retryPending(root: recordings)
        precondition(during == .queued(0), "Active transcription was queued twice")
        if case .transcribing(_, let count) = statuses.read() {
            precondition(count == 7, "Displayed queue count did not reflect pending jobs")
        } else { preconditionFailure("Transcribing status disappeared during a retry scan") }
        await EngineProbe.shared.releaseSamples()
        try await waitUntil("Retry queue did not finish") {
            if case .idle = statuses.read() {
                return FileManager.default.fileExists(atPath: lateRegistration.appendingPathComponent("transcript.json").path)
            }
            return false
        }
        let calls = await EngineProbe.shared.callCount()
        precondition(calls == before + 8, "Duplicate or excluded session reached the synthetic model")
        let canonicalAfter = try Data(contentsOf: queued.appendingPathComponent("transcript.json"))
        precondition(canonicalAfter == canonical, "Finished-after-scan transcript was overwritten")
        for session in [unregistered, optedOut, held, deleted, laterOptOut, laterDeleted, laterMissing] {
            precondition(!fm.fileExists(atPath: session.appendingPathComponent("transcript.json").path))
            precondition(!fm.fileExists(atPath: session.appendingPathComponent("transcribe.log").path))
        }
        try await waitUntil("An existing or late-registered completion was not delivered") {
            guard let jobs = try? journal.jobs() else { return false }
            return [activeID, legacyID, lateID].allSatisfy { id in
                jobs.first { $0.id == id }?.state == .delivered
            }
        }
        let jobs = try journal.jobs()
        precondition(jobs.count == 6, "Retry registered a new historical completion hook")
        precondition(jobs.first { $0.id == queuedID }?.state == .waitingForTranscript,
                     "Stale queued work released its completion hook")
        precondition(jobs.first { $0.id == deliveredID }?.attempts == 1,
                     "Retry repeated an already delivered hook")
        for session in [active, legacy, lateRegistration] {
            let receipt = try String(contentsOf: session.appendingPathComponent("receipt.txt"), encoding: .utf8)
            precondition(receipt == "once\n", "Existing waiting hook did not run exactly once")
        }

        try await checkRetryClearsFailure(root: root.appendingPathComponent("failure-check"))
        print("Manual retry checks passed: disabled without side effects, opted-in discovery, registered legacy only, held/deleted/opt-out exclusions, active/preparing/queued deduplication, stale completed output preservation, existing hooks only, active/queued late handoff registration, no delivered-hook replay, and failure cleared after successful retry behind another active job.")
    }

    private static func checkRetryClearsFailure(root: URL) async throws {
        let journal = CompletionJournal(directory: root.appendingPathComponent("journal"))
        let recordings = root.appendingPathComponent("recordings")
        let coordinator = TranscriptionCoordinator(journal: journal)
        let statuses = StatusProbe()
        await coordinator.setStatusHandler { statuses.update($0) }
        let failed = try fixture("01-failed", root: recordings)
        let obstruction = failed.appendingPathComponent("transcript.md")
        try FileManager.default.createDirectory(at: obstruction, withIntermediateDirectories: false)
        try Data("preserve".utf8).write(to: obstruction.appendingPathComponent("keep"))
        await coordinator.enqueue(failed)
        try await waitUntil("Synthetic write failure did not finish") {
            if case .failed(let session) = statuses.read() { return session == failed.lastPathComponent }
            return false
        }
        let other = try fixture("02-other-active", root: recordings)
        await EngineProbe.shared.holdSamples()
        let callsBefore = await EngineProbe.shared.callCount()
        await coordinator.enqueue(other)
        try await waitUntil("Other synthetic session did not start") {
            await EngineProbe.shared.callCount() > callsBefore
        }
        try FileManager.default.removeItem(at: obstruction)
        let queued = await coordinator.retryPending(root: recordings)
        precondition(queued == .queued(1))
        if case .transcribing(let session, let count) = statuses.read() {
            precondition(session == other.lastPathComponent && count == 1)
        } else { preconditionFailure("Retry did not preserve the active session status") }
        await EngineProbe.shared.releaseSamples()
        try await waitUntil("Successful retry left a stale failure") {
            if case .idle = statuses.read() {
                return FileManager.default.fileExists(atPath: failed.appendingPathComponent("transcript.json").path)
            }
            return false
        }
        let callsAfter = await EngineProbe.shared.callCount()
        precondition(callsAfter == callsBefore + 4)
        let jobs = try journal.jobs()
        precondition(jobs.isEmpty, "Unregistered retries acquired completion hooks")
    }

    private static func checkFailedWritesRemainPending(root: URL, coordinator: TranscriptionCoordinator) async throws {
        do {
            let blockedFile = "transcript.md"
            let session = try fixture("write-failure-\(blockedFile)", root: root)
            let obstruction = session.appendingPathComponent(blockedFile)
            try FileManager.default.createDirectory(at: obstruction, withIntermediateDirectories: false)
            let sentinel = obstruction.appendingPathComponent("keep")
            try Data("Do not replace this directory".utf8).write(to: sentinel)
            await coordinator.enqueue(session)
            let failedBy = Date().addingTimeInterval(3)
            var failed = false
            while !failed, Date() < failedBy {
                let log = (try? String(contentsOf: session.appendingPathComponent("transcribe.log"), encoding: .utf8)) ?? ""
                failed = log.contains("transcription failed:")
                if !failed { try await Task.sleep(for: .milliseconds(10)) }
            }
            precondition(failed, "A failed readable write did not report failure")
            precondition(FileManager.default.fileExists(atPath: sentinel.path))
            precondition(!FileManager.default.fileExists(atPath: session.appendingPathComponent("transcript.json").path),
                         "A failed Markdown write published the completion marker")
            precondition(TranscriptionRecovery.isPending(session: session, root: root, registeredWaiting: false),
                         "A failed Markdown write prevented retry")
            try FileManager.default.removeItem(at: obstruction)
            await coordinator.enqueue(session)
            try await waitForTranscript(session)
            precondition(!TranscriptionRecovery.isPending(session: session, root: root, registeredWaiting: false))
            let markdown = try String(contentsOf: session.appendingPathComponent("transcript.md"), encoding: .utf8)
            precondition(markdown.contains("Synthetic lease check"), "Repaired retry did not publish readable text")
        }

        // Existing canonical output intentionally blocks enqueue. Introduce a
        // JSON obstruction only after model work has started.
        let session = try fixture("json-publication-failure", root: root)
        let before = await EngineProbe.shared.callCount()
        await EngineProbe.shared.holdSamples()
        await coordinator.enqueue(session)
        let startedBy = Date().addingTimeInterval(3)
        while await EngineProbe.shared.callCount() == before, Date() < startedBy {
            try await Task.sleep(for: .milliseconds(10))
        }
        let after = await EngineProbe.shared.callCount()
        precondition(after > before)
        let obstruction = session.appendingPathComponent("transcript.json")
        try FileManager.default.createDirectory(at: obstruction, withIntermediateDirectories: false)
        try Data("Do not replace this directory".utf8).write(to: obstruction.appendingPathComponent("keep"))
        await EngineProbe.shared.releaseSamples()
        let failedBy = Date().addingTimeInterval(3)
        var failed = false
        while !failed, Date() < failedBy {
            let log = (try? String(contentsOf: session.appendingPathComponent("transcribe.log"), encoding: .utf8)) ?? ""
            failed = log.contains("transcription failed:")
            if !failed { try await Task.sleep(for: .milliseconds(10)) }
        }
        precondition(failed)
        precondition(FileManager.default.fileExists(atPath: session.appendingPathComponent("transcript.md").path))
        precondition(FileManager.default.fileExists(atPath: obstruction.appendingPathComponent("keep").path))
        try FileManager.default.removeItem(at: obstruction)
        precondition(TranscriptionRecovery.isPending(session: session, root: root, registeredWaiting: false))
        await coordinator.enqueue(session)
        try await waitForTranscript(session)
    }

    private static func checkCaptureQuality(root: URL, coordinator: TranscriptionCoordinator) async throws {
        let clean: [String: Any] = ["coverage_complete": true, "received_audio": true,
            "interruptions": [], "errors": [], "interruption_count": 0, "error_count": 0]
        let cases: [(String, [String: Any], [String])] = [
            ("legacy", [:], []),
            ("complete", ["mic_capture": clean, "system_capture": clean], []),
            ("mic-incomplete", ["mic_capture": ["coverage_complete": false]], ["mic"]),
            ("system-incomplete", ["system_capture": ["coverage_complete": false]], ["system"]),
            ("both-incomplete", ["mic_capture": ["coverage_complete": false],
                "system_capture": ["coverage_complete": false]], ["mic", "system"]),
            ("no-audio", ["system_capture": ["received_audio": false]], ["system"]),
            ("interruptions", ["mic_capture": ["coverage_complete": true,
                "interruptions": [["reason": "Private device diagnostic", "start_seconds": 1, "end_seconds": 2]]]], ["mic"]),
            ("errors", ["system_capture": ["errors": ["Private device diagnostic"]]], ["system"]),
            ("error", ["system_capture": ["error": "Private device diagnostic"]], ["system"]),
            ("counts", ["mic_capture": ["error_count": 1], "system_capture": ["interruption_count": 2]], ["mic", "system"]),
            ("wrong-bool-types", ["mic_capture": ["coverage_complete": 0, "received_audio": "false"],
                "system_capture": ["coverage_complete": "false", "received_audio": NSNull()]], []),
            ("wrong-count-types", ["mic_capture": ["error_count": true, "interruption_count": "2"],
                "system_capture": ["error_count": 0.5, "interruption_count": -1]], []),
            ("wrong-array-types", ["mic_capture": ["errors": [1, false], "interruptions": "bad"],
                "system_capture": ["errors": "bad", "interruptions": [true], "error": true]], []),
            ("empty-evidence", ["mic_capture": ["errors": ["", " "], "interruptions": [[:]]],
                "system_capture": ["error": " ", "interruptions": [["reason": false]]]], []),
            ("wrong-report-types", ["mic_capture": [false], "system_capture": "bad"], [])
        ]
        let notice = "Some audio was interrupted or could not be captured. The transcript and notes may be incomplete."
        for (name, reports, expected) in cases {
            let session = try fixture("quality-\(name)", root: root)
            let metaURL = session.appendingPathComponent("meta.json")
            var metadata = try JSONSerialization.jsonObject(with: Data(contentsOf: metaURL)) as! [String: Any]
            metadata.merge(reports) { _, new in new }
            try JSONSerialization.data(withJSONObject: metadata).write(to: metaURL)
            await coordinator.enqueue(session)
            try await waitForTranscript(session)
            let jsonURL = session.appendingPathComponent("transcript.json")
            let data = try Data(contentsOf: jsonURL)
            let transcript = try JSONSerialization.jsonObject(with: data) as! [String: Any]
            let markdown = try String(contentsOf: session.appendingPathComponent("transcript.md"), encoding: .utf8)
            if expected.isEmpty {
                precondition(transcript["capture_quality"] == nil, "Malformed/clean metadata created a caveat: \(name)")
                precondition(!markdown.contains(notice))
            } else {
                let quality = transcript["capture_quality"] as? [String: Any]
                precondition(quality?["status"] as? String == "incomplete")
                precondition(quality?["affected_sources"] as? [String] == expected)
                precondition(quality?.count == 2, "Raw recorder diagnostics leaked into the bounded contract")
                precondition(markdown.components(separatedBy: notice).count == 2, "Notice missing or repeated: \(name)")
                precondition(markdown.range(of: notice)!.lowerBound < markdown.range(of: "**[")!.lowerBound)
            }
            precondition(!String(decoding: data, as: UTF8.self).contains("Private device diagnostic"))
            precondition(!markdown.contains("Private device diagnostic"))
            precondition(transcript["speaker_labels"] as? String == "audio_source")
            let segments = transcript["segments"] as! [[String: Any]]
            precondition(Set(segments.compactMap { $0["speaker"] as? String }) == Set(["me", "them"]))
            precondition(segments.count == 2 && segments.allSatisfy { $0["text"] as? String == "Synthetic lease check" })
            if name == "both-incomplete" {
                try FileManager.default.removeItem(at: jsonURL)
                await coordinator.enqueue(session)
                try await waitForTranscript(session)
                let retry = try String(contentsOf: session.appendingPathComponent("transcript.md"), encoding: .utf8)
                precondition(retry.components(separatedBy: notice).count == 2, "Retry appended a duplicate caveat")
            }
        }
        print("Capture-quality checks passed: \(cases.count) report fixtures; no raw diagnostics; stable source schema; one notice across retries.")
    }

    private static func waitForTranscript(_ session: URL) async throws {
        let completedBy = Date().addingTimeInterval(3)
        let url = session.appendingPathComponent("transcript.json")
        while !FileManager.default.fileExists(atPath: url.path), Date() < completedBy {
            try await Task.sleep(for: .milliseconds(10))
        }
        precondition(FileManager.default.fileExists(atPath: url.path), "Transcript missing: \(session.lastPathComponent)")
    }

    private static func checkCanonicalSpeakers(root: URL, coordinator: TranscriptionCoordinator) async throws {
        for suffix in ["speakers", "speakers-unavailable", "speakers-missing-track", "speakers-skipped-track"] {
            let session = try fixture(suffix, root: root)
            if suffix == "speakers-missing-track" {
                try FileManager.default.removeItem(at: session.appendingPathComponent("system.caf"))
            }
            let metadataURL = session.appendingPathComponent("meta.json")
            var metadata = try JSONSerialization.jsonObject(with: Data(contentsOf: metadataURL)) as! [String: Any]
            metadata["speaker_identification_requested"] = true
            if suffix == "speakers" { metadata["mic_capture"] = ["coverage_complete": false] }
            try JSONSerialization.data(withJSONObject: metadata).write(to: metadataURL)
            await coordinator.enqueue(session)
            let completedBy = Date().addingTimeInterval(3)
            while !FileManager.default.fileExists(atPath: session.appendingPathComponent("transcript.json").path), Date() < completedBy {
                try await Task.sleep(for: .milliseconds(10))
            }
            let json = try JSONSerialization.jsonObject(with: Data(contentsOf: session.appendingPathComponent("transcript.json"))) as! [String: Any]
            let markdown = try String(contentsOf: session.appendingPathComponent("transcript.md"), encoding: .utf8)
            precondition(json["speaker_labels"] as? String == "diarized_v1")
            if suffix == "speakers" {
                let quality = json["capture_quality"] as? [String: Any]
                precondition(quality?["affected_sources"] as? [String] == ["mic"])
                let notice = "Some audio was interrupted or could not be captured. The transcript and notes may be incomplete."
                precondition(markdown.components(separatedBy: notice).count == 2,
                             "Capture caveat must coexist with diarization and its own partial-detection notice")
            } else { precondition(json["capture_quality"] == nil) }
            let map = json["speakers"] as! [String: [String: Any]]
            let segments = json["segments"] as! [[String: Any]]
            let status = json["speaker_identification"] as! [String: Any]
            precondition(map["unknown"] == nil)
            if ["speakers-missing-track", "speakers-skipped-track"].contains(suffix) {
                precondition(status["status"] as? String == "partial",
                             "Missing or failed tracks must not count as complete speaker detection")
                precondition(status["tracks"] as? [String: String] == ["mic": "completed", "system": "unavailable"])
                precondition(Set(map.keys) == Set(["mic_speaker_1"]))
                precondition(segments.count == 1 && segments[0]["source"] as? String == "mic")
                precondition(markdown.contains("Speaker detection partial."))
                continue
            } else if suffix == "speakers-unavailable" {
                precondition(map.isEmpty && status["status"] as? String == "unavailable")
                precondition(segments.allSatisfy { $0["speaker"] as? String == "unknown" })
                precondition(markdown.contains("Speaker detection unavailable."))
                precondition(markdown.contains("] Microphone:**") && markdown.contains("] System audio:**"))
            } else {
                precondition(Set(map.keys) == Set(["mic_speaker_1", "mic_speaker_2", "system_speaker_1", "system_speaker_2"]))
                precondition(map["mic_speaker_1"]?["name"] as? String == "Alex")
                precondition(map["system_speaker_1"]?["name"] as? String == "Max")
                precondition(map["system_speaker_2"]?["name"] == nil)
                precondition(status["status"] as? String == "partial")
                for (identity, value) in map {
                    precondition(value["source"] as? String == (identity.hasPrefix("mic_") ? "mic" : "system"))
                    if let name = value["name"] as? String {
                        precondition(value["name_source"] as? String == "self_introduction")
                        let evidence = value["evidence"] as! [Int]
                        precondition(!evidence.isEmpty && evidence.allSatisfy {
                            segments[$0]["speaker"] as? String == identity && (segments[$0]["text"] as! String).contains(name)
                        })
                    }
                }
                precondition(markdown.contains("] Alex:**") && markdown.contains("] Max:**"))
                precondition(markdown.contains("Other speaker 2") && markdown.contains("Microphone speaker 2"))
                precondition(segments.filter { $0["speaker"] as? String == "unknown" }.count == 2)
            }
            precondition(Set(segments.compactMap { $0["source"] as? String }) == Set(["mic", "system"]))
        }
    }

    private static func checkLeaseEnrollment(root: URL) throws {
        func managed(_ name: String, phase: RecordingReviewGate.Phase = .finished,
                     review: RecordingReviewGate.ReviewStatus = .pending) throws -> URL {
            let session = try fixture(name, root: root)
            let start = Date(timeIntervalSince1970: 1_792_540_800)
            let record = RecordingReviewGate.Record(sessionID: UUID(), owner: .manual, phase: phase,
                startedAt: start, checkInAt: start.addingTimeInterval(60), stopAt: start.addingTimeInterval(120),
                reviewStatus: review, reasons: ["Test recording held for review"])
            try RecordingReviewGate.write(record, session: session)
            return session
        }
        func markerExists(_ session: URL) -> Bool {
            FileManager.default.fileExists(atPath: session.appendingPathComponent(RecordingReviewGate.processingLeaseMarkerFilename).path)
        }
        func refused(_ session: URL) {
            do {
                try RecordingReviewGate.registerCompletedSessionLease(session: session)
                preconditionFailure("Unsafe session enrolled for deletion: \(session.lastPathComponent)")
            } catch {}
            precondition(!markerExists(session), "Refused enrollment published a capability marker")
        }

        for review in [RecordingReviewGate.ReviewStatus.pending, .keptLocal] {
            let held = try managed("enrollment-\(review.rawValue)", review: review)
            let before = try Data(contentsOf: held.appendingPathComponent(RecordingReviewGate.filename))
            precondition(!markerExists(held))
            try RecordingReviewGate.registerCompletedSessionLease(session: held)
            precondition(markerExists(held))
            let after = try Data(contentsOf: held.appendingPathComponent(RecordingReviewGate.filename))
            precondition(before == after, "Lease enrollment changed the processing decision")
            precondition(RecordingReviewGate.blocksProcessing(session: held))
            do {
                _ = try RecordingReviewGate.acquireProcessingLease(session: held)
                preconditionFailure("Deletion capability granted processing authorization")
            } catch RecordingReviewGate.GateError.processingHeld {}
            precondition(!FileManager.default.fileExists(atPath: held.appendingPathComponent("transcript.json").path))
        }

        let active = try managed("enrollment-active", phase: .active)
        let unfinalized = try managed("enrollment-unfinalized")
        try FileManager.default.removeItem(at: unfinalized.appendingPathComponent("meta.json"))
        let legacy = try fixture("enrollment-legacy", root: root)
        let deleted = try managed("enrollment-deleted")
        try Data("deleted".utf8).write(to: deleted.appendingPathComponent(RecordingReviewGate.deletionFilename))
        for session in [active, unfinalized, legacy, deleted] { refused(session) }

        let linkedTarget = try managed("enrollment-linked-target")
        let linked = root.appendingPathComponent("enrollment-symlink")
        try FileManager.default.createSymbolicLink(at: linked, withDestinationURL: linkedTarget)
        refused(linked)
        precondition(!markerExists(linkedTarget), "Following a directory link enrolled its target")
        let linkedMetadata = try managed("enrollment-linked-metadata")
        try FileManager.default.removeItem(at: linkedMetadata.appendingPathComponent("meta.json"))
        try FileManager.default.createSymbolicLink(at: linkedMetadata.appendingPathComponent("meta.json"),
            withDestinationURL: linkedTarget.appendingPathComponent("meta.json"))
        refused(linkedMetadata)
        let linkedJournal = try managed("enrollment-linked-journal")
        try FileManager.default.removeItem(at: linkedJournal.appendingPathComponent(RecordingReviewGate.filename))
        try FileManager.default.createSymbolicLink(at: linkedJournal.appendingPathComponent(RecordingReviewGate.filename),
            withDestinationURL: linkedTarget.appendingPathComponent(RecordingReviewGate.filename))
        refused(linkedJournal)

        let scanHeld = try managed("enrollment-scan-held")
        RecordingReviewGate.registerCompletedSessionLeases(root: root)
        precondition(markerExists(scanHeld) && RecordingReviewGate.blocksProcessing(session: scanHeld),
                     "Startup enrollment failed to preserve a held meeting")
        for session in [active, unfinalized, legacy, deleted, linkedMetadata, linkedJournal] {
            precondition(!markerExists(session), "Startup enrollment accepted an unsafe session")
        }
    }

    private static func fixture(_ name: String, root: URL) throws -> URL {
        let session = root.appendingPathComponent(name)
        try FileManager.default.createDirectory(at: session, withIntermediateDirectories: true)
        let metadata: [String: Any] = [
            "started": "2026-10-21T00:00:00Z", "ended": "2026-10-21T00:01:00Z",
            "files": ["mic": "mic.caf", "system": "system.caf"], "transcription_requested": true
        ]
        try JSONSerialization.data(withJSONObject: metadata).write(to: session.appendingPathComponent("meta.json"))
        try Data().write(to: session.appendingPathComponent("mic.caf"))
        try Data().write(to: session.appendingPathComponent("system.caf"))
        return session
    }
}
