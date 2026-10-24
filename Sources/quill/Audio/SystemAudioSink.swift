import AVFoundation
import Foundation

/// One stereo PCM CAF file for the entire session. Its lock protects file writes,
/// generation changes, timing, and closure against the native callback queue.
final class SystemAudioSink: @unchecked Sendable {
    struct Telemetry: Sendable {
        var lastWrite: Double?
        var failure: String?
    }
    let format: AVAudioFormat
    private let lock = NSLock()
    private var file: AVAudioFile?
    private var generation = -1
    private let captureTimeline: RecordingCaptureTimeline?
    private var timeline: MicCaptureTimeline
    private var firstDate: Date?
    private var current = Telemetry()
    private var pendingReason: String?
    private var interruptions: [[String: Any]] = []
    private var failures: [String] = []
    private var interruptionCount = 0
    private var failureCount = 0
    private var terminalFailure: String?
    private let writeBuffer: @Sendable (AVAudioFile, AVAudioPCMBuffer) throws -> Void
    private let clock: @Sendable () -> Double

    init(url: URL, sampleRate: Double, clock: @escaping @Sendable () -> Double,
         captureTimeline: RecordingCaptureTimeline? = nil,
         writeBuffer: @escaping @Sendable (AVAudioFile, AVAudioPCMBuffer) throws -> Void = { try $0.write(from: $1) }) throws {
        guard let format = AVAudioFormat(commonFormat: .pcmFormatFloat32,
            sampleRate: sampleRate, channels: 2, interleaved: false) else {
            throw NSError(domain: "QuillSystemAudio", code: 1)
        }
        self.captureTimeline = captureTimeline
        self.format = format
        self.clock = clock
        self.writeBuffer = writeBuffer
        timeline = MicCaptureTimeline(sampleRate: sampleRate)
        file = try AVAudioFile(forWriting: url, settings: [
            // Unlike AAC, fixed-size PCM packets need no final packet table
            // for already-written audio to survive a process crash.
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: 2,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ], commonFormat: .pcmFormatFloat32, interleaved: false)
    }

    var firstBufferAt: Date? { lock.withLock { firstDate } }
    var telemetry: Telemetry { lock.withLock { current } }
    var hasCapturedAudio: Bool { lock.withLock { timeline.capturedFrames > 0 } }
    var captureFailure: String? { lock.withLock { terminalFailure ?? failures.first } }

    func activate(generation: Int) {
        lock.withLock { self.generation = generation; current = Telemetry() }
    }

    func interrupt(reason: String) {
        lock.withLock { generation = -1; pendingReason = pendingReason ?? reason }
    }

    func append(_ buffer: AVAudioPCMBuffer, hostSeconds: Double?, generation: Int, converter: MicPCMConverter) {
        lock.withLock {
            guard self.generation == generation, let file, current.failure == nil,
                  buffer.frameLength > 0 else { return }
            let now = clock()
            let candidate = hostSeconds ?? (now - Double(buffer.frameLength) / buffer.format.sampleRate)
            // Reject malformed timestamps rather than allocate an unbounded
            // silence interval or overflow the frame count.
            guard candidate.isFinite, candidate <= now + 1, candidate >= now - 30 else {
                recordFailure("System audio supplied an invalid capture timestamp")
                return
            }
            do {
                var offset = 0.0
                for converted in try converter.convert(buffer) {
                    let rawStart = candidate + offset
                    offset += Double(converted.frameLength) / format.sampleRate
                    for (output, mappedStart) in try captureAudioSlices(converted, start: rawStart, timeline: captureTimeline) {
                        let placement = timeline.place(startSeconds: mappedStart, frames: Int64(output.frameLength))
                        if placement.silenceFrames > 0 {
                            // A stalled/recovered track should return in seconds;
                            // longer gaps stay explicitly incomplete, never huge
                            // callback-blocking writes after machine sleep.
                            guard Double(placement.silenceFrames) / format.sampleRate <= 30 else {
                                throw NSError(domain: "QuillSystemAudio", code: 2,
                                    userInfo: [NSLocalizedDescriptionKey: "System audio gap exceeds 30 seconds"])
                            }
                            let start = timeline.endSeconds
                            try appendSilence(placement.silenceFrames, to: file)
                            recordInterruption(["start_seconds": start, "end_seconds": timeline.endSeconds,
                                "reason": pendingReason ?? "capture_gap", "silence_inserted": true])
                        }
                        if placement.audioFrames > 0 {
                            var audio = output
                            if placement.trimFrames > 0 {
                                guard let trimmed = AVAudioPCMBuffer(pcmFormat: format,
                                    frameCapacity: AVAudioFrameCount(placement.audioFrames)) else {
                                    throw NSError(domain: "QuillSystemAudio", code: 3)
                                }
                                trimmed.frameLength = AVAudioFrameCount(placement.audioFrames)
                                for channel in 0..<Int(format.channelCount) {
                                    trimmed.floatChannelData![channel].update(
                                        from: output.floatChannelData![channel].advanced(by: Int(placement.trimFrames)),
                                        count: Int(placement.audioFrames))
                                }
                                audio = trimmed
                            }
                            try writeBuffer(file, audio)
                            if firstDate == nil {
                                // A failed first write may already have established
                                // the file timeline before recovery pads its prefix.
                                firstDate = Date().addingTimeInterval((timeline.originSeconds ?? mappedStart) - now)
                            }
                            timeline.committed(audio: Int64(audio.frameLength))
                            current.lastWrite = now
                        }
                        pendingReason = nil
                    }
                }
                // Paused callbacks prove transport liveness without writing media.
                if captureTimeline?.isPaused == true { current.lastWrite = now }
            } catch { recordFailure("System audio could not be saved: \(error.localizedDescription)") }
        }
    }

