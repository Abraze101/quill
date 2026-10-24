import Foundation
import MeetingWorkflow

final class MeetingRecordingWatchdogTests {
    private func signal(_ state: MeetingSignal.State = .joined, at: Double,
                        meeting: String = "call-1", source: String = "tab-1",
                        provider: String = "google-meet") -> MeetingSignal {
        .init(source: source, meeting: meeting, provider: provider, state: state, timestamp: at)
    }

    func testWaitsForConfirmedJoinAndKeepsWarningWithoutRepeatedNotification() {
        var watchdog = MeetingRecordingWatchdog()
        expectNil(watchdog.evaluate(signals: [signal(.unknown, at: 90)], capture: .idle, deliberatelySkipped: [], now: 90))
        expectNil(watchdog.evaluate(signals: [signal(at: 100)], capture: .idle, deliberatelySkipped: [], now: 100))
        expectNil(watchdog.evaluate(signals: [signal(at: 109)], capture: .idle, deliberatelySkipped: [], now: 109))
        expectEqual(watchdog.evaluate(signals: [signal(at: 110)], capture: .idle, deliberatelySkipped: [], now: 110),
                    .init(meetingID: "call-1", notify: true))
        expectEqual(watchdog.evaluate(signals: [signal(at: 120)], capture: .idle, deliberatelySkipped: [], now: 120),
                    .init(meetingID: "call-1", notify: false))
    }

    func testRecordingPauseAndDeliberateSkipNeverNag() {
        for capture in [MeetingRecordingWatchdog.CaptureState.recording, .paused] {
            var watchdog = MeetingRecordingWatchdog()
            expectNil(watchdog.evaluate(signals: [signal(at: 100)], capture: .idle, deliberatelySkipped: [], now: 100))
            expectNil(watchdog.evaluate(signals: [signal(at: 120)], capture: capture, deliberatelySkipped: [], now: 120))
            expectNil(watchdog.evaluate(signals: [signal(at: 200)], capture: capture, deliberatelySkipped: [], now: 200))
            expectNil(watchdog.evaluate(signals: [signal(at: 201)], capture: .idle, deliberatelySkipped: [], now: 201))
            expectEqual(watchdog.evaluate(signals: [signal(at: 211)], capture: .idle, deliberatelySkipped: [], now: 211),
                        .init(meetingID: "call-1", notify: true))
        }
        var watchdog = MeetingRecordingWatchdog()
        expectNil(watchdog.evaluate(signals: [signal(at: 100)], capture: .idle, deliberatelySkipped: [], now: 100))
        expectNil(watchdog.evaluate(signals: [signal(at: 110)], capture: .idle, deliberatelySkipped: ["call-1"], now: 110))
        expectNil(watchdog.evaluate(signals: [signal(at: 500)], capture: .idle, deliberatelySkipped: ["call-1"], now: 500))
    }

    func testFailedStartsRemainEligibleUnlikeUserSkips() {
        let suppression = MeetingAutomation.Suppression(skipped: ["failed", "user-skipped"], failedStarts: ["failed"])
        let deliberatelySkipped = suppression.skipped.subtracting(suppression.failedStarts)
        var watchdog = MeetingRecordingWatchdog()
        let signals = [signal(at: 100, meeting: "failed"), signal(at: 100, meeting: "user-skipped")]
        expectNil(watchdog.evaluate(signals: signals, capture: .idle, deliberatelySkipped: deliberatelySkipped, now: 100))
        expectEqual(watchdog.evaluate(signals: signals, capture: .idle, deliberatelySkipped: deliberatelySkipped, now: 110),
                    .init(meetingID: "failed", notify: true))
    }

    func testStartingAndBusyHaveBoundedGraceAndFailureRetainsElapsedTime() {
        for capture in [MeetingRecordingWatchdog.CaptureState.starting, .busy] {
            var watchdog = MeetingRecordingWatchdog()
            expectNil(watchdog.evaluate(signals: [signal(at: 100)], capture: capture, deliberatelySkipped: [], now: 100))
            expectNil(watchdog.evaluate(signals: [signal(at: 119)], capture: capture, deliberatelySkipped: [], now: 119))
            expectNil(watchdog.evaluate(signals: [signal(at: 129)], capture: capture, deliberatelySkipped: [], now: 129))
            expectEqual(watchdog.evaluate(signals: [signal(at: 130)], capture: capture, deliberatelySkipped: [], now: 130),
                        .init(meetingID: "call-1", notify: true))
            expectEqual(watchdog.evaluate(signals: [signal(at: 131)], capture: .idle, deliberatelySkipped: [], now: 131),
                        .init(meetingID: "call-1", notify: false))
        }
        var failedStart = MeetingRecordingWatchdog()
        expectNil(failedStart.evaluate(signals: [signal(at: 100)], capture: .starting, deliberatelySkipped: [], now: 100))
        expectEqual(failedStart.evaluate(signals: [signal(at: 112)], capture: .idle, deliberatelySkipped: [], now: 112),
                    .init(meetingID: "call-1", notify: true))
    }

