import AVFoundation
import Foundation

/// A converter belongs to one input generation. The output format belongs to
/// the recording and never changes when a headset connects or disconnects.
final class MicPCMConverter: @unchecked Sendable {
    /// AVAudioConverter's callback is synchronous but imported as Sendable.
    /// Keep its one-shot input ownership in a locked box, not captured locals.
    private final class Input: @unchecked Sendable {
        let buffer: AVAudioPCMBuffer
        private let lock = NSLock()
        private var offset = 0
        init(_ buffer: AVAudioPCMBuffer) { self.buffer = buffer }
        var remainingFrames: Int { lock.withLock { Int(buffer.frameLength) - offset } }
        func next(_ requested: AVAudioPacketCount,
                  _ status: UnsafeMutablePointer<AVAudioConverterInputStatus>) -> AVAudioBuffer? {
            lock.withLock { () -> AVAudioBuffer? in
                let count = min(Int(requested), Int(buffer.frameLength) - offset)
                guard count > 0 else { status.pointee = .noDataNow; return nil }
                guard let slice = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: AVAudioFrameCount(count)) else {
                    status.pointee = .noDataNow
                    return nil
                }
                slice.frameLength = AVAudioFrameCount(count)
                let source = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
                let destination = UnsafeMutableAudioBufferListPointer(slice.mutableAudioBufferList)
                let bytesPerFrame = Int(buffer.format.streamDescription.pointee.mBytesPerFrame)
                for index in source.indices {
                    destination[index].mData!.copyMemory(
                        from: source[index].mData!.advanced(by: offset * bytesPerFrame),
                        byteCount: count * bytesPerFrame)
                }
                offset += count
                status.pointee = .haveData
                return slice
            }
        }
    }
    private let converter: AVAudioConverter
    private let outputFormat: AVAudioFormat

    init(input: AVAudioFormat, output: AVAudioFormat) throws {
        guard let converter = AVAudioConverter(from: input, to: output) else {
            throw NSError(domain: "QuillMicrophone", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Unsupported microphone format"])
        }
        self.converter = converter
        self.outputFormat = output
        converter.primeMethod = .none
    }

    func convert(_ input: AVAudioPCMBuffer) throws -> [AVAudioPCMBuffer] {
        let ratio = outputFormat.sampleRate / input.format.sampleRate
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * ratio) + 512)
        let source = Input(input)
        var outputs: [AVAudioPCMBuffer] = []
        // A capacity larger than the rate-adjusted input usually drains in one
        // call. Bound malformed converter behavior instead of spinning in a tap.
        for _ in 0..<8 {
            guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
                throw NSError(domain: "QuillMicrophone", code: 2)
            }
            var error: NSError?
            let status = converter.convert(to: output, error: &error) { requested, status in
                source.next(requested, status)
            }
            if status == .error { throw error ?? NSError(domain: "QuillMicrophone", code: 3) }
            if output.frameLength > 0 { outputs.append(output) }
            if status == .inputRanDry || status == .endOfStream || output.frameLength == 0 {
                guard source.remainingFrames == 0 else {
                    throw NSError(domain: "QuillMicrophone", code: 7,
                        userInfo: [NSLocalizedDescriptionKey: "Microphone converter did not consume its input"])
                }
                return outputs
            }
        }
        throw NSError(domain: "QuillMicrophone", code: 4,
                      userInfo: [NSLocalizedDescriptionKey: "Microphone conversion did not drain"])
    }
}

