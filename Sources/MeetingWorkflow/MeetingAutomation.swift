import Foundation

public struct MeetingSignal: Codable, Equatable, Sendable {
    public enum State: String, Codable, Sendable { case joined, left, unknown }
    /// Browser-reported outer window bounds. A browser ID is not a native
    /// window ID; capture must validate the coordinate mapping and resolve a
    /// unique matching Chrome window.
    public struct BrowserWindow: Codable, Equatable, Sendable {
        public let id: Int
        public let left: Int
        public let top: Int
        public let width: Int
        public let height: Int

        public init(id: Int, left: Int, top: Int, width: Int, height: Int) {
            self.id = id
            self.left = left
            self.top = top
            self.width = width
            self.height = height
        }
    }
    public let source: String
    public let meeting: String
    public let provider: String
    public let state: State
    public let timestamp: TimeInterval
    public let browserWindow: BrowserWindow?

    public init(source: String, meeting: String, provider: String, state: State, timestamp: TimeInterval, browserWindow: BrowserWindow? = nil) {
        self.source = source
        self.meeting = meeting
        self.provider = provider
        self.state = state
        self.timestamp = timestamp
        self.browserWindow = browserWindow
    }
}

/// Only explicit meeting evidence can start or end a recording. A missing
/// detector heartbeat is an unknown state, never evidence that a call ended.
public struct MeetingAutomation: Sendable {
    public enum Owner: Equatable, Sendable {
        case manual
        case meeting(String)
    }

    public struct Recording: Equatable, Sendable {
        public let id: String
        public let owner: Owner
        public init(id: String, owner: Owner) { self.id = id; self.owner = owner }
    }

    public enum Action: Equatable, Sendable {
        case start(meeting: String)
        case stop(recording: String)
    }

    public enum Status: Equatable, Sendable {
        case disabled
        case waiting
        case joined(String)
        case skipped
        case signalLost
    }

    /// Legacy files were an array of skipped meeting IDs. Those remain skips;
    /// only failures explicitly recorded by this version may be retried.
    public struct Suppression: Codable, Equatable, Sendable {
        public let skipped: Set<String>
        public let failedStarts: Set<String>

        public init(skipped: Set<String> = [], failedStarts: Set<String> = []) {
            self.skipped = skipped
            self.failedStarts = failedStarts.intersection(skipped)
        }

        private enum CodingKeys: String, CodingKey { case skipped, failedStarts }
        public init(from decoder: Decoder) throws {
            if let legacy = try? decoder.singleValueContainer().decode(Set<String>.self) {
                self.init(skipped: legacy)
            } else {
                let values = try decoder.container(keyedBy: CodingKeys.self)
                self.init(skipped: try values.decode(Set<String>.self, forKey: .skipped),
                          failedStarts: try values.decodeIfPresent(Set<String>.self, forKey: .failedStarts) ?? [])
            }
        }
    }

    public var enabled = false
    public private(set) var status: Status = .disabled
    public private(set) var recording: Recording?
    public private(set) var skipped: Set<String> = []
    private var failedStarts: Set<String> = []
    public var suppression: Suppression { .init(skipped: skipped, failedStarts: failedStarts) }
    private var signals: [String: MeetingSignal] = [:]
    private var joinedSince: [String: TimeInterval] = [:]
    private var leftSince: [String: TimeInterval] = [:]
    private var requestedStart: String?
    private var requestedStop: String?
    private let joinDelay: TimeInterval
    private let leaveDelay: TimeInterval
    private let freshness: TimeInterval

    public init(joinDelay: TimeInterval = 2, leaveDelay: TimeInterval = 3, freshness: TimeInterval = 20, skipped: Set<String> = [], failedStarts: Set<String> = []) {
        self.joinDelay = joinDelay
        self.leaveDelay = leaveDelay
        self.freshness = freshness
        self.skipped = skipped
        self.failedStarts = failedStarts.intersection(skipped)
    }

    public mutating func ingest(_ signal: MeetingSignal, now: TimeInterval) {
        guard !signal.source.isEmpty, !signal.meeting.isEmpty,
              ["google-meet", "zoom-web", "zoom-desktop"].contains(signal.provider),
              signal.timestamp.isFinite,
              signal.timestamp <= now + 5, now - signal.timestamp <= freshness else { return }
        let key = signal.source + "|" + signal.meeting
        if let previous = signals[key], previous.timestamp >= signal.timestamp { return }
        let previous = signals[key]
        signals[key] = signal
        if signal.state == .joined {
            if previous?.state != .joined || signal.timestamp - (previous?.timestamp ?? 0) > freshness {
                joinedSince[signal.meeting] = now
            }
            leftSince[signal.meeting] = nil
        } else if signal.state == .left {
            joinedSince[signal.meeting] = nil
            if leftSince[signal.meeting] == nil { leftSince[signal.meeting] = now }
        } else {
            joinedSince[signal.meeting] = nil
            leftSince[signal.meeting] = nil
        }
    }

