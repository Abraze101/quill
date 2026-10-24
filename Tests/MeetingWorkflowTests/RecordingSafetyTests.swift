import Foundation
import MeetingWorkflow

func runRecordingSafetyChecks() async throws {
    let tests = RecordingSafetyTests()
    reportCheckProgress("START recording safety: testExactDeadlinesAndSingleExpiry")
    tests.testExactDeadlinesAndSingleExpiry()
    reportCheckProgress("PASS recording safety: testExactDeadlinesAndSingleExpiry")
    reportCheckProgress("START recording safety: testDelayedNineteenHourCallbackStopsImmediately")
    tests.testDelayedNineteenHourCallbackStopsImmediately()
    reportCheckProgress("PASS recording safety: testDelayedNineteenHourCallbackStopsImmediately")
    reportCheckProgress("START recording safety: testExtensionCannotUseExpiredOrStaleToken")
    tests.testExtensionCannotUseExpiredOrStaleToken()
    reportCheckProgress("PASS recording safety: testExtensionCannotUseExpiredOrStaleToken")
    reportCheckProgress("START recording safety: testClockRegressionCannotAddTime")
    tests.testClockRegressionCannotAddTime()
    reportCheckProgress("PASS recording safety: testClockRegressionCannotAddTime")
    reportCheckProgress("START recording safety: testFinishedSessionCannotWarnOrStop")
    tests.testFinishedSessionCannotWarnOrStop()
    reportCheckProgress("PASS recording safety: testFinishedSessionCannotWarnOrStop")
    reportCheckProgress("START recording safety: testActiveCrashJournalAndCleanFinalization")
    try tests.testActiveCrashJournalAndCleanFinalization()
    reportCheckProgress("PASS recording safety: testActiveCrashJournalAndCleanFinalization")
    reportCheckProgress("START recording safety: testWallClockChangesDoNotInvalidateFinalizedMedia")
    try tests.testWallClockChangesDoNotInvalidateFinalizedMedia()
    reportCheckProgress("PASS recording safety: testWallClockChangesDoNotInvalidateFinalizedMedia")
    reportCheckProgress("START recording safety: testHeldJournalRequiresExplicitReviewAndFinishedMetadata")
    try tests.testHeldJournalRequiresExplicitReviewAndFinishedMetadata()
    reportCheckProgress("PASS recording safety: testHeldJournalRequiresExplicitReviewAndFinishedMetadata")
    reportCheckProgress("START recording safety: testCorruptAndLinkedJournalsFailClosed")
    try tests.testCorruptAndLinkedJournalsFailClosed()
    reportCheckProgress("PASS recording safety: testCorruptAndLinkedJournalsFailClosed")
    reportCheckProgress("START recording safety: testAccumulatedReasonsSurviveDeadlineUpdatesAndRetry")
    try tests.testAccumulatedReasonsSurviveDeadlineUpdatesAndRetry()
    reportCheckProgress("PASS recording safety: testAccumulatedReasonsSurviveDeadlineUpdatesAndRetry")
    reportCheckProgress("START recording safety: testHeldRecordsCannotRegisterClaimOrResumeTranscription")
    try tests.testHeldRecordsCannotRegisterClaimOrResumeTranscription()
    reportCheckProgress("PASS recording safety: testHeldRecordsCannotRegisterClaimOrResumeTranscription")
    reportCheckProgress("START recording safety: testHoldBetweenClaimAndLaunchReturnsAttempt")
    try tests.testHoldBetweenClaimAndLaunchReturnsAttempt()
    reportCheckProgress("PASS recording safety: testHoldBetweenClaimAndLaunchReturnsAttempt")
    reportCheckProgress("START recording safety: testRetryRemainsHeldUntilExplicitApproval")
    try await tests.testRetryRemainsHeldUntilExplicitApproval()
    reportCheckProgress("PASS recording safety: testRetryRemainsHeldUntilExplicitApproval")
    print("13 recording safety and persistent review checks passed")
}

