import AVFoundation
import Foundation

/// One continuous, fixed-format mic file across input-device changes. Engine
/// control is main-actor serialized; callbacks use the locked, generation-owned
/// sink. Recovery cannot recreate the file or restart a stopped session.
@MainActor
final class MicRecorder {
    enum RecorderError: Error, CustomStringConvertible {
        case engineStartFailed(Error)
        case fileCreationFailed(Error)
        case formatUnsupported(AVAudioFormat)

        var description: String {
            switch self {
            case .engineStartFailed(let error): return "mic engine start failed: \(error)"
            case .fileCreationFailed(let error): return "mic file creation failed: \(error)"
            case .formatUnsupported(let format): return "can't capture mic format \(format)"
            }
        }
    }

    var onStatusChange: (@MainActor @Sendable (MicCaptureStatus) -> Void)?
    var captureTimeline: RecordingCaptureTimeline?
    private var engine: AVAudioEngine?
    private var observer: NSObjectProtocol?
    private var tapInstalled = false
    private var sink: MicAudioSink?
    private var policy = MicRecoveryPolicy()
    private var recoveryTask: Task<Void, Never>?
    private var watchdog: Task<Void, Never>?
    private var engineStartedSeconds = 0.0
    private var useVoiceProcessing = false
    private var recordingURL: URL?

    var isRecording: Bool { policy.active }
    var firstBufferAt: Date? { sink?.firstBufferAt }
    var captureReport: [String: Any] { sink?.report ?? ["coverage_complete": false] }

    func start(writingTo url: URL) throws {
        guard !policy.active else { return }
        recordingURL = url
        useVoiceProcessing = Config.micVoiceProcessing()
        let generation = policy.start()
        do {
            try configureEngine(generation: generation)
            watchdog = Task { @MainActor [weak self] in
                while !Task.isCancelled {
                    do { try await Task.sleep(for: .milliseconds(500)) } catch { return }
                    guard let self, self.policy.active else { return }
                    self.checkLiveness()
                }
            }
        } catch {
            stop()
            throw error
        }
    }

    func stop() {
        guard policy.active else { return }
        // Invalidate before cancelling tasks or touching native resources.
        policy.stop()
        recoveryTask?.cancel()
        recoveryTask = nil
        watchdog?.cancel()
        watchdog = nil
        sink?.interrupt(reason: "recording_stopped")
        let stoppedAt = Self.clockSeconds
        tearDownEngine()
        sink?.close(at: stoppedAt)
    }

    private static var clockSeconds: Double {
        AVAudioTime.seconds(forHostTime: mach_absolute_time())
    }

    private func configureEngine(generation: Int) throws {
        guard policy.active, policy.generation == generation else { throw CancellationError() }
        let nextEngine = AVAudioEngine()
        engine = nextEngine
        let input = nextEngine.inputNode
        var voice = useVoiceProcessing
        if voice {
            do {
                try input.setVoiceProcessingEnabled(true)
                input.voiceProcessingOtherAudioDuckingConfiguration =
                    .init(enableAdvancedDucking: false, duckingLevel: .min)
            } catch {
                log("mic voice processing unavailable (\(error)); using raw input")
                voice = false
            }
        }

        let hardwareFormat = input.outputFormat(forBus: 0)
        guard hardwareFormat.sampleRate.isFinite, hardwareFormat.sampleRate > 0,
              hardwareFormat.channelCount > 0,
              let mono = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                  sampleRate: hardwareFormat.sampleRate, channels: 1, interleaved: false) else {
            throw RecorderError.formatUnsupported(hardwareFormat)
        }

        if sink == nil {
            guard let recordingURL else { throw CancellationError() }
            do { sink = try MicAudioSink(url: recordingURL, format: mono, captureTimeline: captureTimeline) }
            catch { throw RecorderError.fileCreationFailed(error) }
        }
        guard let sink else { throw CancellationError() }

