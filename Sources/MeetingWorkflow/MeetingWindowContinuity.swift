import Foundation

/// Continuity of an already selected capture window. Starting a recording
/// still requires stable joined evidence in MeetingAutomation.
public enum MeetingWindowContinuity {
    public enum Decision: Equatable, Sendable {
        case keepRecording, meetingLeft, windowUnavailable
    }

    public static func evaluate(signals: [MeetingSignal], meeting: String, source: String,
                                windowID: Int, now: TimeInterval) -> Decision {
        guard let latest = signals.filter({
            $0.meeting == meeting && $0.source == source && $0.timestamp.isFinite
        }).max(by: { $0.timestamp < $1.timestamp }),
              latest.timestamp <= now + 5, now - latest.timestamp <= 20 else {
            return .windowUnavailable
        }
        if latest.state == .left { return .meetingLeft }
        // An unknown meeting state can still explicitly confirm the same
        // active tab/window. Missing window evidence never authorizes capture.
        guard let window = latest.browserWindow, window.id == windowID,
              window.id > 0, window.width > 0, window.height > 0 else {
            return .windowUnavailable
        }
        return .keepRecording
    }
}