    public mutating func tick(now: TimeInterval) -> Action? {
        let fresh = signals.values.filter { now - $0.timestamp <= freshness && $0.timestamp <= now + 5 }
        let joined = fresh.filter { $0.state == .joined }.sorted {
            if $0.timestamp == $1.timestamp { return $0.source < $1.source }
            return $0.timestamp < $1.timestamp
        }
        for meeting in Set(fresh.filter { $0.state == .left }.map(\.meeting)) {
            // One tab leaving does not end a meeting still joined elsewhere.
            // Keep a manual/safety-stop suppression until all fresh sources
            // explicitly agree on leave; unknown evidence cannot release it.
            let matching = fresh.filter { $0.meeting == meeting }
            if matching.allSatisfy({ $0.state == .left }),
               let since = leftSince[meeting], now - since >= leaveDelay {
                skipped.remove(meeting)
                failedStarts.remove(meeting)
            }
        }

        // A manually started recording is never owned by the detector.
        if let recording, case .meeting(let meeting) = recording.owner {
            let matching = fresh.filter { $0.meeting == meeting }
            if matching.isEmpty || matching.contains(where: { $0.state == .unknown }) {
                status = .signalLost
                return nil
            }
            if !matching.contains(where: { $0.state == .joined }),
               matching.contains(where: { $0.state == .left }),
               let since = leftSince[meeting], now - since >= leaveDelay {
                status = enabled ? .waiting : .disabled
                guard requestedStop != recording.id else { return nil }
                requestedStop = recording.id
                return .stop(recording: recording.id)
            }
            status = .joined(meeting)
            return nil
        }

        guard enabled else { status = .disabled; return nil }
        guard let candidate = joined.first(where: { !skipped.contains($0.meeting) }) else {
            status = joined.isEmpty ? .waiting : .skipped
            return nil
        }
        status = .joined(candidate.meeting)
        guard recording == nil, requestedStart == nil,
              let since = joinedSince[candidate.meeting], now - since >= joinDelay else { return nil }
        requestedStart = candidate.meeting
        return .start(meeting: candidate.meeting)
    }

    public mutating func didStart(id: String, owner: Owner) {
        recording = Recording(id: id, owner: owner)
        requestedStart = nil
        requestedStop = nil
    }

    /// Suppress a failed start until the user retries or leaves. Permission
    /// errors must not trigger a fresh prompt on every polling tick.
    public mutating func startFailed(meeting: String, retryable: Bool = true) {
        skipped.insert(meeting)
        if retryable { failedStarts.insert(meeting) }
        else { failedStarts.remove(meeting) }
        requestedStart = nil
    }

    /// Never guess between failed calls, revive a deliberate skip, or replace
    /// a recording/start already in progress. Fresh stable join evidence remains
    /// mandatory even when the user explicitly requests another attempt.
    public func failedStartToRetry(now: TimeInterval) -> String? {
        guard enabled, recording == nil, requestedStart == nil else { return nil }
        let eligible = failedStarts.filter { meeting in
            guard skipped.contains(meeting), let since = joinedSince[meeting], now - since >= joinDelay else { return false }
            let matching = signals.values.filter {
                $0.meeting == meeting && now - $0.timestamp <= freshness && $0.timestamp <= now + 5
            }
            return !matching.isEmpty && matching.allSatisfy { $0.state == .joined }
        }
        return eligible.count == 1 ? eligible.first : nil
    }

    public mutating func retryFailedStart(meeting: String, now: TimeInterval) -> Action? {
        // The caller explicitly selected this meeting. The menu's automatic
        // candidate lookup still declines ambiguous multiple failures.
        guard enabled, recording == nil, requestedStart == nil,
              failedStarts.contains(meeting), skipped.contains(meeting),
              let since = joinedSince[meeting], now - since >= joinDelay else { return nil }
        let matching = signals.values.filter {
            $0.meeting == meeting && now - $0.timestamp <= freshness && $0.timestamp <= now + 5
        }
        guard !matching.isEmpty, matching.allSatisfy({ $0.state == .joined }) else { return nil }
        skipped.remove(meeting)
        failedStarts.remove(meeting)
        requestedStart = meeting
        status = .joined(meeting)
        return .start(meeting: meeting)
    }

    public mutating func didStop(id: String, manually: Bool, now: TimeInterval) {
        guard recording?.id == id else { return }
        if manually { suppressCurrentMeetings(now: now) }
        recording = nil
        requestedStop = nil
        requestedStart = nil
    }

    /// Skip suppresses automatic recording. If the current recording is
    /// manual, leave it running; the user can use the ordinary Stop control.
    public mutating func skipCurrent(now: TimeInterval) -> Action? {
        suppressCurrentMeetings(now: now)
        status = .skipped
        guard let recording, case .meeting = recording.owner else { return nil }
        guard requestedStop != recording.id else { return nil }
        requestedStop = recording.id
        return .stop(recording: recording.id)
    }

    /// A reminder is tied to one still-joined call, not whichever call happens
    /// to be current when its button dispatches.
    public mutating func skip(meeting: String, now: TimeInterval) -> Action? {
        guard signals.values.contains(where: { $0.meeting == meeting && $0.state == .joined
            && now - $0.timestamp <= freshness && $0.timestamp <= now + 5 }) else { return nil }
        skipped.insert(meeting)
        failedStarts.remove(meeting)
        guard let recording, recording.owner == .meeting(meeting), requestedStop != recording.id else { return nil }
        requestedStop = recording.id
        return .stop(recording: recording.id)
    }

    private mutating func suppressCurrentMeetings(now: TimeInterval) {
        for signal in signals.values where signal.state != .left && now - signal.timestamp <= freshness {
            skipped.insert(signal.meeting)
            failedStarts.remove(signal.meeting)
        }
        if let recording, case .meeting(let meeting) = recording.owner {
            skipped.insert(meeting)
            failedStarts.remove(meeting)
        }
    }
}
