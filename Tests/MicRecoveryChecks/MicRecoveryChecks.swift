import AVFoundation
import Foundation

@MainActor
private final class CallbackEvents {
    var audio: [MicAudioSink.Result] = []
    var audioGenerations: [Int] = []
    var changes: [Int] = []
}

private final class FirstWriteFailure: @unchecked Sendable {
    private let lock = NSLock()
    private var first = true
    func write(_ file: AVAudioFile, _ buffer: AVAudioPCMBuffer) throws {
        let fail = lock.withLock { let fail = first; first = false; return fail }
        if fail { throw NSError(domain: "QuillSyntheticWrite", code: 1) }
        try file.write(from: buffer)
    }
}

@main
struct MicRecoveryChecks {
    static func policyChecks() {
        var policy = MicRecoveryPolicy()
        let original = policy.start()
        precondition(policy.receivedAudio(original))
        guard case .retry(let replacement, let attempt) = policy.request() else { fatalError("Expected recovery") }
        precondition(attempt == 1)
        precondition(policy.request() == .ignored, "Repeated notifications duplicated recovery")
        precondition(!policy.receivedAudio(original), "Old callbacks restored a replaced route")
        precondition(policy.begin(replacement))
        precondition(policy.request() == .ignored, "Notification overtook engine configuration")
        precondition(policy.started(replacement))
        precondition(policy.receivedAudio(replacement))
        precondition(policy.attempts == 1, "A single flapping callback replenished retries")
        policy.confirmedStableAudio(replacement)
        precondition(policy.attempts == 0, "Stable audio did not reset outage retries")
        print("PASS recovery coalesces notifications and accepts only current-generation audio")

        guard case .retry(let stoppedGeneration, _) = policy.request() else { fatalError() }
        policy.stop()
        precondition(!policy.begin(stoppedGeneration))
        precondition(!policy.started(stoppedGeneration))
        precondition(!policy.receivedAudio(stoppedGeneration))
        precondition(policy.request() == .ignored)
        print("PASS Stop invalidates pending attempts, starts, notifications, and buffers")

        _ = policy.start()
        for index in 1...3 {
            guard case .retry(let generation, let attempt) = policy.request() else { fatalError() }
            precondition(attempt == index)
            precondition(policy.begin(generation))
            precondition(policy.started(generation))
        }
        precondition(policy.request() == .exhausted)
        precondition(policy.phase == .failed)
        precondition(policy.request() == .ignored)
        policy.stop()
        precondition(!policy.active)
        print("PASS one outage is bounded to three restart attempts")
    }

    static func timelineChecks() {
        var timeline = MicCaptureTimeline(sampleRate: 24_000)
        var placement = timeline.place(startSeconds: 100, frames: 24_000)
        precondition(placement.silenceFrames == 0 && placement.audioFrames == 24_000)
        timeline.committed(audio: placement.audioFrames)
        placement = timeline.place(startSeconds: 103, frames: 12_000)
        precondition(placement.silenceFrames == 48_000)
        timeline.committed(silence: placement.silenceFrames, audio: placement.audioFrames)
        precondition(timeline.endSeconds == 3.5 && timeline.capturedFrames == 36_000)
        placement = timeline.place(startSeconds: 103.25, frames: 12_000)
        precondition(placement.trimFrames == 6_000 && placement.audioFrames == 6_000)
        timeline.committed(audio: placement.audioFrames)
        precondition(timeline.endSeconds == 3.75)
        print("PASS recovery gaps retain meeting time and overlapping buffers are trimmed")
    }