    func testUnknownBreaksPendingConfirmationButDoesNotRepeatDeliveredNotification() {
        var watchdog = MeetingRecordingWatchdog()
        expectNil(watchdog.evaluate(signals: [signal(at: 100)], capture: .idle, deliberatelySkipped: [], now: 100))
        expectNil(watchdog.evaluate(signals: [signal(.unknown, at: 109)], capture: .idle, deliberatelySkipped: [], now: 109))
        expectNil(watchdog.evaluate(signals: [signal(at: 110)], capture: .idle, deliberatelySkipped: [], now: 110))
        expectEqual(watchdog.evaluate(signals: [signal(at: 120)], capture: .idle, deliberatelySkipped: [], now: 120),
                    .init(meetingID: "call-1", notify: true))
        expectNil(watchdog.evaluate(signals: [signal(.unknown, at: 121)], capture: .idle, deliberatelySkipped: [], now: 121))
        expectEqual(watchdog.evaluate(signals: [signal(at: 122)], capture: .idle, deliberatelySkipped: [], now: 122),
                    .init(meetingID: "call-1", notify: false))
        expectNil(watchdog.evaluate(signals: [signal(.unknown, at: 143)], capture: .idle, deliberatelySkipped: [], now: 143))
        expectNil(watchdog.evaluate(signals: [signal(at: 144)], capture: .idle, deliberatelySkipped: [], now: 144))
    }

    func testLeaveAndStaleSignalsClearWarningAndNewCallStartsFresh() {
        for end in [signal(.left, at: 112), signal(at: 90)] {
            var watchdog = MeetingRecordingWatchdog()
            expectNil(watchdog.evaluate(signals: [signal(at: 100)], capture: .idle, deliberatelySkipped: [], now: 100))
            expectEqual(watchdog.evaluate(signals: [signal(at: 110)], capture: .idle, deliberatelySkipped: [], now: 110),
                        .init(meetingID: "call-1", notify: true))
            expectNil(watchdog.evaluate(signals: [end], capture: .idle, deliberatelySkipped: [], now: 112))
            expectNil(watchdog.evaluate(signals: [signal(at: 113)], capture: .idle, deliberatelySkipped: [], now: 113))
            expectEqual(watchdog.evaluate(signals: [signal(at: 123)], capture: .idle, deliberatelySkipped: [], now: 123),
                        .init(meetingID: "call-1", notify: true))
        }
    }

    func testLatestPerSourceWinsAndAnotherJoinedTabStillCounts() {
        for signals in [[signal(at: 100), signal(.left, at: 101)], [signal(.left, at: 101), signal(at: 100)],
                        [signal(at: 100), signal(.unknown, at: 101)], [signal(.unknown, at: 101), signal(at: 100)],
                        [signal(at: 100), signal(.left, at: 100)], [signal(.left, at: 100), signal(at: 100)]] {
            var watchdog = MeetingRecordingWatchdog()
            expectNil(watchdog.evaluate(signals: signals, capture: .idle, deliberatelySkipped: [], now: 101))
            expectNil(watchdog.evaluate(signals: signals, capture: .idle, deliberatelySkipped: [], now: 111))
        }
        var watchdog = MeetingRecordingWatchdog()
        let signals = [signal(.left, at: 100), signal(at: 100, source: "tab-2")]
        expectNil(watchdog.evaluate(signals: signals, capture: .idle, deliberatelySkipped: [], now: 100))
        expectEqual(watchdog.evaluate(signals: signals, capture: .idle, deliberatelySkipped: [], now: 110),
                    .init(meetingID: "call-1", notify: true))
    }

    func testMalformedAndUnsupportedSignalsDoNotWarn() {
        for invalid in [signal(at: .nan), signal(at: .infinity), signal(at: 106), signal(at: 79),
                        signal(at: 100, meeting: ""), signal(at: 100, source: ""), signal(at: 100, provider: "calendar")] {
            var watchdog = MeetingRecordingWatchdog(idleGrace: 0)
            expectNil(watchdog.evaluate(signals: [invalid], capture: .idle, deliberatelySkipped: [], now: 100))
        }
        var watchdog = MeetingRecordingWatchdog()
        expectNil(watchdog.evaluate(signals: [signal(at: 100)], capture: .idle, deliberatelySkipped: [], now: .nan))
    }

