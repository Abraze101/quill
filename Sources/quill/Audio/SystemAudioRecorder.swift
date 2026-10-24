import AVFoundation
import CoreAudio
import Foundation

/// Cross-queue Stop fence. It is deliberately independent of CoreAudio's
/// serial queue, which may be waiting in a native permission/start call.
private final class SystemAudioStopFence: @unchecked Sendable {
    private let lock = NSLock()
    private var active = false
    private var used = false
    private var generation = 0
    private var sink: SystemAudioSink?
    private var stopSeconds: Double?

    func begin(generation: Int) throws {
        try lock.withLock {
            guard !used else { throw CancellationError() }
            used = true
            active = true
            self.generation = generation
        }
    }
    func accepts(_ generation: Int) -> Bool { lock.withLock { active && self.generation == generation } }
    var isActive: Bool { lock.withLock { active } }
    var currentSink: SystemAudioSink? { lock.withLock { sink } }
    var stoppedAt: Double? { lock.withLock { stopSeconds } }
    func install(_ sink: SystemAudioSink, generation: Int) throws {
        try lock.withLock {
            guard active, self.generation == generation else { throw CancellationError() }
            self.sink = sink
            sink.activate(generation: generation)
        }
    }
    func replace(generation: Int, reason: String) -> Bool {
        lock.withLock {
            guard active else { return false }
            self.generation = generation
            sink?.interrupt(reason: reason)
            return true
        }
    }
    func stop(at now: Double) {
        lock.withLock {
            // Stop before start must also prohibit a later permission/start.
            used = true
            active = false
            if stopSeconds == nil { stopSeconds = now }
            sink?.interrupt(reason: "recording_stopped")
        }
    }
}

/// CoreAudio handles, recovery policy, timers and attempts are confined to one
/// serial control queue. Callbacks only touch the generation-owned locked sink.
/// The same system.caf and transcript origin survive every recovered route.
final class SystemAudioRecorder: @unchecked Sendable {
    enum RecorderError: Error, CustomStringConvertible {
        case tapCreationFailed(OSStatus)
        case tapFormatUnreadable(OSStatus)
        case aggregateCreationFailed(OSStatus)
        case ioProcCreationFailed(OSStatus)
        case deviceStartFailed(OSStatus)
        case fileCreationFailed(Error)

        var description: String {
            switch self {
            case .tapCreationFailed(let s):
                return "process tap creation failed (OSStatus \(s)) — check System Settings → Privacy & Security → Screen & System Audio Recording"
            case .tapFormatUnreadable(let s): return "couldn't read tap stream format (OSStatus \(s))"
            case .aggregateCreationFailed(let s): return "aggregate device creation failed (OSStatus \(s))"
            case .ioProcCreationFailed(let s): return "IO proc creation failed (OSStatus \(s))"
            case .deviceStartFailed(let s): return "device start failed (OSStatus \(s))"
            case .fileCreationFailed(let e): return "output file creation failed: \(e)"
            }
        }
    }

    @MainActor var onStatusChange: (@MainActor @Sendable (SystemAudioCaptureStatus) -> Void)?
    @MainActor private var startRequested = false
    @MainActor private var stopRequested = false
    @MainActor var captureTimeline: RecordingCaptureTimeline?
    private var timelineForCapture: RecordingCaptureTimeline?
    private let control = DispatchQueue(label: "com.digimata.quill.system-tap.control")
    private let fence = SystemAudioStopFence()
    private let clock: @Sendable () -> Double
    private let makeTransport: @Sendable (DispatchQueue) -> any SystemAudioCaptureTransport
    private let automaticWatchdog: Bool
    private var policy: SystemAudioRecoveryPolicy
    private var transport: (any SystemAudioCaptureTransport)?
    private var timer: DispatchSourceTimer?
    private var recovery: DispatchWorkItem?
    private var outputURL: URL?

    init(timing: SystemAudioRecoveryPolicy.Timing = .init(), automaticWatchdog: Bool = true,
         clock: @escaping @Sendable () -> Double = { AVAudioTime.seconds(forHostTime: mach_absolute_time()) },
         makeTransport: @escaping @Sendable (DispatchQueue) -> any SystemAudioCaptureTransport = { SystemAudioTransport(control: $0) }) {
        policy = SystemAudioRecoveryPolicy(timing: timing)
        self.automaticWatchdog = automaticWatchdog
        self.clock = clock
        self.makeTransport = makeTransport
    }

    var firstBufferAt: Date? { fence.currentSink?.firstBufferAt }
    var captureFailure: String? { fence.currentSink?.captureFailure }
    var hasCapturedAudio: Bool { fence.currentSink?.hasCapturedAudio ?? false }
    var captureReport: [String: Any] {
        fence.currentSink?.report ?? ["received_audio": false, "coverage_complete": false]
    }