    static func format(_ rate: Double, channels: AVAudioChannelCount = 1) -> AVAudioFormat {
        AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: channels, interleaved: false)!
    }

    static func tone(rate: Double, seconds: Double, frequency: Double, channels: AVAudioChannelCount = 1) -> AVAudioPCMBuffer {
        let frames = AVAudioFrameCount((rate * seconds).rounded())
        let buffer = AVAudioPCMBuffer(pcmFormat: format(rate, channels: channels), frameCapacity: frames)!
        buffer.frameLength = frames
        for channel in 0..<Int(channels) {
            for index in 0..<Int(frames) {
                buffer.floatChannelData![channel][index] = Float(0.25 * sin(2 * Double.pi * frequency * Double(index) / rate))
            }
        }
        return buffer
    }

    static func conversionChecks() throws {
        for (inputRate, outputRate) in [(24_000.0, 24_000.0), (48_000, 24_000), (24_000, 48_000), (44_100, 24_000)] {
            let converter = try MicPCMConverter(input: format(inputRate), output: format(outputRate))
            var frames = 0
            for _ in 0..<20 {
                for output in try converter.convert(tone(rate: inputRate, seconds: 0.1, frequency: 440)) {
                    precondition(output.format.sampleRate == outputRate && output.format.channelCount == 1)
                    frames += Int(output.frameLength)
                }
            }
            precondition(abs(Double(frames) / outputRate - 2) < 0.025,
                         "Converter lost or repeated samples at \(inputRate)->\(outputRate): \(frames)")
        }
        let stereo = try MicPCMConverter(input: format(48_000, channels: 2), output: format(24_000))
        let outputs = try stereo.convert(tone(rate: 48_000, seconds: 0.2, frequency: 700, channels: 2))
        let stereoFrames = outputs.reduce(0, { $0 + Int($1.frameLength) })
        precondition(abs(Double(stereoFrames) / 24_000 - 0.2) < 0.002, "Stereo converter returned \(stereoFrames) frames")
        print("PASS real converter preserves duration across 24/48/44.1 kHz and stereo-to-mono")
    }

    static func failedFirstWriteKeepsOrigin() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("quill-mic-origin-\(UUID())")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let failure = FirstWriteFailure()
        let url = directory.appendingPathComponent("mic.caf")
        let sink = try MicAudioSink(url: url, format: format(24_000), writeBuffer: failure.write)
        let origin = AVAudioTime.seconds(forHostTime: mach_absolute_time())
        let expectedDate = Date()
        func time(_ offset: Double) -> AVAudioTime { AVAudioTime(hostTime: AVAudioTime.hostTime(forSeconds: origin + offset)) }
        let firstConverter = try MicPCMConverter(input: format(24_000), output: sink.format)
        sink.activate(generation: 1, voiceCheck: false)
        let first = sink.append(tone(rate: 24_000, seconds: 0.1, frequency: 440),
            time: time(0), generation: 1, converter: firstConverter)
        precondition(first.error != nil && sink.firstBufferAt == nil)
        sink.interrupt(reason: "audio_write_failed")
        sink.activate(generation: 2, voiceCheck: false)
        let recoveryConverter = try MicPCMConverter(input: format(24_000), output: sink.format)
        let recovered = sink.append(tone(rate: 24_000, seconds: 0.1, frequency: 880),
            time: time(1), generation: 2, converter: recoveryConverter)
        precondition(recovered.error == nil && recovered.firstBuffer)
        precondition(abs(sink.firstBufferAt!.timeIntervalSince(expectedDate)) < 0.1,
                     "Failed first write shifted the source offset despite a padded recovery prefix")
        sink.close(at: origin + 1.1)
        precondition(sink.report["silence_frames"] as? Int64 == 24_000)
        precondition(sink.report["captured_frames"] as? Int64 == 2_400)
        precondition(sink.report["coverage_complete"] as? Bool == false)
        let file = try AVAudioFile(forReading: url)
        precondition(abs(Double(file.length) / file.processingFormat.sampleRate - 1.1) < 0.03)
        print("PASS failed first PCM write retains the original offset and one recovery prefix")
    }

    static func sinkChecks() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("quill-mic-recovery-\(UUID())")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("mic.caf")
        let sink = try MicAudioSink(url: url, format: format(24_000))
        let origin = AVAudioTime.seconds(forHostTime: mach_absolute_time())
        func time(_ offset: Double) -> AVAudioTime { AVAudioTime(hostTime: AVAudioTime.hostTime(forSeconds: origin + offset)) }
        let firstConverter = try MicPCMConverter(input: format(24_000), output: sink.format)
        sink.activate(generation: 1, voiceCheck: false)
        let first = sink.append(tone(rate: 24_000, seconds: 1, frequency: 440), time: time(0), generation: 1, converter: firstConverter)
        precondition(first.firstBuffer && first.error == nil)
        let firstDate = sink.firstBufferAt
        sink.interrupt(reason: "audio_device_changed")
        let ignored = sink.append(tone(rate: 24_000, seconds: 1, frequency: 440), time: time(1), generation: 1, converter: firstConverter)
        precondition(!ignored.firstBuffer && ignored.error == nil)
        sink.activate(generation: 2, voiceCheck: false)
        let secondConverter = try MicPCMConverter(input: format(48_000), output: sink.format)
        for index in 0..<10 {
            let resumed = sink.append(tone(rate: 48_000, seconds: 0.1, frequency: 880),
                time: time(2 + Double(index) / 10), generation: 2, converter: secondConverter)
            precondition(resumed.error == nil)
        }
        precondition(sink.firstBufferAt == firstDate, "Recovery moved the transcript origin")
        sink.close(at: origin + 3)
        let report = sink.report
        let interruptions = report["interruptions"] as! [[String: Any]]
        precondition(interruptions.count == 1 && interruptions[0]["silence_inserted"] as? Bool == true)
        precondition((report["silence_frames"] as! Int64) >= 24_000 - 2)
        precondition(report["coverage_complete"] as? Bool == false)
        let before = try Data(contentsOf: url)
        _ = sink.append(tone(rate: 48_000, seconds: 0.1, frequency: 880), time: time(4), generation: 2, converter: secondConverter)
        let after = try Data(contentsOf: url)
        precondition(after == before, "Late buffer modified a finalized file")

        let reader = try AVAudioFile(forReading: url)
        precondition(reader.processingFormat.sampleRate == 24_000)
        let samples = AVAudioPCMBuffer(pcmFormat: reader.processingFormat, frameCapacity: AVAudioFrameCount(reader.length))!
        try reader.read(into: samples)
        let rate = reader.processingFormat.sampleRate
        func rms(_ start: Double, _ end: Double) -> Double {
            let values = samples.floatChannelData![0]
            let first = Int(start * rate), last = min(Int(end * rate), Int(samples.frameLength))
            var sum = 0.0
            for index in first..<last { sum += Double(values[index] * values[index]) }
            return sqrt(sum / Double(last - first))
        }
        precondition(abs(Double(reader.length) / rate - 3) < 0.03)
        precondition(rms(0.2, 0.8) > 0.1, "Pre-change audio was lost")
        precondition(rms(1.2, 1.8) < 0.002, "Missing interval was not represented by silence")
        precondition(rms(2.2, 2.8) > 0.1, "Resumed audio was lost")
        print("PASS actual PCM file preserves prefix, pads interruption, resamples suffix, rejects stale/closed callbacks")

        let terminal = try MicAudioSink(url: directory.appendingPathComponent("terminal.caf"), format: format(24_000))
        let terminalConverter = try MicPCMConverter(input: format(24_000), output: terminal.format)
        terminal.activate(generation: 1, voiceCheck: false)
        _ = terminal.append(tone(rate: 24_000, seconds: 1, frequency: 440), time: time(0), generation: 1, converter: terminalConverter)
        terminal.interrupt(reason: "microphone_buffers_stopped")
        terminal.close(at: origin + 13)
        let missing = (terminal.report["interruptions"] as! [[String: Any]])[0]
        precondition(missing["silence_inserted"] as? Bool == false)
        precondition(abs((missing["end_seconds"] as! Double) - 13) < 0.001)
        precondition(terminal.report["duration_seconds"] as? Double == 1)
        print("PASS terminal microphone loss is explicit instead of claiming complete coverage")

        let voice = try MicAudioSink(url: directory.appendingPathComponent("voice.caf"), format: format(24_000))
        let voiceConverter = try MicPCMConverter(input: format(24_000), output: voice.format)
        voice.activate(generation: 1, voiceCheck: true)
        let silentPrefix = voice.append(tone(rate: 24_000, seconds: 1, frequency: 0),
                                       time: time(0), generation: 1, converter: voiceConverter)
        precondition(silentPrefix.needsRawFallback)
        let voiceOrigin = voice.firstBufferAt
        voice.interrupt(reason: "voice_processing_silence")
        voice.activate(generation: 2, voiceCheck: false)
        let rawConverter = try MicPCMConverter(input: format(48_000), output: voice.format)
        let raw = voice.append(tone(rate: 48_000, seconds: 1, frequency: 440),
                               time: time(1.5), generation: 2, converter: rawConverter)
        precondition(raw.firstBuffer && !raw.needsRawFallback && raw.error == nil)
        voice.close(at: origin + 2.5)
        precondition(voice.firstBufferAt == voiceOrigin)
        precondition(voice.report["duration_seconds"] as? Double == 2.5)
        precondition(voice.report["captured_frames"] as? Int64 == 48_000)
        print("PASS silent voice-processing fallback keeps the prefix and recording origin")
    }

    private static func offMain(_ body: @escaping @Sendable () -> Void) async {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                dispatchPrecondition(condition: .notOnQueue(.main))
                body()
                continuation.resume()
            }
        }
    }

    @MainActor
    static func callbackIsolationChecks() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("quill-mic-callbacks-\(UUID())")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("synthetic.caf")
        let sink = try MicAudioSink(url: url, format: format(24_000))
        let converter = try MicPCMConverter(input: format(24_000), output: sink.format)
        let origin = AVAudioTime.seconds(forHostTime: mach_absolute_time())
        let events = CallbackEvents()
        sink.activate(generation: 41, voiceCheck: false)

        // Production also creates these callbacks from MainActor. Invoke the
        // same blocks on a real dispatch queue, without opening an audio device.
        let tap: @convention(block) @Sendable (AVAudioPCMBuffer, AVAudioTime) -> Void =
            MicAudioCallbacks.tap(sink: sink, converter: converter, generation: 41) { result, generation in
            MainActor.preconditionIsolated()
            events.audio.append(result)
            events.audioGenerations.append(generation)
        }
        await offMain {
            tap(tone(rate: 24_000, seconds: 0.1, frequency: 440),
                AVAudioTime(hostTime: AVAudioTime.hostTime(forSeconds: origin)))
        }
        let deadline = Date().addingTimeInterval(5)
        while events.audio.isEmpty && Date() < deadline {
            try await Task.sleep(for: .milliseconds(10))
        }
        precondition(events.audio.count == 1 && events.audio[0].firstBuffer && events.audio[0].error == nil)
        precondition(events.audioGenerations == [41])
        precondition(sink.report["captured_frames"] as? Int64 == 2_400)

        sink.interrupt(reason: "synthetic_generation_change")
        sink.activate(generation: 42, voiceCheck: false)
        await offMain {
            tap(tone(rate: 24_000, seconds: 0.1, frequency: 440),
                AVAudioTime(hostTime: AVAudioTime.hostTime(forSeconds: origin + 0.1)))
        }
        precondition(sink.report["captured_frames"] as? Int64 == 2_400, "Stale callback wrote audio")
        sink.close(at: origin + 0.1)
        let saved = try Data(contentsOf: url)
        await offMain {
            tap(tone(rate: 24_000, seconds: 0.1, frequency: 440),
                AVAudioTime(hostTime: AVAudioTime.hostTime(forSeconds: origin + 0.2)))
        }
        let afterClosedCallback = try Data(contentsOf: url)
        precondition(afterClosedCallback == saved, "Callback changed the closed recording")
        precondition(events.audio.count == 1, "Stale callback reported fresh audio")
        print("PASS MainActor-created tap runs off-main, writes synthetic audio, returns status on MainActor, and rejects stale callbacks")

        let center = NotificationCenter()
        let observer = center.addObserver(forName: .AVAudioEngineConfigurationChange, object: nil, queue: nil,
            using: MicAudioCallbacks.configurationChange(generation: 42) { generation in
                MainActor.preconditionIsolated()
                events.changes.append(generation)
            })
        defer { center.removeObserver(observer) }
        await offMain { center.post(name: .AVAudioEngineConfigurationChange, object: nil) }
        let changeDeadline = Date().addingTimeInterval(5)
        while events.changes.isEmpty && Date() < changeDeadline {
            try await Task.sleep(for: .milliseconds(10))
        }
        precondition(events.changes == [42])
        print("PASS audio configuration notification arrives off-main and dispatches its generation to MainActor")
    }

    @MainActor
    static func main() async throws {
        policyChecks()
        timelineChecks()
        try conversionChecks()
        try failedFirstWriteKeepsOrigin()
        try sinkChecks()
        try await callbackIsolationChecks()
        print("Microphone recovery checks passed (11 scenarios; synthetic audio only, no devices opened).")
    }
}
