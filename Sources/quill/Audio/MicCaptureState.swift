import Foundation

/// Delivered on the main actor. A running engine is not enough: `recording`
/// is emitted only after its current generation has written an audio buffer.
enum MicCaptureStatus: Equatable, Sendable {
    case recording
    case recovering(attempt: Int)
    case failed(message: String)
}

/// Device-independent ownership and retry policy. Stop invalidates every
/// scheduled attempt and late callback; notification bursts share one attempt.
struct MicRecoveryPolicy: Sendable {
    enum Phase: Equatable { case stopped, awaitingAudio, recording, scheduled, starting, failed }
    enum Request: Equatable { case ignored, retry(generation: Int, attempt: Int), exhausted }
    private(set) var phase = Phase.stopped
    private(set) var generation = 0
    private(set) var attempts = 0
    let maxAttempts: Int

    init(maxAttempts: Int = 3) { self.maxAttempts = maxAttempts }
    var active: Bool { phase != .stopped }

    mutating func start() -> Int {
        generation += 1
        attempts = 0
        phase = .awaitingAudio
        return generation
    }

    mutating func request() -> Request {
        guard active, phase != .failed, phase != .scheduled, phase != .starting else { return .ignored }
        guard attempts < maxAttempts else {
            generation += 1
            phase = .failed
            return .exhausted
        }
        attempts += 1
        generation += 1
        phase = .scheduled
        return .retry(generation: generation, attempt: attempts)
    }

    mutating func begin(_ generation: Int) -> Bool {
        guard self.generation == generation, phase == .scheduled else { return false }
        phase = .starting
        return true
    }

    mutating func started(_ generation: Int) -> Bool {
        guard self.generation == generation, phase == .starting else { return false }
        phase = .awaitingAudio
        return true
    }

    mutating func receivedAudio(_ generation: Int) -> Bool {
        guard self.generation == generation, phase == .awaitingAudio else { return false }
        phase = .recording
        return true
    }

    mutating func confirmedStableAudio(_ generation: Int) {
        guard self.generation == generation, phase == .recording else { return }
        attempts = 0
    }

    mutating func stop() {
        generation += 1
        phase = .stopped
    }
}

/// Maps monotonic capture timestamps to one fixed-rate file. A route restart
/// keeps the same origin; silence fills unavailable intervals so later words
/// retain their real meeting times. Overlapping buffers are trimmed, not added.
struct MicCaptureTimeline: Sendable {
    struct Placement: Equatable {
        let silenceFrames: Int64
        let trimFrames: Int64
        let audioFrames: Int64
    }
    let sampleRate: Double
    private(set) var originSeconds: Double?
    private(set) var framesWritten: Int64 = 0
    private(set) var capturedFrames: Int64 = 0
    private(set) var silenceFrames: Int64 = 0

    mutating func place(startSeconds: Double, frames: Int64) -> Placement {
        precondition(startSeconds.isFinite && frames >= 0)
        if originSeconds == nil { originSeconds = startSeconds }
        let expected = max(0, Int64(((startSeconds - originSeconds!) * sampleRate).rounded()))
        // Converter latency and host-time rounding should not create repeated
        // tiny gaps. Actual interruptions exceed this 25 ms tolerance.
        let tolerance = Int64((sampleRate * 0.025).rounded())
        let difference = expected - framesWritten
        let silence = difference > tolerance ? difference : 0
        let trim = difference < -tolerance ? min(frames, -difference) : 0
        return Placement(silenceFrames: silence, trimFrames: trim, audioFrames: frames - trim)
    }

    mutating func committed(silence: Int64 = 0, audio: Int64 = 0) {
        framesWritten += silence + audio
        silenceFrames += silence
        capturedFrames += audio
    }

    var endSeconds: Double { Double(framesWritten) / sampleRate }
}