    @MainActor
    func start(writingTo url: URL) async throws {
        guard !stopRequested else { throw CancellationError() }
        guard !startRequested else { return }
        startRequested = true
        var startSucceeded = false
        defer { if !startSucceeded { stopRequested = true } }
        let fence = self.fence
        let timeline = captureTimeline
        let clock = self.clock
        try await withTaskCancellationHandler {
            try Task.checkCancellation()
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                control.async { [self] in
                    do {
                        let generation = policy.start(at: clock())
                        try fence.begin(generation: generation)
                        outputURL = url
                        timelineForCapture = timeline
                        try configure(generation: generation)
                        guard fence.accepts(generation) else { throw CancellationError() }
                        policy.started(generation, at: clock())
                        startWatchdog()
                        continuation.resume()
                    } catch {
                        fence.stop(at: clock())
                        cleanup()
                        fence.currentSink?.close(at: fence.stoppedAt ?? clock())
                        continuation.resume(throwing: error)
                    }
                }
            }
        } onCancel: { fence.stop(at: clock()) }
        startSucceeded = true
    }

    /// Synchronous invalidation for Stop/sleep. Native teardown follows on its
    /// queue; queued callbacks and recovery attempts cannot revive this session.
    @MainActor
    func requestStop() { stopRequested = true; fence.stop(at: clock()) }

    @MainActor
    func stop() async {
        requestStop()
        await withCheckedContinuation { continuation in
            control.async { [self] in
                cleanup()
                fence.currentSink?.close(at: fence.stoppedAt ?? clock())
                continuation.resume()
            }
        }
    }

    private func configure(generation: Int) throws {
        guard fence.accepts(generation), let outputURL else { throw CancellationError() }
        let next = makeTransport(control)
        let timeline = timelineForCapture
        transport = next
        try next.start(isCurrent: { [fence] in fence.accepts(generation) }, prepare: { [fence, clock] input in
            guard fence.accepts(generation) else { throw CancellationError() }
            let sink: SystemAudioSink
            if let existing = fence.currentSink { sink = existing }
            else {
                guard input.sampleRate.isFinite, input.sampleRate > 0, input.channelCount > 0 else {
                    throw RecorderError.tapFormatUnreadable(-1)
                }
                do { sink = try SystemAudioSink(url: outputURL, sampleRate: input.sampleRate, clock: clock, captureTimeline: timeline) }
                catch { throw RecorderError.fileCreationFailed(error) }
            }
            let converter = try MicPCMConverter(input: input, output: sink.format)
            do { try fence.install(sink, generation: generation) }
            catch { sink.close(at: fence.stoppedAt ?? clock()); throw error }
            return { buffer, seconds in sink.append(buffer, hostSeconds: seconds, generation: generation, converter: converter) }
        }, routeChanged: { [weak self] in
            guard let self else { return }
            self.control.async { [self] in
                guard fence.accepts(generation) else { return }
                policy.routeChanged(at: clock())
            }
        })
        guard fence.accepts(generation) else { throw CancellationError() }
    }

    private func startWatchdog() {
        guard automaticWatchdog, timer == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: control)
        timer.schedule(deadline: .now() + 0.5, repeating: 0.5)
        timer.setEventHandler { [weak self] in self?.checkHealth() }
        self.timer = timer
        timer.resume()
    }

    private func checkHealth() {
        guard fence.isActive else { return }
        let telemetry = fence.currentSink?.telemetry
        handle(policy.tick(at: clock(), successfulWrite: telemetry?.lastWrite,
                           failure: telemetry?.failure != nil))
    }

    private func handle(_ action: SystemAudioRecoveryPolicy.Action) {
        switch action {
        case .none: return
        case .recording: emit(.recording, generation: policy.generation)
        case .failed:
            guard fence.replace(generation: policy.generation, reason: "recovery_exhausted") else { return }
            transport?.stop()
            transport = nil
            let message = "System audio unavailable after three recovery attempts. Microphone recording can continue."
            fence.currentSink?.fail(message)
            timer?.cancel()
            timer = nil
            emit(.failed(message: message), generation: policy.generation)
        case .recover(let generation, let attempt, let delay, let reason):
            guard fence.replace(generation: generation, reason: reason) else { return }
            fence.currentSink?.recordRecovery(reason: reason, attempt: attempt)
            emit(.recovering(attempt: attempt), generation: generation)
            recovery?.cancel()
            let work = DispatchWorkItem { [weak self] in
                guard let self, self.fence.accepts(generation), self.policy.begin(generation) else { return }
                self.transport?.stop()
                self.transport = nil
                do {
                    try self.configure(generation: generation)
                    self.policy.started(generation, at: self.clock())
                } catch {
                    self.transport?.stop()
                    self.transport = nil
                    guard self.fence.accepts(generation) else { return }
                    self.handle(self.policy.restartFailed(generation))
                }
            }
            recovery = work
            control.asyncAfter(deadline: .now() + delay, execute: work)
        }
    }

    private func emit(_ status: SystemAudioCaptureStatus, generation: Int) {
        Task { @MainActor [weak self] in
            guard let self, self.fence.accepts(generation) else { return }
            self.onStatusChange?(status)
        }
    }

    private func cleanup() {
        policy.stop()
        recovery?.cancel()
        recovery = nil
        timer?.cancel()
        timer = nil
        transport?.stop()
        transport = nil
    }

    /// Deterministic production-policy tick for isolated hardware-free checks.
    /// It follows the same serial queue as the live watchdog.
    @MainActor
    func checkHealthForTesting() async {
        await withCheckedContinuation { continuation in
            control.async { [self] in checkHealth(); continuation.resume() }
        }
    }
}
