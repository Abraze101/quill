import Foundation
import MeetingWorkflow

func expectNil<T>(_ value: T?, file: StaticString = #file, line: UInt = #line) {
    precondition(value == nil, "Expected nil; got \(String(describing: value))", file: file, line: line)
}
func expectEqual<T: Equatable>(_ actual: T, _ expected: T, file: StaticString = #file, line: UInt = #line) {
    precondition(actual == expected, "Expected \(expected); got \(actual)", file: file, line: line)
}

final class MeetingAutomationTests {
    private func signal(_ state: MeetingSignal.State, at: Double, meeting: String = "call-1", source: String = "tab-1") -> MeetingSignal {
        MeetingSignal(source: source, meeting: meeting, provider: "google-meet", state: state, timestamp: at)
    }

    func testRequiresEnabledAndStableJoinedEvidence() {
        var automation = MeetingAutomation()
        automation.ingest(signal(.joined, at: 100), now: 100)
        expectNil(automation.tick(now: 104))
        automation.enabled = true
        expectEqual(automation.tick(now: 104), .start(meeting: "call-1"))
        expectNil(automation.tick(now: 105))
    }

    func testLobbyUnknownAndStaleSignalsNeverStart() {
        var automation = MeetingAutomation()
        automation.enabled = true
        automation.ingest(signal(.unknown, at: 100), now: 100)
        expectNil(automation.tick(now: 105))
        automation.ingest(signal(.joined, at: 10), now: 100)
        expectNil(automation.tick(now: 106))
        automation.ingest(signal(.joined, at: 1000), now: 100)
        expectNil(automation.tick(now: 107))
    }

    func testOnlyExplicitStableLeaveStopsOwnedRecording() {
        var automation = MeetingAutomation()
        automation.enabled = true
        automation.didStart(id: "record-1", owner: .meeting("call-1"))
        expectNil(automation.tick(now: 100))
        expectEqual(automation.status, .signalLost)
        automation.ingest(signal(.left, at: 101), now: 101)
        expectNil(automation.tick(now: 102))
        expectEqual(automation.tick(now: 104), .stop(recording: "record-1"))
        expectNil(automation.tick(now: 105))
    }

    func testManualRecordingIsNeverStoppedOnLeaveOrSkip() {
        var automation = MeetingAutomation()
        automation.enabled = true
        automation.didStart(id: "manual", owner: .manual)
        automation.ingest(signal(.joined, at: 100), now: 100)
        expectNil(automation.skipCurrent(now: 102))
        automation.ingest(signal(.left, at: 103), now: 103)
        expectNil(automation.tick(now: 110))
        expectEqual(automation.recording?.id, "manual")
    }

    func testManualStopSuppressesAutoRestartUntilLeave() {
        var automation = MeetingAutomation()
        automation.enabled = true
        automation.ingest(signal(.joined, at: 100), now: 100)
        automation.didStart(id: "r", owner: .meeting("call-1"))
        automation.didStop(id: "r", manually: true, now: 103)
        automation.ingest(signal(.joined, at: 105), now: 105)
        expectNil(automation.tick(now: 109))
        automation.ingest(signal(.left, at: 110), now: 110)
        expectNil(automation.tick(now: 114))
        automation.ingest(signal(.joined, at: 115, meeting: "call-2"), now: 115)
        expectEqual(automation.tick(now: 118), .start(meeting: "call-2"))
    }

    func testUnknownBreaksJoinDebounceAndNeverMeansLeave() {
        var automation = MeetingAutomation()
        automation.enabled = true
        automation.ingest(signal(.joined, at: 100), now: 100)
        automation.ingest(signal(.unknown, at: 101), now: 101)
        expectNil(automation.tick(now: 105))
        automation.didStart(id: "r", owner: .meeting("call-1"))
        expectNil(automation.tick(now: 106))
        expectEqual(automation.status, .signalLost)
    }

    func testStaleAndOutOfOrderLeaveCannotStopAnotherRecording() {
        var automation = MeetingAutomation()
        automation.didStart(id: "new", owner: .meeting("call-2"))
        automation.ingest(signal(.joined, at: 100, meeting: "call-2"), now: 100)
        automation.ingest(signal(.left, at: 99, meeting: "call-1"), now: 101)
        expectNil(automation.tick(now: 105))
        automation.didStop(id: "old", manually: false, now: 106)
        expectEqual(automation.recording?.id, "new")
    }

