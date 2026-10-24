import Foundation

/// A session-specific unattended-recording deadline. The injected clock must be
/// monotonic and include sleep (for example mach_continuous_time), not wall time.
/// Delayed timer callbacks reconcile against deadlines; callbacks are not ticks
/// in a counter. This type never controls another recorder or starts capture.
public struct RecordingSafety: Sendable {
    public struct Token: Equatable, Sendable {
        public let sessionID: UUID
        public let revision: Int
        public init(sessionID: UUID, revision: Int) {
            self.sessionID = sessionID
            self.revision = revision
        }
    }

    public enum Stage: Sendable { case recording, warning, expired, finished }
    public enum Event: Equatable, Sendable { case warning(Token), stop(Token) }

    public let sessionID: UUID
    public let owner: RecordingReviewGate.Owner
    public let startedAt: Date
    public let startedMonotonic: TimeInterval
    public let responseWindow: TimeInterval
    public let extensionInterval: TimeInterval
    public private(set) var checkInAt: TimeInterval
    public private(set) var stopAt: TimeInterval
    public private(set) var revision = 1
    public private(set) var stage: Stage = .recording
    private var lastObserved: TimeInterval

    public init(sessionID: UUID = UUID(), owner: RecordingReviewGate.Owner,
                now: TimeInterval, startedAt: Date = Date(),
                checkInAfter: TimeInterval = 7_200, responseWindow: TimeInterval = 300,
                extensionInterval: TimeInterval = 3_600) {
        precondition(now.isFinite && startedAt.timeIntervalSince1970.isFinite)
        precondition(checkInAfter.isFinite && checkInAfter > 0)
        precondition(responseWindow.isFinite && responseWindow > 0)
        precondition(extensionInterval.isFinite && extensionInterval > 0)
        self.sessionID = sessionID
        self.owner = owner
        self.startedAt = startedAt
        startedMonotonic = now
        lastObserved = now
        self.responseWindow = responseWindow
        self.extensionInterval = extensionInterval
        checkInAt = now + checkInAfter
        stopAt = now + checkInAfter + responseWindow
    }

    public var token: Token { Token(sessionID: sessionID, revision: revision) }

    /// At most one stop event for this session, even after a 19-hour scheduling
    /// gap. A late callback goes straight to stop instead of granting more time.
    public mutating func tick(now: TimeInterval) -> Event? {
        guard now.isFinite, stage != .finished, stage != .expired else { return nil }
        lastObserved = max(lastObserved, now)
        if lastObserved >= stopAt {
            stage = .expired
            return .stop(token)
        }
        if lastObserved >= checkInAt, stage == .recording {
            stage = .warning
            return .warning(token)
        }
        return nil
    }

    /// Stale UI actions cannot extend another warning or another session. An
    /// action arriving at/after expiry is refused; tick still emits its stop.
    @discardableResult
    public mutating func extend(token: Token, now: TimeInterval) -> Bool {
        guard now.isFinite, token == self.token, stage == .warning else { return false }
        let observed = max(lastObserved, now)
        guard observed < stopAt else { return false }
        lastObserved = observed
        checkInAt = observed + extensionInterval
        stopAt = checkInAt + responseWindow
        revision += 1
        stage = .recording
        return true
    }

    public mutating func finish(now: TimeInterval) {
        if now.isFinite { lastObserved = max(lastObserved, now) }
        stage = .finished
    }

    public func remainingResponse(now: TimeInterval) -> TimeInterval {
        guard now.isFinite else { return 0 }
        return max(0, stopAt - max(lastObserved, now))
    }

    /// Wall-clock dates are diagnostic/persistent evidence only. They do not
    /// drive live deadlines and cannot change when the system clock is edited.
    public var gateRecord: RecordingReviewGate.Record {
        RecordingReviewGate.Record(sessionID: sessionID, owner: owner,
            startedAt: startedAt,
            checkInAt: startedAt.addingTimeInterval(checkInAt - startedMonotonic),
            stopAt: startedAt.addingTimeInterval(stopAt - startedMonotonic),
            warningRevision: revision,
            reasons: stage == .expired ? ["unattended_timeout"] : [])
    }
}