        // VoiceProcessingIO is duplex: keep a mono rendered output path at the
        // current hardware rate. Its output is converted to the file's original
        // mono format, just like raw input after a sample-rate change.
        if voice { nextEngine.connect(nextEngine.mainMixerNode, to: nextEngine.outputNode, format: mono) }
        let tapFormat = voice ? mono : hardwareFormat
        let converter = try MicPCMConverter(input: tapFormat, output: sink.format)
        sink.activate(generation: generation, voiceCheck: voice)
        input.installTap(onBus: 0, bufferSize: 4096, format: tapFormat,
            block: MicAudioCallbacks.tap(sink: sink, converter: converter, generation: generation) { [weak self] result, generation in
                self?.received(result, generation: generation)
            })
        tapInstalled = true
        nextEngine.prepare()
        // Apple's notification arrives on an internal audio queue. Never tear
        // down/deallocate the engine there; only schedule main-actor recovery.
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: nextEngine, queue: nil,
            using: MicAudioCallbacks.configurationChange(generation: generation) { [weak self] generation in
                guard let self, self.policy.generation == generation else { return }
                self.requestRecovery(reason: "audio_device_changed")
            })
        do { try nextEngine.start() }
        catch { throw RecorderError.engineStartFailed(error) }
        engineStartedSeconds = Self.clockSeconds
        log("mic: voiceProcessing=\(input.isVoiceProcessingEnabled) input=\(hardwareFormat) file=\(sink.format)")
    }

    private func tearDownEngine() {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        engine?.stop()
        if tapInstalled { engine?.inputNode.removeTap(onBus: 0) }
        tapInstalled = false
        engine = nil
    }

    private func received(_ result: MicAudioSink.Result, generation: Int) {
        guard policy.active, policy.generation == generation else { return }
        if let error = result.error {
            log(error)
            requestRecovery(reason: "audio_write_failed")
        } else if result.needsRawFallback {
            useVoiceProcessing = false
            log("voice processing delivered silence; restarting raw without discarding captured audio")
            requestRecovery(reason: "voice_processing_silence")
        } else if result.firstBuffer, policy.receivedAudio(generation) {
            onStatusChange?(.recording)
        }
    }

    private func checkLiveness() {
        guard policy.phase == .awaitingAudio || policy.phase == .recording else { return }
        let last = policy.phase == .awaitingAudio && captureTimeline?.isPaused != true ? engineStartedSeconds
            : max(sink?.lastBufferSeconds ?? engineStartedSeconds, engineStartedSeconds)
        let now = Self.clockSeconds
        if now - last > 2 {
            requestRecovery(reason: "microphone_buffers_stopped")
        } else if policy.phase == .recording && now - engineStartedSeconds >= 2 && now - last < 0.5 {
            // A lone callback from a flapping device must not replenish the
            // retry budget. Require fresh buffers after two seconds of capture.
            policy.confirmedStableAudio(policy.generation)
        }
    }

    private func requestRecovery(reason: String) {
        switch policy.request() {
        case .ignored: return
        case .exhausted:
            sink?.interrupt(reason: reason)
            tearDownEngine()
            let message = "Microphone unavailable after three recovery attempts. System audio is still recording."
            sink?.fail(message)
            onStatusChange?(.failed(message: message))
            log(message)
        case .retry(let generation, let attempt):
            sink?.interrupt(reason: reason)
            onStatusChange?(.recovering(attempt: attempt))
            log("mic recovery \(attempt)/3: \(reason)")
            recoveryTask?.cancel()
            recoveryTask = Task { @MainActor [weak self] in
                // Debounce device negotiation bursts and back off repeated
                // failures. Generation checks also defeat Stop/task races.
                do { try await Task.sleep(for: .milliseconds(250 * attempt)) } catch { return }
                guard let self, self.policy.begin(generation) else { return }
                self.tearDownEngine()
                do {
                    try self.configureEngine(generation: generation)
                    _ = self.policy.started(generation)
                } catch {
                    self.log("mic recovery failed: \(error)")
                    self.tearDownEngine()
                    _ = self.policy.started(generation)
                    self.requestRecovery(reason: "microphone_restart_failed")
                }
            }
        }
    }

    private func log(_ message: String) {
        FileHandle.standardError.write(Data((message + "\n").utf8))
    }
}
