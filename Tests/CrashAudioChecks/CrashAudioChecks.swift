import AVFoundation
import Darwin
import Foundation

/// No capture device or model is opened. The child owns the production audio
/// sinks; the parent reads their files while open and after killing the child.
@main
private enum CrashAudioChecks {
    static let duration = 2.0

    static func main() throws {
        if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--child" {
            try writeWithoutClosing(URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true))
            return
        }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("quill-crash-audio-\(UUID())")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let child = Process()
        child.executableURL = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
        child.arguments = ["--child", directory.path]
        child.standardInput = FileHandle.nullDevice
        child.standardOutput = FileHandle.nullDevice
        try child.run()
        defer {
            if child.isRunning {
                _ = kill(child.processIdentifier, SIGKILL)
                child.waitUntilExit()
            }
        }
        let marker = directory.appendingPathComponent("ready.json")
        let deadline = Date().addingTimeInterval(15)
        while !FileManager.default.fileExists(atPath: marker.path) && child.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.01)
        }
        precondition(child.isRunning && FileManager.default.fileExists(atPath: marker.path),
                     "Synthetic writer failed before reporting completed writes")
        let reports = try JSONSerialization.jsonObject(with: Data(contentsOf: marker)) as! [String: [String: Any]]
        precondition(reports["mic"]?["captured_frames"] as? Int == 48_000)
        precondition(reports["system"]?["captured_frames"] as? Int == 96_000)
        for report in reports.values {
            precondition(report["coverage_complete"] as? Bool == true)
            precondition(report["silence_frames"] as? Int == 0)
            precondition(report["capture_start_offset_seconds"] == nil,
                         "A sink without the shared timeline invented a capture offset")
        }
        let micURL = directory.appendingPathComponent("mic.caf")
        let systemURL = directory.appendingPathComponent("system.caf")
        let micFrames = try checkPCM(micURL, sampleRate: 24_000, channels: 1, allowBufferedTail: true)
        let systemFrames = try checkPCM(systemURL, sampleRate: 48_000, channels: 2, allowBufferedTail: true)
        let micBefore = try Data(contentsOf: micURL)
        let systemBefore = try Data(contentsOf: systemURL)
        print("PASS open PCM files: mic \(micFrames)/48000 frames, system \(systemFrames)/96000 frames; retained samples match within 16-bit quantization")

        precondition(kill(child.processIdentifier, SIGKILL) == 0)
        child.waitUntilExit()
        precondition(child.terminationReason == .uncaughtSignal && child.terminationStatus == SIGKILL,
                     "Writer exited cleanly instead of exercising crash recovery")
        let micAfter = try Data(contentsOf: micURL)
        let systemAfter = try Data(contentsOf: systemURL)
        precondition(micAfter == micBefore && systemAfter == systemBefore, "SIGKILL unexpectedly finalized the files")
        let recoveredMicFrames = try checkPCM(micURL, sampleRate: 24_000, channels: 1, allowBufferedTail: true)
        let recoveredSystemFrames = try checkPCM(systemURL, sampleRate: 48_000, channels: 2, allowBufferedTail: true)
        precondition(recoveredMicFrames == micFrames && recoveredSystemFrames == systemFrames)
        print("PASS actual SIGKILL retains both CAF tracks without a final packet table or clean close")

        try checkCleanClose(directory)
        try checkCaptureStartOffsets(directory)

        try checkTranscriptionInput(directory.appendingPathComponent("mic.caf"))
        try checkTranscriptionInput(directory.appendingPathComponent("system.caf"))
        for channels: AVAudioChannelCount in [1, 2] {
            let legacy = directory.appendingPathComponent("legacy-aac-\(channels).caf")
            try writeLegacyAAC(legacy, channels: channels)
            let reader = try AVAudioFile(forReading: legacy)
            precondition(reader.fileFormat.streamDescription.pointee.mFormatID == kAudioFormatMPEG4AAC)
            precondition(reader.length > 0)
            try checkTranscriptionInput(legacy)
        }
        print("PASS finalized historical mono/stereo AAC CAF and crashed PCM CAF remain readable transcription inputs")
        print("Crash audio checks passed; generated samples only, no microphone, screen capture, models or user files.")
    }

    static func format(rate: Double, channels: AVAudioChannelCount) -> AVAudioFormat {
        AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: channels, interleaved: false)!
    }

    static func sample(frame: Int, rate: Double, channel: Int) -> Float {
        Float(0.3 * sin(2 * .pi * Double(frame) * Double(channel == 0 ? 440 : 880) / rate))
    }

    static func buffer(rate: Double, channels: AVAudioChannelCount, start: Int, frames: Int) -> AVAudioPCMBuffer {
        let buffer = AVAudioPCMBuffer(pcmFormat: format(rate: rate, channels: channels), frameCapacity: AVAudioFrameCount(frames))!
        buffer.frameLength = buffer.frameCapacity
        for channel in 0..<Int(channels) {
            for frame in 0..<frames {
                buffer.floatChannelData![channel][frame] = sample(frame: start + frame, rate: rate, channel: channel)
            }
        }
        return buffer
    }

    static func writeWithoutClosing(_ directory: URL) throws {
        // Bound the child's lifetime even if a parent assertion terminates
        // before its cleanup defer can run.
        _ = alarm(30)
        let now = AVAudioTime.seconds(forHostTime: mach_absolute_time())
        let origin = now - duration
        let mic = try MicAudioSink(url: directory.appendingPathComponent("mic.caf"), format: format(rate: 24_000, channels: 1))
        let system = try SystemAudioSink(url: directory.appendingPathComponent("system.caf"), sampleRate: 48_000, clock: { now })
        let micConverter = try MicPCMConverter(input: mic.format, output: mic.format)
        let systemConverter = try MicPCMConverter(input: system.format, output: system.format)
        mic.activate(generation: 1, voiceCheck: false)
        system.activate(generation: 1)
        try withExtendedLifetime((mic, system)) {
            for index in 0..<16 {
                let seconds = origin + Double(index) / 8
                let result = mic.append(buffer(rate: 24_000, channels: 1, start: index * 3_000, frames: 3_000),
                    time: AVAudioTime(hostTime: AVAudioTime.hostTime(forSeconds: seconds)), generation: 1, converter: micConverter)
                precondition(result.error == nil && result.firstBuffer == (index == 0))
                system.append(buffer(rate: 48_000, channels: 2, start: index * 6_000, frames: 6_000),
                              hostSeconds: seconds, generation: 1, converter: systemConverter)
                precondition(system.captureFailure == nil)
            }
            try JSONSerialization.data(withJSONObject: ["mic": mic.report, "system": system.report])
                .write(to: directory.appendingPathComponent("ready.json"), options: .atomic)
            // Intentionally never call close or release the sinks. SIGKILL
            // prevents Swift destructors and AVAudioFile finalization.
            while true { _ = pause() }
        }
    }

    @discardableResult
    static func checkPCM(_ url: URL, sampleRate: Double, channels: AVAudioChannelCount,
                         allowBufferedTail: Bool = false) throws -> Int {
        let file = try AVAudioFile(forReading: url, commonFormat: .pcmFormatFloat32, interleaved: false)
        let stream = file.fileFormat.streamDescription.pointee
        precondition(stream.mFormatID == kAudioFormatLinearPCM && stream.mBitsPerChannel == 16)
        precondition(stream.mFormatFlags & kAudioFormatFlagIsFloat == 0)
        precondition(stream.mFormatFlags & kAudioFormatFlagIsNonInterleaved == 0)
        precondition(stream.mBytesPerFrame == channels * 2)
        precondition(stream.mSampleRate == sampleRate && stream.mChannelsPerFrame == channels)
        let expectedFrames = Int(sampleRate * duration)
        let frames = Int(file.length)
        // AVAudioFile may retain a small final write in its process buffer.
        // PCM protects the readable prefix, not samples that never reached
        // the file. Recovery must use its actual length rather than the
        // capture callback count. Clean Stop must retain every frame.
        let missingBytes = (expectedFrames - frames) * Int(channels) * 2
        precondition(missingBytes >= 0 && missingBytes <= (allowBufferedTail ? 8192 : 0),
                     "PCM lost more than a bounded writer-buffer tail: \(frames)/\(expectedFrames) frames")
        let samples = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(frames))!
        try file.read(into: samples)
        precondition(samples.frameLength == AVAudioFrameCount(frames))
        for channel in 0..<Int(channels) {
            var maximumError: Float = 0
            for frame in 0..<frames {
                maximumError = max(maximumError, abs(samples.floatChannelData![channel][frame]
                    - sample(frame: frame, rate: sampleRate, channel: channel)))
            }
            precondition(maximumError <= 2.0 / 32_768, "Saved PCM exceeded 16-bit quantization error")
        }
        return frames
    }

    static func checkCleanClose(_ directory: URL) throws {
        let now = AVAudioTime.seconds(forHostTime: mach_absolute_time())
        let micURL = directory.appendingPathComponent("clean-mic.caf")
        let systemURL = directory.appendingPathComponent("clean-system.caf")
        let mic = try MicAudioSink(url: micURL, format: format(rate: 24_000, channels: 1))
        let system = try SystemAudioSink(url: systemURL, sampleRate: 48_000, clock: { now })
        mic.activate(generation: 1, voiceCheck: false)
        system.activate(generation: 1)
        let result = mic.append(buffer(rate: 24_000, channels: 1, start: 0, frames: 48_000),
            time: AVAudioTime(hostTime: AVAudioTime.hostTime(forSeconds: now - duration)), generation: 1,
            converter: try MicPCMConverter(input: mic.format, output: mic.format))
        precondition(result.error == nil)
        system.append(buffer(rate: 48_000, channels: 2, start: 0, frames: 96_000), hostSeconds: now - duration,
            generation: 1, converter: try MicPCMConverter(input: system.format, output: system.format))
        precondition(system.captureFailure == nil)
        mic.close(at: now)
        system.close(at: now)
        try checkPCM(micURL, sampleRate: 24_000, channels: 1)
        try checkPCM(systemURL, sampleRate: 48_000, channels: 2)
        print("PASS clean close retains every microphone/system frame with 16-bit sample fidelity")
    }

    static func writeLegacyAAC(_ url: URL, channels: AVAudioChannelCount) throws {
        // Keep the pre-upgrade settings as a compatibility fixture. Leaving
        // this scope cleanly is required for the old AAC packet table.
        let file = try AVAudioFile(forWriting: url, settings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 48_000,
            AVNumberOfChannelsKey: channels,
        ], commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: buffer(rate: 48_000, channels: channels, start: 0, frames: 96_000))
    }

    static func checkCaptureStartOffsets(_ directory: URL) throws {
        let timeline = RecordingCaptureTimeline(clock: { 100 })
        let mic = try MicAudioSink(url: directory.appendingPathComponent("delayed-mic.caf"),
            format: format(rate: 24_000, channels: 1), captureTimeline: timeline)
        let system = try SystemAudioSink(url: directory.appendingPathComponent("delayed-system.caf"),
            sampleRate: 48_000, clock: { 108 }, captureTimeline: timeline)
        precondition(mic.report["capture_start_offset_seconds"] == nil)
        precondition(system.report["capture_start_offset_seconds"] == nil)
        mic.activate(generation: 1, voiceCheck: false)
        system.activate(generation: 1)
        let first = mic.append(buffer(rate: 24_000, channels: 1, start: 0, frames: 12_000),
            time: AVAudioTime(hostTime: AVAudioTime.hostTime(forSeconds: 102)), generation: 1,
            converter: try MicPCMConverter(input: mic.format, output: mic.format))
        precondition(first.error == nil)
        system.append(buffer(rate: 48_000, channels: 2, start: 0, frames: 24_000), hostSeconds: 103,
            generation: 1, converter: try MicPCMConverter(input: system.format, output: system.format))
        func checkOffsets() {
            let micOffset = mic.report["capture_start_offset_seconds"] as? Double
            let systemOffset = system.report["capture_start_offset_seconds"] as? Double
            precondition(micOffset != nil && abs(micOffset! - 2) < 0.0001)
            precondition(systemOffset != nil && abs(systemOffset! - 3) < 0.0001)
        }
        checkOffsets()
        precondition(timeline.pause(at: 104))
        precondition(timeline.resume(at: 106))
        mic.interrupt(reason: "synthetic_route_change")
        system.interrupt(reason: "synthetic_route_change")
        mic.activate(generation: 2, voiceCheck: false)
        system.activate(generation: 2)
        let recovered = mic.append(buffer(rate: 48_000, channels: 1, start: 0, frames: 24_000),
            time: AVAudioTime(hostTime: AVAudioTime.hostTime(forSeconds: 106)), generation: 2,
            converter: try MicPCMConverter(input: format(rate: 48_000, channels: 1), output: mic.format))
        precondition(recovered.error == nil)
        system.append(buffer(rate: 24_000, channels: 2, start: 0, frames: 12_000), hostSeconds: 106,
            generation: 2, converter: try MicPCMConverter(input: format(rate: 24_000, channels: 2), output: system.format))
        precondition(system.captureFailure == nil)
        checkOffsets()
        mic.close(at: 106.5)
        system.close(at: 106.5)
        checkOffsets()
        precondition(abs((mic.report["duration_seconds"] as! Double) - 2.5) < 0.03)
        precondition(abs((system.report["duration_seconds"] as! Double) - 1.5) < 0.03)
        print("PASS capture-start offsets preserve distinct startup delays across shared pause, route/rate recovery and close")
    }

    static func checkTranscriptionInput(_ url: URL) throws {
        // ParakeetEngine's existing preflight uses this same AVAudioFile
        // read. The optional dependency check also executes FluidAudio's
        // actual 16 kHz mono preprocessing, without loading an ASR model.
        let probe = try AVAudioFile(forReading: url)
        precondition(probe.length > 0)
        #if QUILL_FLUID_AUDIO_CONVERTER
        let samples = try AudioConverter().resampleAudioFile(url)
        let readableDuration = Double(probe.length) / probe.processingFormat.sampleRate
        precondition(abs(Double(samples.count) / 16_000 - readableDuration) < 0.06)
        precondition(samples.allSatisfy(\.isFinite) && samples.contains { abs($0) > 0.1 })
        #else
        let buffer = AVAudioPCMBuffer(pcmFormat: probe.processingFormat, frameCapacity: AVAudioFrameCount(probe.length))!
        try probe.read(into: buffer)
        precondition(buffer.frameLength > 0)
        #endif
    }
}