    private func appendSilence(_ count: Int64, to file: AVAudioFile) throws {
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 8192) else {
            throw NSError(domain: "QuillSystemAudio", code: 4)
        }
        for channel in 0..<Int(format.channelCount) { buffer.floatChannelData![channel].initialize(repeating: 0, count: 8192) }
        var remaining = count
        while remaining > 0 {
            buffer.frameLength = AVAudioFrameCount(min(remaining, 8192))
            try writeBuffer(file, buffer)
            timeline.committed(silence: Int64(buffer.frameLength))
            remaining -= Int64(buffer.frameLength)
        }
    }

    private func recordFailure(_ message: String) {
        current.failure = message
        failureCount += 1
        if failures.count < 16 { failures.append(String(message.prefix(512))) }
    }

    private func recordInterruption(_ entry: [String: Any]) {
        interruptionCount += 1
        guard interruptions.count < 48 else { return }
        var bounded = entry
        if let reason = bounded["reason"] as? String { bounded["reason"] = String(reason.prefix(256)) }
        interruptions.append(bounded)
    }

    func recordRecovery(reason: String, attempt: Int) {
        lock.withLock {
            // Preserve even an interruption too short for silence padding.
            recordInterruption(["start_seconds": timeline.endSeconds,
                "reason": reason, "attempt": attempt, "silence_inserted": false])
        }
    }

    func fail(_ message: String) {
        lock.withLock { terminalFailure = String(message.prefix(512)); recordFailure(message); generation = -1 }
    }

    func close(at seconds: Double) {
        lock.withLock {
            generation = -1
            guard file != nil else { return }
            if let origin = timeline.originSeconds {
                let end = max(timeline.endSeconds, (captureTimeline?.outputTime(for: seconds) ?? seconds) - origin)
                if end - timeline.endSeconds > 0.25 {
                    recordInterruption(["start_seconds": timeline.endSeconds, "end_seconds": end,
                        "reason": pendingReason ?? "capture_ended_early", "silence_inserted": false])
                }
            } else { recordFailure("No system audio buffers were captured") }
            file = nil
        }
    }

    var report: [String: Any] {
        lock.withLock {
            var report: [String: Any] = ["received_audio": timeline.capturedFrames > 0,
                "sample_rate": format.sampleRate, "channels": format.channelCount,
                "captured_frames": timeline.capturedFrames, "silence_frames": timeline.silenceFrames,
                "duration_seconds": timeline.endSeconds, "interruptions": interruptions,
                "errors": failures, "interruption_count": interruptionCount, "error_count": failureCount,
                "coverage_complete": failures.isEmpty && interruptions.isEmpty && timeline.capturedFrames > 0]
            if let captureTimeline, let origin = timeline.originSeconds {
                let offset = origin - captureTimeline.startedAt
                if offset.isFinite { report["capture_start_offset_seconds"] = max(0, offset) }
            }
            if let error = terminalFailure ?? failures.first { report["error"] = error }
            return report
        }
    }
}