    func testFailedStartDoesNotLoop() {
        var automation = MeetingAutomation()
        automation.enabled = true
        automation.ingest(signal(.joined, at: 100), now: 100)
        expectEqual(automation.tick(now: 103), .start(meeting: "call-1"))
        automation.startFailed(meeting: "call-1")
        expectNil(automation.tick(now: 104))
        expectEqual(automation.failedStartToRetry(now: 105), "call-1")
        expectEqual(automation.retryFailedStart(meeting: "call-1", now: 105), .start(meeting: "call-1"))
        expectNil(automation.tick(now: 106))
    }

    func testRetryKeepsExactOwnershipAndOtherSkips() {
        var automation = MeetingAutomation(skipped: ["deliberately-skipped"])
        automation.enabled = true
        automation.ingest(signal(.joined, at: 100), now: 100)
        expectEqual(automation.tick(now: 103), .start(meeting: "call-1"))
        automation.startFailed(meeting: "call-1")
        let before = automation.suppression
        expectNil(automation.retryFailedStart(meeting: "deliberately-skipped", now: 104))
        expectEqual(automation.suppression, before)
        expectEqual(automation.retryFailedStart(meeting: "call-1", now: 104), .start(meeting: "call-1"))
        expectEqual(automation.skipped, ["deliberately-skipped"])
        expectNil(automation.retryFailedStart(meeting: "call-1", now: 104))
        automation.didStart(id: "retried", owner: .meeting("call-1"))
        automation.ingest(signal(.left, at: 105, meeting: "unrelated", source: "tab-2"), now: 105)
        expectNil(automation.tick(now: 108))
        automation.ingest(signal(.left, at: 110), now: 110)
        expectEqual(automation.tick(now: 113), .stop(recording: "retried"))
        expectEqual(automation.skipped, ["deliberately-skipped"])
    }

    func testRetryNeverReplacesManualOrAutomaticRecording() {
        for owner in [MeetingAutomation.Owner.manual, .meeting("other-call")] {
            var automation = MeetingAutomation()
            automation.enabled = true
            automation.ingest(signal(.joined, at: 100), now: 100)
            automation.startFailed(meeting: "call-1")
            automation.didStart(id: "existing", owner: owner)
            let before = automation.suppression
            expectNil(automation.failedStartToRetry(now: 104))
            expectNil(automation.retryFailedStart(meeting: "call-1", now: 104))
            expectEqual(automation.recording?.id, "existing")
            expectEqual(automation.recording?.owner, owner)
            expectEqual(automation.suppression, before)
        }
    }

    func testRetryDoesNotUndoSkipOrCancellation() {
        var automation = MeetingAutomation()
        automation.enabled = true
        automation.ingest(signal(.joined, at: 100), now: 100)
        automation.startFailed(meeting: "call-1")
        expectEqual(automation.failedStartToRetry(now: 103), "call-1")
        expectNil(automation.skipCurrent(now: 103))
        expectNil(automation.failedStartToRetry(now: 104))
        expectNil(automation.retryFailedStart(meeting: "call-1", now: 104))
        expectEqual(automation.skipped, ["call-1"])

        automation.startFailed(meeting: "call-1", retryable: false)
        expectEqual(automation.suppression.failedStarts, [])
        expectNil(automation.retryFailedStart(meeting: "call-1", now: 105))
        expectNil(automation.tick(now: 105))
    }