private final class RecordingSafetyTests {
    private let start = Date(timeIntervalSince1970: 1_800_000_000.123)
    private func safety() -> RecordingSafety {
        RecordingSafety(owner: .manual, now: 100, startedAt: start)
    }
    private func fixture(_ body: (URL) throws -> Void) throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try body(root)
    }
    private func finalized(_ dir: URL) throws {
        let metadata: [String: Any] = [
            "started": "2027-01-15T08:00:00Z", "ended": "2027-01-15T08:05:00Z",
            "files": ["mic": "mic.caf", "system": "system.caf"], "transcription_requested": true
        ]
        try JSONSerialization.data(withJSONObject: metadata).write(to: dir.appendingPathComponent("meta.json"))
    }
    private func expectThrows(_ body: () throws -> Void) {
        do { try body(); preconditionFailure("Expected a held or invalid-record error") }
        catch { /* Expected. */ }
    }

    func testExactDeadlinesAndSingleExpiry() {
        var state = safety()
        expectNil(state.tick(now: 7_299.999))
        expectEqual(state.tick(now: 7_300), .warning(state.token))
        expectNil(state.tick(now: 7_301))
        expectEqual(state.remainingResponse(now: 7_599), 1)
        expectEqual(state.tick(now: 7_600), .stop(state.token))
        expectEqual(state.stage, .expired)
        expectNil(state.tick(now: 8_000))
        expectEqual(state.gateRecord.reasons, ["unattended_timeout"])
    }

    func testDelayedNineteenHourCallbackStopsImmediately() {
        var state = safety()
        expectEqual(state.tick(now: 100 + 19 * 3_600), .stop(state.token))
        expectNil(state.tick(now: 100 + 20 * 3_600))
    }

    func testExtensionCannotUseExpiredOrStaleToken() {
        var state = safety()
        let first = state.token
        expectEqual(state.extend(token: first, now: 200), false)
        _ = state.tick(now: 7_300)
        let wrongSession = RecordingSafety.Token(sessionID: UUID(), revision: first.revision)
        expectEqual(state.extend(token: wrongSession, now: 7_400), false)
        expectEqual(state.extend(token: first, now: 7_400), true)
        expectEqual(state.revision, 2)
        expectEqual(state.checkInAt, 11_000)
        expectEqual(state.stopAt, 11_300)
        expectEqual(state.stage, .recording)
        _ = state.tick(now: 11_000)
        expectEqual(state.extend(token: first, now: 11_001), false)
        expectEqual(state.extend(token: state.token, now: 11_300), false)
        // Rejected late UI action must not swallow the pending stop event.
        expectEqual(state.tick(now: 11_300), .stop(state.token))
    }

    func testClockRegressionCannotAddTime() {
        var state = safety()
        let original = state.gateRecord
        _ = state.tick(now: 7_350)
        expectEqual(state.remainingResponse(now: -50_000), 250)
        expectEqual(state.extend(token: state.token, now: 7_000), true)
        expectEqual(state.checkInAt, 10_950)
        expectEqual(state.gateRecord.startedAt, original.startedAt)
        expectEqual(state.gateRecord.checkInAt, Date(timeIntervalSince1970: 1_800_010_850))
        expectNil(state.tick(now: .nan))
    }

    func testFinishedSessionCannotWarnOrStop() {
        var state = safety()
        state.finish(now: 200)
        expectNil(state.tick(now: 100_000))
        expectEqual(state.extend(token: state.token, now: 100_001), false)
    }

    func testActiveCrashJournalAndCleanFinalization() throws {
        try fixture { root in
            let state = safety()
            try RecordingReviewGate.write(state.gateRecord, session: root)
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), true)
            expectEqual(try RecordingReviewGate.read(session: root)?.phase, .active)
            // Repeated reads simulate a restart: an active journal is never
            // converted to clean completion by recovery.
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), true)
            try RecordingReviewGate.finish(session: root, sessionID: state.sessionID)
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), true)
            try finalized(root)
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), false)
            expectEqual(try RecordingReviewGate.read(session: root)?.reviewStatus, .approved)
        }
    }

    func testWallClockChangesDoNotInvalidateFinalizedMedia() throws {
        try fixture { root in
            let state = safety()
            try RecordingReviewGate.write(state.gateRecord, session: root)
            try RecordingReviewGate.finish(session: root, sessionID: state.sessionID)
            try finalized(root)
            let file = root.appendingPathComponent("meta.json")
            var metadata = try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as! [String: Any]
            // Changing system time during capture may make wall dates reverse.
            // The presence of finalized metadata, not that clock order, gates
            // media readiness; live safety deadlines use the monotonic clock.
            metadata["ended"] = "2027-01-15T07:00:00Z"
            try JSONSerialization.data(withJSONObject: metadata).write(to: file)
            expectEqual(RecordingReviewGate.hasFinalizedMetadata(session: root), true)
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), false)
        }
    }

    func testHeldJournalRequiresExplicitReviewAndFinishedMetadata() throws {
        try fixture { root in
            let state = safety()
            try RecordingReviewGate.write(state.gateRecord, session: root)
            expectThrows { try RecordingReviewGate.decide(session: root, sessionID: state.sessionID, status: .approved) }
            try RecordingReviewGate.finish(session: root, sessionID: state.sessionID, reasons: ["unattended_timeout"])
            try finalized(root)
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), true)
            try RecordingReviewGate.finish(session: root, sessionID: state.sessionID)
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), true)
            expectThrows { try RecordingReviewGate.decide(session: root, sessionID: UUID(), status: .approved) }
            try RecordingReviewGate.decide(session: root, sessionID: state.sessionID, status: .keptLocal)
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), true)
            try RecordingReviewGate.decide(session: root, sessionID: state.sessionID, status: .approved)
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), false)
            expectEqual(try RecordingReviewGate.read(session: root)?.reasons, ["unattended_timeout"])
        }
    }

    func testCorruptAndLinkedJournalsFailClosed() throws {
        try fixture { root in
            let journal = root.appendingPathComponent(RecordingReviewGate.filename)
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), false) // Legacy only.
            try Data("{broken".utf8).write(to: journal)
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), true)
            expectThrows { try RecordingReviewGate.write(safety().gateRecord, session: root) }
            try FileManager.default.removeItem(at: journal)
            try FileManager.default.createSymbolicLink(at: journal, withDestinationURL: root.appendingPathComponent("absent"))
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), true)
            try FileManager.default.removeItem(at: journal)
            try RecordingReviewGate.write(safety().gateRecord, session: root)
            let data = try Data(contentsOf: journal)
            var json = try JSONSerialization.jsonObject(with: data) as! [String: Any]
            json["schema_version"] = 500
            try JSONSerialization.data(withJSONObject: json).write(to: journal)
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), true)
        }
    }

    func testAccumulatedReasonsSurviveDeadlineUpdatesAndRetry() throws {
        try fixture { root in
            var state = safety()
            try RecordingReviewGate.write(state.gateRecord, session: root)
            try RecordingReviewGate.hold(session: root, sessionID: state.sessionID, reason: "microphone_failed")
            _ = state.tick(now: 7_300)
            try RecordingReviewGate.write(state.gateRecord, session: root)
            expectEqual(state.extend(token: state.token, now: 7_301), true)
            try RecordingReviewGate.write(state.gateRecord, session: root)
            expectEqual(try RecordingReviewGate.read(session: root)?.reasons, ["microphone_failed"])
            expectThrows { try RecordingReviewGate.write(safety().gateRecord, session: root) }
            try RecordingReviewGate.finish(session: root, sessionID: state.sessionID)
            try finalized(root)
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), true)
            expectThrows { try RecordingReviewGate.write(state.gateRecord, session: root) }
        }
    }

    func testHeldRecordsCannotRegisterClaimOrResumeTranscription() throws {
        try fixture { root in
            let session = root.appendingPathComponent("session", isDirectory: true)
            try FileManager.default.createDirectory(at: session, withIntermediateDirectories: true)
            try finalized(session)
            let journal = CompletionJournal(directory: root.appendingPathComponent("hooks"))
            let waiting = try journal.register(session: session, command: "true", waitingForTranscript: true)
            let ready = try journal.register(session: session, command: "echo", waitingForTranscript: false)
            let state = safety()
            try RecordingReviewGate.write(state.gateRecord, session: session)
            expectThrows { _ = try journal.register(session: session, command: "false", waitingForTranscript: false) }
            try journal.ready(waiting)
            expectNil(try journal.claim())
            expectEqual(try journal.jobs().first(where: { $0.id == waiting })?.state, .waitingForTranscript)
            expectEqual(try journal.jobs().first(where: { $0.id == ready })?.attempts, 0)
            expectEqual(TranscriptionRecovery.isPending(session: session, root: root, registeredWaiting: true), false)
            expectEqual(RecordingReviewGate.pendingSessions(root: root).map { $0.resolvingSymlinksInPath() },
                        [session.resolvingSymlinksInPath()])
            try RecordingReviewGate.decide(session: session, sessionID: state.sessionID, status: .keptLocal)
            expectEqual(RecordingReviewGate.pendingSessions(root: root), [])
            expectNil(try journal.claim())
        }
    }

    func testHoldBetweenClaimAndLaunchReturnsAttempt() throws {
        try fixture { root in
            let journal = CompletionJournal(directory: root.appendingPathComponent("hooks"))
            let id = try journal.register(session: root, command: "true", waitingForTranscript: false)
            expectEqual(try journal.claim()?.attempts, 1)
            let state = safety()
            try RecordingReviewGate.write(state.gateRecord, session: root)
            expectEqual(try journal.deferIfHeld(id), true)
            expectEqual(try journal.jobs().first?.attempts, 0)
            expectEqual(try journal.jobs().first?.state, .ready)
            expectNil(try journal.claim())
        }
    }

    func testRetryRemainsHeldUntilExplicitApproval() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let journal = CompletionJournal(directory: root.appendingPathComponent("hooks"))
        let id = try journal.register(session: root, command: "false", waitingForTranscript: false, maxAttempts: 2)
        let delivery = CompletionDelivery(journal: journal)
        _ = try await delivery.runReadyOnce(timeout: 1, retryDelay: 0)
        expectEqual(try journal.jobs().first?.state, .retry)
        let state = safety()
        try RecordingReviewGate.write(state.gateRecord, session: root)
        try RecordingReviewGate.finish(session: root, sessionID: state.sessionID, reasons: ["screen_failed"])
        try finalized(root)
        expectEqual(try await delivery.runReadyOnce(timeout: 1), false)
        expectEqual(try journal.jobs().first?.attempts, 1)
        let reopened = CompletionJournal(directory: journal.directory)
        try reopened.recover()
        expectNil(try reopened.claim())
        try RecordingReviewGate.decide(session: root, sessionID: state.sessionID, status: .approved)
        expectEqual(try reopened.claim()?.id, id)
        expectEqual(try reopened.jobs().first?.attempts, 2)
    }
}