    func testFreshnessAndClockToleranceBoundaries() {
        var watchdog = MeetingRecordingWatchdog()
        expectNil(watchdog.evaluate(signals: [signal(at: 105)], capture: .idle, deliberatelySkipped: [], now: 100))
        expectEqual(watchdog.evaluate(signals: [signal(at: 105)], capture: .idle, deliberatelySkipped: [], now: 110),
                    .init(meetingID: "call-1", notify: true))
        expectEqual(watchdog.evaluate(signals: [signal(at: 105)], capture: .idle, deliberatelySkipped: [], now: 125),
                    .init(meetingID: "call-1", notify: false))
        expectNil(watchdog.evaluate(signals: [signal(at: 105)], capture: .idle, deliberatelySkipped: [], now: 126))
        // A fresh heartbeat after an unobserved long gap gets a new debounce.
        expectNil(watchdog.evaluate(signals: [signal(at: 200)], capture: .idle, deliberatelySkipped: [], now: 200))
        expectNil(watchdog.evaluate(signals: [signal(at: 230)], capture: .idle, deliberatelySkipped: [], now: 230))
    }

    func testIdentityCannotSwitchThePendingWarningAndOrderingIsStable() {
        var watchdog = MeetingRecordingWatchdog()
        expectNil(watchdog.evaluate(signals: [signal(at: 100, meeting: "old-call")], capture: .idle, deliberatelySkipped: [], now: 100))
        expectNil(watchdog.evaluate(signals: [signal(at: 109, meeting: "new-call")], capture: .idle, deliberatelySkipped: [], now: 109))
        expectNil(watchdog.evaluate(signals: [signal(at: 110, meeting: "new-call")], capture: .idle, deliberatelySkipped: [], now: 110))
        expectEqual(watchdog.evaluate(signals: [signal(at: 119, meeting: "new-call")], capture: .idle, deliberatelySkipped: [], now: 119),
                    .init(meetingID: "new-call", notify: true))
        for reverse in [false, true] {
            var multiple = MeetingRecordingWatchdog()
            let calls = [signal(at: 100, meeting: "b"), signal(at: 100, meeting: "a")]
            let signals = reverse ? Array(calls.reversed()) : calls
            expectNil(multiple.evaluate(signals: signals, capture: .idle, deliberatelySkipped: [], now: 100))
            expectEqual(multiple.evaluate(signals: signals, capture: .idle, deliberatelySkipped: [], now: 110),
                        .init(meetingID: "a", notify: true))
            expectEqual(multiple.evaluate(signals: signals, capture: .idle, deliberatelySkipped: ["a"], now: 111),
                        .init(meetingID: "b", notify: true))
        }
    }

    func testSleepShutdownResetAndClockReversalRequireNewConfirmation() {
        var watchdog = MeetingRecordingWatchdog()
        expectNil(watchdog.evaluate(signals: [signal(at: 100)], capture: .idle, deliberatelySkipped: [], now: 100))
        expectEqual(watchdog.evaluate(signals: [signal(at: 110)], capture: .idle, deliberatelySkipped: [], now: 110),
                    .init(meetingID: "call-1", notify: true))
        watchdog.reset()
        expectNil(watchdog.evaluate(signals: [signal(at: 111)], capture: .idle, deliberatelySkipped: [], now: 111))
        expectNil(watchdog.evaluate(signals: [signal(at: 80)], capture: .idle, deliberatelySkipped: [], now: 80))
        expectEqual(watchdog.evaluate(signals: [signal(at: 90)], capture: .idle, deliberatelySkipped: [], now: 90),
                    .init(meetingID: "call-1", notify: true))
    }

    func testReminderSkipIsBoundToOneMeetingAndConvertsOnlyItsFailedStart() {
        var automation = MeetingAutomation(skipped: ["call-1", "call-2", "old-user-skip"],
                                           failedStarts: ["call-1", "call-2"])
        automation.enabled = true
        automation.ingest(signal(at: 100), now: 100)
        automation.ingest(signal(at: 100, meeting: "call-2", source: "tab-2"), now: 100)
        expectNil(automation.skip(meeting: "call-1", now: 110))
        expectEqual(automation.suppression.skipped, ["call-1", "call-2", "old-user-skip"])
        expectEqual(automation.suppression.failedStarts, ["call-2"])
        expectNil(automation.retryFailedStart(meeting: "call-1", now: 110))
        expectEqual(automation.failedStartToRetry(now: 110), "call-2")

        var fresh = MeetingAutomation()
        fresh.ingest(signal(at: 100), now: 100)
        fresh.ingest(signal(at: 100, meeting: "call-2", source: "tab-2"), now: 100)
        expectNil(fresh.skip(meeting: "call-1", now: 110))
        expectEqual(fresh.suppression.skipped, ["call-1"])
    }

