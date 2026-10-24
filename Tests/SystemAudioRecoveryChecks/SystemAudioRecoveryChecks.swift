import AVFoundation
import Foundation

private final class Clock: @unchecked Sendable {
    private let lock = NSLock()
    private var seconds = 100.0
    var now: Double { lock.withLock { seconds } }
    func set(_ seconds: Double) { lock.withLock { self.seconds = seconds } }
}

private enum Sample {
    static func format(_ rate: Double = 48_000) -> AVAudioFormat {
        AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 2, interleaved: false)!
    }
    static func buffer(rate: Double = 48_000, seconds: Double = 0.1, silent: Bool = false) -> AVAudioPCMBuffer {
        let buffer = AVAudioPCMBuffer(pcmFormat: format(rate), frameCapacity: AVAudioFrameCount(rate * seconds))!
        buffer.frameLength = buffer.frameCapacity
        for channel in 0..<2 {
            for i in 0..<Int(buffer.frameLength) {
                buffer.floatChannelData![channel][i] = silent ? 0 : Float(0.25 * sin(2 * .pi * Double(i) * Double(channel == 0 ? 440 : 880) / rate))
            }
        }
        return buffer
    }
}

/// Native-boundary fake: blocking start and retained old callback deliberately
/// reproduce permission races and delayed CoreAudio delivery without devices.
private final class Transport: SystemAudioCaptureTransport, @unchecked Sendable {
    private let lock = NSLock()
    private var consumer: SystemAudioBufferConsumer?
    private var changed: (@Sendable () -> Void)?
    private var didEnter = false
    private var didStop = false
    private var didStart = false
    let gate: DispatchSemaphore?
    let rate: Double
    let fail: Bool
    init(gate: DispatchSemaphore? = nil, rate: Double = 48_000, fail: Bool = false) {
        self.gate = gate; self.rate = rate; self.fail = fail
    }
    var entered: Bool { lock.withLock { didEnter } }
    var stopped: Bool { lock.withLock { didStop } }
    var started: Bool { lock.withLock { didStart } }
    func start(isCurrent: @escaping @Sendable () -> Bool, prepare: @Sendable (AVAudioFormat) throws -> SystemAudioBufferConsumer,
               routeChanged: @escaping @Sendable () -> Void) throws {
        lock.withLock { didEnter = true }
        if let gate { precondition(gate.wait(timeout: .now() + 5) == .success) }
        guard isCurrent() else { throw CancellationError() }
        if fail { throw NSError(domain: "test", code: 1) }
        let callback = try prepare(Sample.format(rate))
        lock.withLock { consumer = callback; changed = routeChanged; didStart = true }
    }
    func stop() { lock.withLock { didStop = true } }
    func emit(at seconds: Double, silent: Bool = false) {
        let callback = lock.withLock { consumer }
        callback?(Sample.buffer(rate: rate, silent: silent), seconds)
    }
    func route() { lock.withLock { changed }?() }
}

private final class Factory: @unchecked Sendable {
    private let lock = NSLock()
    private var created: [Transport] = []
    let initialGate: DispatchSemaphore?
    let recoveryGate: DispatchSemaphore?
    let failRestarts: Bool
    init(initialGate: DispatchSemaphore? = nil, recoveryGate: DispatchSemaphore? = nil, failRestarts: Bool = false) {
        self.initialGate = initialGate; self.recoveryGate = recoveryGate; self.failRestarts = failRestarts
    }
    var transports: [Transport] { lock.withLock { created } }
    func make() -> Transport {
        lock.withLock {
            let transport = Transport(gate: created.isEmpty ? initialGate : recoveryGate,
                rate: created.isEmpty ? 48_000 : 24_000, fail: !created.isEmpty && failRestarts)
            created.append(transport)
            return transport
        }
    }
}

private final class FailFirstWrite: @unchecked Sendable {
    private let lock = NSLock()
    private var shouldFail = true
    func write(_ file: AVAudioFile, _ buffer: AVAudioPCMBuffer) throws {
        let fail = lock.withLock { let result = shouldFail; shouldFail = false; return result }
        if fail { throw NSError(domain: "test", code: 9, userInfo: [NSLocalizedDescriptionKey: "Injected write failure"]) }
        try file.write(from: buffer)
    }
}

@MainActor private final class Events { var values: [SystemAudioCaptureStatus] = [] }