/// Split an output buffer at the shared pause boundaries. Timestamp mapping is
/// identical for both audio sources and the final screen composition.
func captureAudioSlices(_ buffer: AVAudioPCMBuffer, start: Double,
                        timeline: RecordingCaptureTimeline?) throws -> [(AVAudioPCMBuffer, Double)] {
    guard let timeline else { return [(buffer, start)] }
    return try timeline.audioSlices(start: start, frames: Int64(buffer.frameLength),
                                    sampleRate: buffer.format.sampleRate).map { slice in
        if slice.sourceFrames == 0..<Int64(buffer.frameLength) { return (buffer, slice.outputStart) }
        guard let copy = AVAudioPCMBuffer(pcmFormat: buffer.format,
                frameCapacity: AVAudioFrameCount(slice.sourceFrames.count)) else {
            throw NSError(domain: "QuillAudio", code: 10,
                          userInfo: [NSLocalizedDescriptionKey: "Unable to copy an audio pause boundary"])
        }
        copy.frameLength = AVAudioFrameCount(slice.sourceFrames.count)
        for channel in 0..<Int(buffer.format.channelCount) {
            copy.floatChannelData![channel].update(
                from: buffer.floatChannelData![channel].advanced(by: Int(slice.sourceFrames.lowerBound)),
                count: slice.sourceFrames.count)
        }
        return (copy, slice.outputStart)
    }
}

/// The tap and main actor meet only through this lock. Old-generation callbacks
/// cannot write after recovery/Stop, and closing the PCM file waits for writes.
/// This object creates the file exactly once; it never reopens or deletes it.
final class MicAudioSink: @unchecked Sendable {
    struct Result: Sendable {
        var firstBuffer = false
        var needsRawFallback = false
        var error: String?
    }

    let format: AVAudioFormat
    private let lock = NSLock()
    private var file: AVAudioFile?
    private var generation = -1
    private let captureTimeline: RecordingCaptureTimeline?
    private var timeline: MicCaptureTimeline
    private var firstDate: Date?
    private var latestBufferSeconds: Double?
    private var sawBuffer = false
    private var voiceCheck = false
    private var livenessFrames = 0
    private var livenessPeak: Float = 0
    private var livenessSettled = false
    private var reportedError = false
    private var pendingReason: String?
    private var interruptions: [[String: Any]] = []
    private var failures: [String] = []
    private let writeBuffer: @Sendable (AVAudioFile, AVAudioPCMBuffer) throws -> Void

    init(url: URL, format: AVAudioFormat, captureTimeline: RecordingCaptureTimeline? = nil,
         writeBuffer: @escaping @Sendable (AVAudioFile, AVAudioPCMBuffer) throws -> Void = { try $0.write(from: $1) }) throws {
        self.writeBuffer = writeBuffer
        self.captureTimeline = captureTimeline
        self.format = format
        timeline = MicCaptureTimeline(sampleRate: format.sampleRate)
        file = try AVAudioFile(forWriting: url, settings: [
            // Fixed-size PCM packets remain readable after an unclean exit;
            // AAC needs its packet table finalized when the file closes.
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: format.sampleRate,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ], commonFormat: .pcmFormatFloat32, interleaved: false)
    }

    var firstBufferAt: Date? { lock.withLock { firstDate } }
    var lastBufferSeconds: Double? { lock.withLock { latestBufferSeconds } }

    func activate(generation: Int, voiceCheck: Bool) {
        lock.withLock {
            self.generation = generation
            self.voiceCheck = voiceCheck
            sawBuffer = false
            livenessFrames = 0
            livenessPeak = 0
            livenessSettled = false
            reportedError = false
        }
    }

    func interrupt(reason: String) {
        lock.withLock {
            generation = -1
            pendingReason = pendingReason ?? reason
        }
    }