    func testReminderSkipNeverStopsManualOrOtherMeetingRecording() {
        for owner in [MeetingAutomation.Owner.manual, .meeting("other-call")] {
            var automation = MeetingAutomation()
            automation.ingest(signal(at: 100), now: 100)
            automation.didStart(id: "unrelated-recording", owner: owner)
            expectNil(automation.skip(meeting: "call-1", now: 110))
            expectEqual(automation.recording, .init(id: "unrelated-recording", owner: owner))
            expectEqual(automation.suppression.skipped, ["call-1"])
        }
        var owned = MeetingAutomation()
        owned.ingest(signal(at: 100), now: 100)
        owned.didStart(id: "owned-recording", owner: .meeting("call-1"))
        expectEqual(owned.skip(meeting: "call-1", now: 110), .stop(recording: "owned-recording"))
        expectNil(owned.skip(meeting: "call-1", now: 111))
        expectEqual(owned.recording, .init(id: "owned-recording", owner: .meeting("call-1")))
    }

    func testReminderSkipRejectsLeftUnknownStaleAndDifferentCall() {
        for state in [MeetingSignal.State.left, .unknown] {
            var automation = MeetingAutomation()
            automation.ingest(signal(at: 100), now: 100)
            automation.ingest(signal(state, at: 101), now: 101)
            expectNil(automation.skip(meeting: "call-1", now: 110))
            expectEqual(automation.suppression.skipped, [])
        }
        var automation = MeetingAutomation()
        automation.ingest(signal(at: 100), now: 100)
        expectNil(automation.skip(meeting: "different-call", now: 110))
        expectNil(automation.skip(meeting: "call-1", now: 121))
        expectEqual(automation.suppression.skipped, [])
        automation.ingest(signal(at: 130), now: 125)
        expectNil(automation.skip(meeting: "call-1", now: 124))
        expectEqual(automation.suppression.skipped, [])
    }

    func testReminderSkipOnlyClearsAfterEveryJoinedTabLeaves() {
        var automation = MeetingAutomation()
        automation.enabled = true
        automation.ingest(signal(at: 100), now: 100)
        automation.ingest(signal(at: 100, source: "tab-2"), now: 100)
        expectNil(automation.skip(meeting: "call-1", now: 102))
        automation.ingest(signal(.left, at: 103), now: 103)
        expectNil(automation.tick(now: 107))
        expectEqual(automation.suppression.skipped, ["call-1"])
        automation.ingest(signal(.left, at: 108, source: "tab-2"), now: 108)
        expectNil(automation.tick(now: 111))
        expectEqual(automation.suppression.skipped, [])
    }
}

func runMeetingRecordingWatchdogChecks() {
    let tests = MeetingRecordingWatchdogTests()
    let checks: [(String, () -> Void)] = [
        ("joined grace and persistent warning", tests.testWaitsForConfirmedJoinAndKeepsWarningWithoutRepeatedNotification),
        ("recording pause and user skip", tests.testRecordingPauseAndDeliberateSkipNeverNag),
        ("failed starts remain eligible", tests.testFailedStartsRemainEligibleUnlikeUserSkips),
        ("bounded starting and busy grace", tests.testStartingAndBusyHaveBoundedGraceAndFailureRetainsElapsedTime),
        ("unknown state debounce and notification deduplication", tests.testUnknownBreaksPendingConfirmationButDoesNotRepeatDeliveredNotification),
        ("leave and stale evidence", tests.testLeaveAndStaleSignalsClearWarningAndNewCallStartsFresh),
        ("latest source and multiple tabs", tests.testLatestPerSourceWinsAndAnotherJoinedTabStillCounts),
        ("invalid signals", tests.testMalformedAndUnsupportedSignalsDoNotWarn),
        ("freshness and clock bounds", tests.testFreshnessAndClockToleranceBoundaries),
        ("exact warning identity", tests.testIdentityCannotSwitchThePendingWarningAndOrderingIsStable),
        ("sleep shutdown and clock reset", tests.testSleepShutdownResetAndClockReversalRequireNewConfirmation),
        ("skip exact identity and failed start", tests.testReminderSkipIsBoundToOneMeetingAndConvertsOnlyItsFailedStart),
        ("skip preserves unrelated recordings", tests.testReminderSkipNeverStopsManualOrOtherMeetingRecording),
        ("skip rejects stale actions", tests.testReminderSkipRejectsLeftUnknownStaleAndDifferentCall),
        ("skip release requires every tab to leave", tests.testReminderSkipOnlyClearsAfterEveryJoinedTabLeaves)
    ]
    for (name, check) in checks { check(); print("PASS meeting watchdog \(name)") }
    print("\(checks.count) meeting recording watchdog checks passed")
}
