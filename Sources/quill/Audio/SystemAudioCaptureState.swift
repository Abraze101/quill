import Foundation

enum SystemAudioCaptureStatus: Equatable, Sendable {
    case recording
    case recovering(attempt: Int)
    case failed(message: String)
}

/// Queue-owned health policy. A route event is suspicion, not evidence of
/// failure: healthy callbacks (even all-zero audio) avoid needless restarts.
struct SystemAudioRecoveryPolicy: Sendable {
    enum Phase: Equatable { case stopped, starting, awaitingAudio, recording, scheduled, failed }
    enum Action: Equatable {
        case none
        case recording
        case recover(generation: Int, attempt: Int, delay: Double, reason: String)
        case failed
    }
    struct Timing: Sendable {
        var startupGrace = 5.0
        var stale = 3.0
        var routeDebounce = 0.75
        var stability = 5.0
        var retryDelays = [0.25, 0.5, 2.0]
    }
    let timing: Timing
    private(set) var phase = Phase.stopped
    private(set) var generation = 0
    private(set) var attempts = 0
    private var startedAt = 0.0
    private var firstWrite: Double?
    private var lastWrite: Double?
    private var routeAt: Double?

    init(timing: Timing = Timing()) { self.timing = timing }

    mutating func start(at now: Double) -> Int {
        generation += 1
        attempts = 0
        phase = .starting
        startedAt = now
        return generation
    }

    mutating func started(_ generation: Int, at now: Double) {
        guard self.generation == generation, phase == .starting else { return }
        phase = .awaitingAudio
        startedAt = now
        firstWrite = nil
        lastWrite = nil
        routeAt = nil
    }

    mutating func routeChanged(at now: Double) {
        guard phase == .recording || phase == .awaitingAudio else { return }
        routeAt = now
    }

    mutating func tick(at now: Double, successfulWrite: Double?, failure: Bool = false) -> Action {
        guard phase == .recording || phase == .awaitingAudio else { return .none }
        if failure { return recover(reason: "audio_write_failed") }
        if let successfulWrite, successfulWrite >= startedAt {
            if firstWrite == nil { firstWrite = successfulWrite }
            lastWrite = successfulWrite
            if phase == .awaitingAudio, now - successfulWrite <= timing.stale {
                phase = .recording
                return .recording
            }
        }
        let stale = lastWrite.map { now - $0 > timing.stale } ?? (now - startedAt > timing.startupGrace)
        if let routeAt, now - routeAt < timing.routeDebounce, !stale { return .none }
        if stale { return recover(reason: routeAt == nil ? "system_audio_buffers_stopped" : "audio_device_changed") }
        routeAt = nil
        if let firstWrite, let lastWrite, now - firstWrite >= timing.stability,
           now - lastWrite < min(0.75, timing.stale) {
            attempts = 0
        }
        return .none
    }

    mutating func begin(_ generation: Int) -> Bool {
        guard self.generation == generation, phase == .scheduled else { return false }
        phase = .starting
        return true
    }

    mutating func restartFailed(_ generation: Int) -> Action {
        guard self.generation == generation, phase == .starting else { return .none }
        return recover(reason: "system_audio_restart_failed")
    }

    private mutating func recover(reason: String) -> Action {
        generation += 1
        firstWrite = nil
        lastWrite = nil
        routeAt = nil
        guard attempts < timing.retryDelays.count else { phase = .failed; return .failed }
        attempts += 1
        phase = .scheduled
        return .recover(generation: generation, attempt: attempts,
                        delay: timing.retryDelays[attempts - 1], reason: reason)
    }

    mutating func stop() { generation += 1; phase = .stopped }
}