    func testRetryRequiresEnabledFreshStableUnambiguousCall() {
        var failed = MeetingAutomation()
        failed.enabled = true
        failed.ingest(signal(.joined, at: 100), now: 100)
        failed.startFailed(meeting: "call-1")
        expectNil(failed.retryFailedStart(meeting: "call-1", now: 101))
        expectNil(failed.retryFailedStart(meeting: "call-1", now: 121))
        failed.enabled = false
        expectNil(failed.retryFailedStart(meeting: "call-1", now: 104))
        failed.enabled = true
        for state in [MeetingSignal.State.unknown, .left] {
            var changed = failed
            changed.ingest(signal(state, at: 104), now: 104)
            expectNil(changed.retryFailedStart(meeting: "call-1", now: 108))
            expectEqual(changed.skipped, ["call-1"])
        }
        failed.ingest(signal(.joined, at: 101, meeting: "call-2", source: "tab-2"), now: 101)
        failed.startFailed(meeting: "call-2")
        expectNil(failed.failedStartToRetry(now: 105))
        // No inferred candidate is safe, but a reminder carries an explicitly
        // selected meeting. Retrying it must not release the other failure.
        expectEqual(failed.retryFailedStart(meeting: "call-1", now: 105), .start(meeting: "call-1"))
        expectEqual(failed.skipped, ["call-2"])
        expectEqual(failed.suppression.failedStarts, ["call-2"])
        expectNil(failed.retryFailedStart(meeting: "call-2", now: 107))
    }

