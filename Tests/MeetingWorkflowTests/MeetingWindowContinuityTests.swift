import Foundation
import MeetingWorkflow

final class MeetingWindowContinuityTests {
    private func signal(_ state: MeetingSignal.State, at: Double = 100, window: Int? = 7,
                        source: String = "tab-1", meeting: String = "call-1") -> MeetingSignal {
        .init(source: source, meeting: meeting, provider: "google-meet", state: state, timestamp: at,
              browserWindow: window.map { .init(id: $0, left: 0, top: 30, width: 800, height: 600) })
    }

    private func decision(_ signals: [MeetingSignal], now: Double = 105) -> MeetingWindowContinuity.Decision {
        MeetingWindowContinuity.evaluate(signals: signals, meeting: "call-1", source: "tab-1", windowID: 7, now: now)
    }

    func testUnknownWithSameWindowKeepsExistingVideo() {
        expectEqual(decision([signal(.joined), signal(.unknown, at: 101)]), .keepRecording)
        // This does not change the separate rule that unknown cannot start.
        var automation = MeetingAutomation()
        automation.enabled = true
        automation.ingest(signal(.unknown), now: 100)
        expectNil(automation.tick(now: 105))
    }

    func testExplicitLeaveStopsWithoutCaptureFailure() {
        expectEqual(decision([signal(.joined), signal(.left, at: 101, window: nil)]), .meetingLeft)
    }

    func testTabSwitchOrWindowTransferStopsVideo() {
        expectEqual(decision([signal(.joined), signal(.unknown, at: 101, window: nil)]), .windowUnavailable)
        expectEqual(decision([signal(.unknown, window: 8)]), .windowUnavailable)
        expectEqual(decision([signal(.joined, window: nil)]), .windowUnavailable)
    }

    func testMissingStaleAndFutureEvidenceCannotKeepCapture() {
        expectEqual(decision([]), .windowUnavailable)
        expectEqual(decision([signal(.joined)], now: 121), .windowUnavailable)
        expectEqual(decision([signal(.joined, at: 111)]), .windowUnavailable)
        expectEqual(decision([signal(.joined, at: .nan)]), .windowUnavailable)
    }

    func testAnotherTabOrMeetingCannotReplaceOwnedSource() {
        expectEqual(decision([signal(.joined, source: "other-tab")]), .windowUnavailable)
        expectEqual(decision([signal(.joined, meeting: "other-call")]), .windowUnavailable)
        expectEqual(decision([signal(.unknown, window: nil), signal(.joined, at: 101, source: "other-tab")]), .windowUnavailable)
    }

    func testLatestEvidenceWinsRegardlessOfReadOrder() {
        let left = signal(.left, at: 101, window: nil)
        expectEqual(decision([left, signal(.joined)]), .meetingLeft)
        expectEqual(decision([signal(.joined), left]), .meetingLeft)
    }
}