@main
private struct SystemAudioRecoveryChecks {
    static func policy() {
        var staleStartup = SystemAudioRecoveryPolicy()
        let staleGeneration = staleStartup.start(at: 0)
        staleStartup.started(staleGeneration, at: 0)
        guard case .recover = staleStartup.tick(at: 10, successfulWrite: 1) else {
            fatalError("A stale initial callback claimed recording")
        }
        var state = SystemAudioRecoveryPolicy()
        var generation = state.start(at: 0)
        state.started(generation, at: 0)
        precondition(state.tick(at: 5, successfulWrite: nil) == .none)
        precondition(state.tick(at: 5, successfulWrite: 5) == .recording)
        state.routeChanged(at: 5)
        for now in [5.5, 6.0, 7.0, 8.0] { precondition(state.tick(at: now, successfulWrite: now) == .none) }
        for attempt in 1...3 {
            let now = Double(attempt * 10)
            guard case .recover(let next, let count, _, _) = state.tick(at: now + 4, successfulWrite: nil) else { fatalError("Expected bounded recovery") }
            precondition(count == attempt)
            generation = next
            precondition(state.tick(at: now + 5, successfulWrite: now + 5) == .none)
            precondition(state.begin(next))
            state.started(next, at: now + 5)
            precondition(state.tick(at: now + 5, successfulWrite: now + 5) == .recording)
            precondition(state.attempts == attempt, "One callback reset retries")
        }
        precondition(state.tick(at: 40, successfulWrite: nil) == .failed)
        precondition(state.tick(at: 60, successfulWrite: 60) == .none)
        state.stop()
        precondition(!state.begin(generation))

        generation = state.start(at: 100)
        state.started(generation, at: 100)
        guard case .recover(let next, _, _, _) = state.tick(at: 106, successfulWrite: nil) else { fatalError() }
        precondition(state.begin(next)); state.started(next, at: 106)
        precondition(state.tick(at: 106, successfulWrite: 106) == .recording)
        precondition(state.tick(at: 111, successfulWrite: 111) == .none)
        precondition(state.attempts == 0)
        print("PASS health: startup grace, silent progress, route bursts, three attempts, stable reset, Stop")
    }

