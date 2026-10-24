import Foundation

/// Reminds the user about a confirmed call that has no capture. This policy
/// never starts a recording, infers attendance from a calendar, or stops one.
public struct MeetingRecordingWatchdog: Sendable {
    public enum CaptureState: Sendable {
        case idle, starting, recording, paused, busy
    }

    public struct Warning: Equatable, Sendable {
        public let meetingID: String
        /// True only on the first warning in this uncovered episode. The
        /// warning itself remains available on later polls for persistent UI.
        public let notify: Bool

        public init(meetingID: String, notify: Bool) {
            self.meetingID = meetingID
            self.notify = notify
        }
    }

    private struct SourceKey: Hashable, Sendable {
        let source: String
        let meeting: String
    }

    private struct Episode: Sendable {
        let uncoveredSince: TimeInterval
        var lastJoinedTimestamp: TimeInterval
        var notified = false
    }

    private let idleGrace: TimeInterval
    private let startingGrace: TimeInterval
    private let freshness: TimeInterval
    private let futureTolerance: TimeInterval
    private var episodes: [String: Episode] = [:]
    private var lastNow: TimeInterval?

    public init(idleGrace: TimeInterval = 10, startingGrace: TimeInterval = 30,
                freshness: TimeInterval = 20, futureTolerance: TimeInterval = 5) {
        self.idleGrace = idleGrace
        self.startingGrace = startingGrace
        self.freshness = freshness
        self.futureTolerance = futureTolerance
    }

    /// Use this on sleep and shutdown so a previous call cannot leave a
    /// pending warning behind. A fresh call must then pass the grace period.
    public mutating func reset() {
        episodes.removeAll()
        lastNow = nil
    }

    /// `deliberatelySkipped` includes user Skip/Stop choices, but excludes
    /// failed starts: a failed start is exactly when this reminder is useful.
    public mutating func evaluate(signals: [MeetingSignal], capture: CaptureState,
                                  deliberatelySkipped: Set<String>, now: TimeInterval) -> Warning? {
        guard now.isFinite else { reset(); return nil }
        if let lastNow, now < lastNow { reset() }
        self.lastNow = now

        switch capture {
        case .recording, .paused:
            // Any recording already captures local conversation. A deliberate
            // pause must not generate an instruction to undo the user's choice.
            episodes.removeAll()
            return nil
        case .idle, .starting, .busy:
            break
        }

        var latest: [SourceKey: MeetingSignal] = [:]
        for signal in signals {
            guard !signal.source.isEmpty, !signal.meeting.isEmpty,
                  ["google-meet", "zoom-web", "zoom-desktop"].contains(signal.provider),
                  signal.timestamp.isFinite, signal.timestamp <= now + futureTolerance,
                  now - signal.timestamp <= freshness else { continue }
            let key = SourceKey(source: signal.source, meeting: signal.meeting)
            if let previous = latest[key] {
                // Conflicting equal-time samples cannot establish attendance.
                // Prefer unknown over left over joined, independent of order.
                guard signal.timestamp > previous.timestamp ||
                        (signal.timestamp == previous.timestamp && rank(signal.state) < rank(previous.state)) else { continue }
            }
            latest[key] = signal
        }

        let byMeeting = Dictionary(grouping: latest.values, by: \.meeting)
        for meeting in Array(episodes.keys) {
            guard !deliberatelySkipped.contains(meeting), let matching = byMeeting[meeting],
                  let episode = episodes[meeting],
                  now - episode.lastJoinedTimestamp <= freshness else {
                episodes[meeting] = nil
                continue
            }
            if !matching.contains(where: { $0.state == .joined }) {
                // Unknown evidence hides the banner immediately. Brief toolbar
                // changes must not repeat a delivered notification; before the
                // first alert, however, they break the confirmation debounce.
                if !episode.notified || matching.allSatisfy({ $0.state == .left }) {
                    episodes[meeting] = nil
                }
            }
        }

        var joinedMeetings = Set<String>()
        for (meeting, matching) in byMeeting where !deliberatelySkipped.contains(meeting) {
            guard let joined = matching.filter({ $0.state == .joined }).max(by: { $0.timestamp < $1.timestamp }) else { continue }
            joinedMeetings.insert(meeting)
            if var episode = episodes[meeting] {
                episode.lastJoinedTimestamp = joined.timestamp
                episodes[meeting] = episode
            } else {
                episodes[meeting] = Episode(uncoveredSince: now, lastJoinedTimestamp: joined.timestamp)
            }
        }

        // Starting, resuming and stopping get a bounded grace period. A stuck
        // transition must not suppress the reminder forever. Failed starts that
        // return to idle retain the original uncovered time.
        let grace: TimeInterval
        switch capture {
        case .starting, .busy: grace = startingGrace
        case .idle, .recording, .paused: grace = idleGrace
        }
        let eligible = joinedMeetings.compactMap { meeting -> (meeting: String, since: TimeInterval)? in
            guard let episode = episodes[meeting], now - episode.uncoveredSince >= grace else { return nil }
            return (meeting, episode.uncoveredSince)
        }.sorted { lhs, rhs in
            return lhs.since == rhs.since ? lhs.meeting < rhs.meeting : lhs.since < rhs.since
        }
        guard let meeting = eligible.first?.meeting, var episode = episodes[meeting] else { return nil }
        let notify = !episode.notified
        episode.notified = true
        episodes[meeting] = episode
        return Warning(meetingID: meeting, notify: notify)
    }

    private func rank(_ state: MeetingSignal.State) -> Int {
        switch state {
        case .unknown: return 0
        case .left: return 1
        case .joined: return 2
        }
    }
}