    func append(_ buffer: AVAudioPCMBuffer, time: AVAudioTime, generation: Int,
                converter: MicPCMConverter) -> Result {
        lock.withLock {
            guard self.generation == generation, let file, !reportedError,
                  buffer.frameLength > 0 else { return Result() }
            let now = AVAudioTime.seconds(forHostTime: mach_absolute_time())
            let seconds = time.isHostTimeValid ? AVAudioTime.seconds(forHostTime: time.hostTime)
                : now - Double(buffer.frameLength) / buffer.format.sampleRate
            var result = Result()
            do {
                var outputOffset = 0.0
                for converted in try converter.convert(buffer) {
                    let rawStart = seconds + outputOffset
                    outputOffset += Double(converted.frameLength) / format.sampleRate
                    for (output, mappedStart) in try captureAudioSlices(converted, start: rawStart, timeline: captureTimeline) {
                        let placement = timeline.place(startSeconds: mappedStart, frames: Int64(output.frameLength))
                        let gapStart = timeline.endSeconds
                        if placement.silenceFrames > 0 {
                            try appendSilence(placement.silenceFrames, file: file)
                            interruptions.append([
                                "start_seconds": gapStart, "end_seconds": timeline.endSeconds,
                                "reason": pendingReason ?? "capture_gap", "silence_inserted": true,
                            ])
                        }
                        if placement.audioFrames > 0 {
                            let audio: AVAudioPCMBuffer
                            if placement.trimFrames > 0 {
                                guard let trimmed = AVAudioPCMBuffer(pcmFormat: format,
                                    frameCapacity: AVAudioFrameCount(placement.audioFrames)) else {
                                    throw NSError(domain: "QuillMicrophone", code: 5)
                                }
                                trimmed.frameLength = AVAudioFrameCount(placement.audioFrames)
                                trimmed.floatChannelData![0].update(
                                    from: output.floatChannelData![0].advanced(by: Int(placement.trimFrames)),
                                    count: Int(placement.audioFrames))
                                audio = trimmed
                            } else { audio = output }
                            try writeBuffer(file, audio)
                            if firstDate == nil {
                                // An earlier failed write may have fixed the origin;
                                // the recovery prefix is already present as silence.
                                firstDate = Date().addingTimeInterval((timeline.originSeconds ?? mappedStart) - now)
                            }
                            timeline.committed(audio: Int64(audio.frameLength))
                            if !sawBuffer { result.firstBuffer = true; sawBuffer = true }
                        }
                        pendingReason = nil
                        if voiceCheck && !livenessSettled {
                            for i in 0..<Int(output.frameLength) {
                                livenessPeak = max(livenessPeak, abs(output.floatChannelData![0][i]))
                            }
                            livenessFrames += Int(output.frameLength)
                            if livenessFrames >= Int(format.sampleRate) {
                                livenessSettled = true
                                result.needsRawFallback = livenessPeak == 0
                            }
                        }
                    }
                }
                latestBufferSeconds = now
            } catch {
                let message = "Microphone audio could not be written: \(error.localizedDescription)"
                failures.append(message)
                reportedError = true
                result.error = message
            }
            return result
        }
    }

    private func appendSilence(_ count: Int64, file: AVAudioFile) throws {
        guard let silence = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 8192) else {
            throw NSError(domain: "QuillMicrophone", code: 6)
        }
        silence.floatChannelData![0].initialize(repeating: 0, count: 8192)
        var remaining = count
        while remaining > 0 {
            silence.frameLength = AVAudioFrameCount(min(remaining, 8192))
            try writeBuffer(file, silence)
            timeline.committed(silence: Int64(silence.frameLength))
            remaining -= Int64(silence.frameLength)
        }
    }

    func fail(_ message: String) { lock.withLock { failures.append(message) } }

    func close(at seconds: Double) {
        lock.withLock {
            generation = -1
            guard file != nil else { return }
            if let origin = timeline.originSeconds {
                let end = max(timeline.endSeconds, (captureTimeline?.outputTime(for: seconds) ?? seconds) - origin)
                if end - timeline.endSeconds > 0.25 {
                    // A terminal outage has no later audio to align. Record
                    // its extent without filling a potentially hours-long tail.
                    interruptions.append([
                        "start_seconds": timeline.endSeconds, "end_seconds": end,
                        "reason": pendingReason ?? "capture_ended_early", "silence_inserted": false,
                    ])
                }
            } else {
                failures.append("No microphone buffers were captured")
            }
            file = nil
        }
    }

    var report: [String: Any] {
        lock.withLock {
            var report: [String: Any] = ["sample_rate": format.sampleRate,
             "captured_frames": timeline.capturedFrames,
             "silence_frames": timeline.silenceFrames,
             "duration_seconds": timeline.endSeconds,
             "interruptions": interruptions,
             "errors": failures,
             "coverage_complete": failures.isEmpty && interruptions.isEmpty && timeline.capturedFrames > 0]
            if let captureTimeline, let origin = timeline.originSeconds {
                let offset = origin - captureTimeline.startedAt
                if offset.isFinite { report["capture_start_offset_seconds"] = max(0, offset) }
            }
            return report
        }
    }
}