    static func sink(_ directory: URL) throws {
        let clock = Clock()
        let url = directory.appendingPathComponent("system.caf")
        let sink = try SystemAudioSink(url: url, sampleRate: 48_000, clock: { clock.now })
        let converter = try MicPCMConverter(input: Sample.format(), output: sink.format)
        sink.activate(generation: 1)
        sink.append(Sample.buffer(seconds: 1), hostSeconds: 100, generation: 1, converter: converter)
        let first = sink.firstBufferAt
        sink.interrupt(reason: "route_change")
        sink.recordRecovery(reason: "route_change", attempt: 1)
        let frames = sink.report["captured_frames"] as! Int64
        sink.append(Sample.buffer(), hostSeconds: 100, generation: 1, converter: converter)
        precondition(sink.report["captured_frames"] as! Int64 == frames)
        sink.activate(generation: 2)
        let replacement = try MicPCMConverter(input: Sample.format(24_000), output: sink.format)
        for index in 0..<10 {
            clock.set(102 + Double(index) / 10)
            sink.append(Sample.buffer(rate: 24_000), hostSeconds: clock.now, generation: 2, converter: replacement)
        }
        precondition(sink.firstBufferAt == first)
        precondition(sink.captureFailure == nil)
        sink.close(at: 103)
        precondition(sink.report["coverage_complete"] as? Bool == false)
        let bytes = try Data(contentsOf: url)
        sink.append(Sample.buffer(), hostSeconds: 103, generation: 2, converter: converter)
        let afterStop = try Data(contentsOf: url)
        precondition(afterStop == bytes)
        let file = try AVAudioFile(forReading: url)
        precondition(file.processingFormat.channelCount == 2 && file.processingFormat.sampleRate == 48_000)
        precondition(abs(Double(file.length) / 48_000 - 3) < 0.03)
        let audio = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length))!
        try file.read(into: audio)
        func rms(channel: Int, from: Double, to: Double) -> Double {
            let range = Int(from * 48_000)..<Int(to * 48_000)
            return sqrt(range.reduce(0.0) { $0 + pow(Double(audio.floatChannelData![channel][$1]), 2) } / Double(range.count))
        }
        for channel in 0..<2 {
            precondition(rms(channel: channel, from: 0.2, to: 0.8) > 0.1)
            precondition(rms(channel: channel, from: 1.2, to: 1.8) < 0.003)
            precondition(rms(channel: channel, from: 2.2, to: 2.8) > 0.1)
        }
        print("PASS stereo PCM: prefix/suffix retained, gap padded, rate conversion, origin stable, stale writes rejected")

        let writer = FailFirstWrite()
        let failure = try SystemAudioSink(url: directory.appendingPathComponent("failure.caf"), sampleRate: 48_000,
            clock: { clock.now }, writeBuffer: { try writer.write($0, $1) })
        failure.activate(generation: 1)
        failure.append(Sample.buffer(), hostSeconds: clock.now, generation: 1, converter: converter)
        precondition(failure.captureFailure != nil)
        clock.set(clock.now + 2)
        failure.activate(generation: 2)
        failure.append(Sample.buffer(silent: true), hostSeconds: clock.now, generation: 2, converter: converter)
        precondition(failure.hasCapturedAudio && failure.telemetry.failure == nil)
        precondition(Date().timeIntervalSince(failure.firstBufferAt!) > 1.8,
                     "Failed first write moved the file origin after padding")
        precondition(failure.captureFailure != nil && failure.report["coverage_complete"] as? Bool == false)
        for _ in 0..<500 { failure.recordRecovery(reason: String(repeating: "x", count: 20_000), attempt: 1) }
        let boundedReport = try JSONSerialization.data(withJSONObject: failure.report)
        precondition(boundedReport.count < 32_000)
        precondition(failure.report["interruption_count"] as? Int == 501)
        failure.close(at: clock.now + 0.1)
        print("PASS historical failures survive recovery; reports stay bounded under repeated events")
    }

    @MainActor static func wait(_ condition: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(4)
        while !condition() {
            precondition(Date() < deadline, "Production recorder check timed out")
            try await Task.sleep(for: .milliseconds(5))
        }
    }
    @MainActor static func recorder(clock: Clock, factory: Factory) -> SystemAudioRecorder {
        var timing = SystemAudioRecoveryPolicy.Timing()
        timing.retryDelays = [0.01, 0.01, 0.01]
        return SystemAudioRecorder(timing: timing, automaticWatchdog: false, clock: { clock.now }, makeTransport: { _ in factory.make() })
    }

    @MainActor static func production(_ directory: URL) async throws {
        let clock = Clock(), factory = Factory(), events = Events()
        let recorder = recorder(clock: clock, factory: factory)
        recorder.onStatusChange = { events.values.append($0) }
        try await recorder.start(writingTo: directory.appendingPathComponent("production.caf"))
        try await recorder.start(writingTo: directory.appendingPathComponent("must-not-create.caf"))
        precondition(factory.transports.count == 1, "Duplicate start replaced active capture")
        precondition(!FileManager.default.fileExists(atPath: directory.appendingPathComponent("must-not-create.caf").path))
        let first = factory.transports[0]
        first.emit(at: clock.now, silent: true)
        await recorder.checkHealthForTesting()
        try await wait { events.values.contains(.recording) }
        first.route(); clock.set(101); first.emit(at: 101, silent: true)
        await recorder.checkHealthForTesting()
        precondition(factory.transports.count == 1, "Route change interrupted healthy silence")
        clock.set(105)
        await recorder.checkHealthForTesting()
        try await wait { factory.transports.count == 2 && factory.transports[1].started }
        let recoveredEvents = events.values.filter { $0 == .recording }.count
        precondition(recoveredEvents == 1, "Native start alone claimed recovery")
        let before = recorder.captureReport["captured_frames"] as! Int64
        first.emit(at: 105)
        precondition(recorder.captureReport["captured_frames"] as! Int64 == before)
        clock.set(105.1); factory.transports[1].emit(at: 105.1)
        await recorder.checkHealthForTesting()
        try await wait { events.values.filter { $0 == .recording }.count == 2 }
        precondition(recorder.captureReport["coverage_complete"] as? Bool == false)
        recorder.requestStop()
        let atStop = recorder.captureReport["captured_frames"] as! Int64
        factory.transports[1].emit(at: 105.2)
        precondition(recorder.captureReport["captured_frames"] as! Int64 == atStop)
        await recorder.stop()
        do { try await recorder.start(writingTo: directory.appendingPathComponent("must-not-restart.caf")); fatalError("Stopped recorder restarted") }
        catch is CancellationError {}
        first.route(); await recorder.checkHealthForTesting()
        precondition(factory.transports.count == 2 && factory.transports.allSatisfy(\.stopped))
        print("PASS production recorder: silence healthy, stall restart, fresh-write status, stale callbacks fenced")
    }

    @MainActor static func pauseAndRecovery(_ directory: URL) async throws {
        let clock = Clock(), factory = Factory()
        let timeline = RecordingCaptureTimeline(clock: { clock.now })
        let recorder = recorder(clock: clock, factory: factory)
        recorder.captureTimeline = timeline
        try await recorder.start(writingTo: directory.appendingPathComponent("pause-recovery.caf"))
        timeline.pause(at: 100.05)
        factory.transports[0].emit(at: 100)
        let before = recorder.captureReport["captured_frames"] as! Int64
        precondition(before == 2_400, "The buffer crossing Pause was not clipped")
        // Paused callbacks keep liveness fresh, even longer than the 30-second
        // recovery-gap safety bound. None of them may advance the saved file.
        for second in 101...200 {
            clock.set(Double(second))
            factory.transports[0].emit(at: clock.now)
            await recorder.checkHealthForTesting()
        }
        precondition(factory.transports.count == 1, "Healthy paused capture exhausted the recovery budget")
        precondition(recorder.captureReport["captured_frames"] as! Int64 == before)
        // A real route outage still recovers while paused. Old-generation
        // callbacks remain fenced after recovery and after Resume.
        clock.set(205)
        factory.transports[0].route()
        await recorder.checkHealthForTesting()
        try await wait { factory.transports.count == 2 && factory.transports[1].started }
        factory.transports[1].emit(at: 205)
        await recorder.checkHealthForTesting()
        timeline.resume(at: 206)
        clock.set(206)
        factory.transports[0].emit(at: 206)
        precondition(recorder.captureReport["captured_frames"] as! Int64 == before)
        factory.transports[1].emit(at: 206)
        precondition(recorder.captureReport["captured_frames"] as! Int64 == before + 4_800)
        precondition(recorder.captureReport["silence_frames"] as! Int64 == 0,
                     "Intentional pause became a long silence/recovery gap")
        timeline.pause(at: 206.1)
        clock.set(220)
        timeline.stop()
        await recorder.stop()
        let after = recorder.captureReport["captured_frames"] as! Int64
        factory.transports[1].emit(at: 220)
        precondition(recorder.captureReport["captured_frames"] as! Int64 == after)
        print("PASS paused production recorder: long pauses stay healthy, route recovery keeps edits, stale callbacks never resume recording")
    }

    @MainActor static func blockedStarts(_ directory: URL) async throws {
        for cancelTask in [false, true] {
            let gate = DispatchSemaphore(value: 0), clock = Clock()
            let factory = Factory(initialGate: gate)
            let recorder = recorder(clock: clock, factory: factory)
            let start = Task { @MainActor in try await recorder.start(writingTo: directory.appendingPathComponent("blocked-\(cancelTask).caf")) }
            try await wait { factory.transports.first?.entered == true }
            if cancelTask { start.cancel() } else { recorder.requestStop() }
            gate.signal()
            do { try await start.value; fatalError("Stopped pending start succeeded") } catch is CancellationError {}
            await recorder.stop()
            precondition(factory.transports[0].stopped && !factory.transports[0].started)
            precondition(!recorder.hasCapturedAudio)
        }
        let gate = DispatchSemaphore(value: 0), clock = Clock(), events = Events()
        let factory = Factory(recoveryGate: gate)
        let recorder = recorder(clock: clock, factory: factory)
        recorder.onStatusChange = { events.values.append($0) }
        try await recorder.start(writingTo: directory.appendingPathComponent("blocked-recovery.caf"))
        clock.set(106); await recorder.checkHealthForTesting()
        try await wait { factory.transports.count == 2 && factory.transports[1].entered }
        recorder.requestStop()
        gate.signal()
        await recorder.stop()
        precondition(factory.transports[1].stopped && !factory.transports[1].started)
        precondition(!events.values.contains(.recording))
        print("PASS production recorder: Stop/cancellation during permission and recovery cannot restart")
    }

    @MainActor static func exhausted(_ directory: URL) async throws {
        let clock = Clock(), factory = Factory(failRestarts: true), events = Events()
        let recorder = recorder(clock: clock, factory: factory)
        recorder.onStatusChange = { events.values.append($0) }
        try await recorder.start(writingTo: directory.appendingPathComponent("exhausted.caf"))
        clock.set(106); await recorder.checkHealthForTesting()
        try await wait { events.values.contains { if case .failed = $0 { true } else { false } } }
        precondition(factory.transports.count == 4)
        precondition(recorder.captureFailure != nil)
        clock.set(200); await recorder.checkHealthForTesting()
        precondition(factory.transports.count == 4)
        await recorder.stop()
        print("PASS production recorder: initial plus exactly three failed attempts, actionable failure, no retry loop")
    }

    @MainActor static func main() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("quill-system-checks-\(UUID())")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        policy()
        try sink(directory)
        try await production(directory)
        try await pauseAndRecovery(directory)
        try await blockedStarts(directory)
        try await exhausted(directory)
        print("System audio recovery checks passed; no devices opened or permissions requested.")
    }
}