    func testSuppressionPersistenceKeepsFailuresSeparateFromLegacySkips() {
        do {
            let decoder = JSONDecoder()
            let legacy = try decoder.decode(MeetingAutomation.Suppression.self,
                                            from: Data(#"["old-skip"]"#.utf8))
            expectEqual(legacy.skipped, ["old-skip"])
            expectEqual(legacy.failedStarts, [])
            let original = MeetingAutomation.Suppression(skipped: ["old-skip", "failed"], failedStarts: ["failed", "not-skipped"])
            let restored = try decoder.decode(MeetingAutomation.Suppression.self, from: JSONEncoder().encode(original))
            expectEqual(restored, original)
            expectEqual(restored.failedStarts, ["failed"])
            var automation = MeetingAutomation(skipped: restored.skipped, failedStarts: restored.failedStarts)
            automation.enabled = true
            automation.ingest(signal(.joined, at: 100, meeting: "failed"), now: 100)
            automation.ingest(signal(.joined, at: 100, meeting: "old-skip", source: "tab-2"), now: 100)
            expectNil(automation.tick(now: 103))
            expectEqual(automation.retryFailedStart(meeting: "failed", now: 104), .start(meeting: "failed"))
            expectEqual(automation.skipped, ["old-skip"])
        } catch { preconditionFailure("Suppression persistence failed: \(error)") }
    }

    func testDisablingAutoStartDoesNotLoseOwnershipOfCurrentRecording() {
        var automation = MeetingAutomation()
        automation.enabled = false
        automation.didStart(id: "r", owner: .meeting("call-1"))
        automation.ingest(signal(.left, at: 100), now: 100)
        expectEqual(automation.tick(now: 104), .stop(recording: "r"))
    }

    func testFastRejoinPreservesLeaveOfPreviousCall() {
        var automation = MeetingAutomation()
        automation.enabled = true
        automation.didStart(id: "old", owner: .meeting("call-1"))
        automation.ingest(signal(.left, at: 100), now: 100)
        automation.ingest(signal(.joined, at: 101, meeting: "call-2"), now: 101)
        expectEqual(automation.tick(now: 104), .stop(recording: "old"))
        automation.didStop(id: "old", manually: false, now: 104)
        expectEqual(automation.tick(now: 105), .start(meeting: "call-2"))
    }

    func testSafetyStopSuppressionWaitsForEveryFreshSourceToLeave() {
        var automation = MeetingAutomation()
        automation.enabled = true
        // A closed duplicate tab keeps reporting left while the user's actual
        // meeting tab remains joined. Its old leave must not undo safety Stop.
        automation.ingest(signal(.left, at: 100, source: "closed-tab"), now: 100)
        automation.ingest(signal(.joined, at: 105, source: "meeting-tab"), now: 105)
        automation.didStart(id: "safety-stopped", owner: .meeting("call-1"))
        expectEqual(automation.skipCurrent(now: 108), .stop(recording: "safety-stopped"))
        automation.didStop(id: "safety-stopped", manually: false, now: 108)
        expectNil(automation.tick(now: 109))
        expectEqual(automation.skipped, ["call-1"])
        automation.ingest(signal(.unknown, at: 110, source: "meeting-tab"), now: 110)
        expectNil(automation.tick(now: 111))
        expectEqual(automation.skipped, ["call-1"])
        automation.ingest(signal(.left, at: 112, source: "meeting-tab"), now: 112)
        expectNil(automation.tick(now: 114))
        expectEqual(automation.skipped, ["call-1"])
        expectNil(automation.tick(now: 115))
        expectEqual(automation.skipped, [])
        automation.ingest(signal(.joined, at: 116, source: "meeting-tab"), now: 116)
        expectEqual(automation.tick(now: 119), .start(meeting: "call-1"))
    }

    func testRestoredSkipsSurviveRestartDuringSameMeeting() {
        var automation = MeetingAutomation(skipped: ["call-1"])
        automation.enabled = true
        automation.ingest(signal(.joined, at: 100), now: 100)
        expectNil(automation.tick(now: 104))
        expectEqual(automation.status, .skipped)
        automation.ingest(signal(.left, at: 105), now: 105)
        expectNil(automation.tick(now: 109))
        expectEqual(automation.skipped, [])
    }
}

@main
struct Checks {
    static func main() async throws {
        reportCheckProgress("START meeting workflow policy executable")
        runMeetingRecordingWatchdogChecks()
        runSpeakerIdentityNamesTests()
        try await runRecordingSafetyChecks()
        let tests = MeetingAutomationTests()
        let checks: [(String, () -> Void)] = [
            ("stable joined evidence", tests.testRequiresEnabledAndStableJoinedEvidence),
            ("lobby, stale and future signals", tests.testLobbyUnknownAndStaleSignalsNeverStart),
            ("owned recording leave", tests.testOnlyExplicitStableLeaveStopsOwnedRecording),
            ("manual recording ownership", tests.testManualRecordingIsNeverStoppedOnLeaveOrSkip),
            ("manual stop suppression", tests.testManualStopSuppressesAutoRestartUntilLeave),
            ("unknown is not leave", tests.testUnknownBreaksJoinDebounceAndNeverMeansLeave),
            ("out-of-order signals", tests.testStaleAndOutOfOrderLeaveCannotStopAnotherRecording),
            ("failed start suppression", tests.testFailedStartDoesNotLoop),
            ("retry exact ownership and preserved skips", tests.testRetryKeepsExactOwnershipAndOtherSkips),
            ("retry never replaces recordings", tests.testRetryNeverReplacesManualOrAutomaticRecording),
            ("retry respects skip and cancellation", tests.testRetryDoesNotUndoSkipOrCancellation),
            ("retry fresh stable unambiguous evidence", tests.testRetryRequiresEnabledFreshStableUnambiguousCall),
            ("retry suppression persistence", tests.testSuppressionPersistenceKeepsFailuresSeparateFromLegacySkips),
            ("ownership while disabled", tests.testDisablingAutoStartDoesNotLoseOwnershipOfCurrentRecording),
            ("fast rejoin", tests.testFastRejoinPreservesLeaveOfPreviousCall),
            ("restored skip", tests.testRestoredSkipsSurviveRestartDuringSameMeeting),
            ("multi-source safety-stop suppression", tests.testSafetyStopSuppressionWaitsForEveryFreshSourceToLeave)
        ]
        for (name, check) in checks { check(); print("PASS \(name)") }
        print("\(checks.count) meeting automation checks passed")
        let windows = MeetingWindowMatcherTests()
        windows.testMissingOrInvalidBrowserBoundsNeverChooseAWindow()
        windows.testOnlyTheMatchingWindowIsChosenRegardlessOfOrdering()
        windows.testAmbiguousOverlappingWindowsAreRejected()
        windows.testToleranceAppliesToEachEdgeInclusively()
        windows.testNonfiniteOrNonpositiveNativeBoundsNeverMatch()
        try windows.testOptionalWindowMetadataRemainsCodableAndBackwardsCompatible()
        print("6 meeting window identity checks passed")
        let continuity = MeetingWindowContinuityTests()
        continuity.testUnknownWithSameWindowKeepsExistingVideo()
        continuity.testExplicitLeaveStopsWithoutCaptureFailure()
        continuity.testTabSwitchOrWindowTransferStopsVideo()
        continuity.testMissingStaleAndFutureEvidenceCannotKeepCapture()
        continuity.testAnotherTabOrMeetingCannotReplaceOwnedSource()
        continuity.testLatestEvidenceWinsRegardlessOfReadOrder()
        print("6 meeting window continuity checks passed")
        let completion = CompletionDeliveryTests()
        reportCheckProgress("START completion delivery: testRegistrationIsDurableDeduplicatedAndWaitsForTranscription")
        try completion.testRegistrationIsDurableDeduplicatedAndWaitsForTranscription()
        reportCheckProgress("PASS completion delivery: testRegistrationIsDurableDeduplicatedAndWaitsForTranscription")
        reportCheckProgress("START completion delivery: testFailureBackoffAndFiniteAttempts")
        try completion.testFailureBackoffAndFiniteAttempts()
        reportCheckProgress("PASS completion delivery: testFailureBackoffAndFiniteAttempts")
        reportCheckProgress("START completion delivery: testRecoveryDoesNotRedispatchWhileAnActualChildIsLive")
        try completion.testRecoveryDoesNotRedispatchWhileAnActualChildIsLive()
        reportCheckProgress("PASS completion delivery: testRecoveryDoesNotRedispatchWhileAnActualChildIsLive")
        reportCheckProgress("START completion delivery: testNoRetryByDefaultForAnArbitraryHook")
        try completion.testNoRetryByDefaultForAnArbitraryHook()
        reportCheckProgress("PASS completion delivery: testNoRetryByDefaultForAnArbitraryHook")
        reportCheckProgress("START completion delivery: testOnlyExplicitRegistrationsAreDiscovered")
        try completion.testOnlyExplicitRegistrationsAreDiscovered()
        reportCheckProgress("PASS completion delivery: testOnlyExplicitRegistrationsAreDiscovered")
        reportCheckProgress("START completion delivery: testSubprocessPreservesSessionArgumentAndDoesNotRepeatSuccess")
        try await completion.testSubprocessPreservesSessionArgumentAndDoesNotRepeatSuccess()
        reportCheckProgress("PASS completion delivery: testSubprocessPreservesSessionArgumentAndDoesNotRepeatSuccess")
        reportCheckProgress("START completion delivery: testTransientSubprocessFailureRetriesOnlyWhenOptedIn")
        try await completion.testTransientSubprocessFailureRetriesOnlyWhenOptedIn()
        reportCheckProgress("PASS completion delivery: testTransientSubprocessFailureRetriesOnlyWhenOptedIn")
        reportCheckProgress("START completion delivery: testHungOwnedHookIsTimedOut")
        try await completion.testHungOwnedHookIsTimedOut()
        reportCheckProgress("PASS completion delivery: testHungOwnedHookIsTimedOut")
        reportCheckProgress("START completion delivery: testMissingAndDeletedSessionFoldersStayBlocked")
        try completion.testMissingAndDeletedSessionFoldersStayBlocked()
        reportCheckProgress("PASS completion delivery: testMissingAndDeletedSessionFoldersStayBlocked")
        reportCheckProgress("START completion delivery: testProcessingLeaseRejectsUnsafeLockFilesAndSessionLinks")
        try completion.testProcessingLeaseRejectsUnsafeLockFilesAndSessionLinks()
        reportCheckProgress("PASS completion delivery: testProcessingLeaseRejectsUnsafeLockFilesAndSessionLinks")
        reportCheckProgress("START completion delivery: testProcessingCapabilityMarkerIsAtomicAndStrict")
        try completion.testProcessingCapabilityMarkerIsAtomicAndStrict()
        reportCheckProgress("PASS completion delivery: testProcessingCapabilityMarkerIsAtomicAndStrict")
        reportCheckProgress("START completion delivery: testBusyDeletionLeaseDoesNotSpendCompletionAttempt")
        try await completion.testBusyDeletionLeaseDoesNotSpendCompletionAttempt()
        reportCheckProgress("PASS completion delivery: testBusyDeletionLeaseDoesNotSpendCompletionAttempt")
        reportCheckProgress("START completion delivery: testCompletionLeaseLastsThroughActualChildExit")
        try await completion.testCompletionLeaseLastsThroughActualChildExit()
        reportCheckProgress("PASS completion delivery: testCompletionLeaseLastsThroughActualChildExit")
        print("13 completion delivery and deletion-lease checks passed")
    }
}
