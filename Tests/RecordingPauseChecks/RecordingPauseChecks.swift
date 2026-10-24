import AVFoundation
import Foundation
import VideoToolbox

private final class Clock: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Double
    init(_ value: Double) { self.value = value }
    func set(_ value: Double) { lock.withLock { self.value = value } }
    func read() -> Double { lock.withLock { value } }
}

@main
private enum RecordingPauseChecks {
    @MainActor
    static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("quill-pause-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        timelineCuts()
        try audioFiles(root)
        try await screenTransitions(root)
        if ProcessInfo.processInfo.environment["QUILL_SKIP_SYNTHETIC_VIDEO_ASSEMBLY"] == "1" {
            print("SKIP synthetic MP4 assembly: explicitly disabled for the hosted VM's unavailable video scaler. Run test-recording-pause.sh locally for both MP4 assembly cases.")
            print("Recording pause checks passed (shared timeline, real synthetic audio, transition races; MP4 assembly skipped).")
            return
        }
        try await screenAssembly(root)
        print("Recording pause checks passed (shared timeline, real synthetic audio/video, transition races; no devices opened).")
    }

    static func close(_ a: Double, _ b: Double, tolerance: Double = 0.0001) { precondition(abs(a - b) < tolerance, "\(a) != \(b)") }

    static func progress(_ message: String) {
        FileHandle.standardError.write(Data((message + "\n").utf8))
    }

    static func timelineCuts() {
        let clock = Clock(100)
        let timeline = RecordingCaptureTimeline(clock: clock.read)
        precondition(timeline.pause(at: 102))
        precondition(!timeline.pause(at: 103))
        precondition(timeline.audioSlices(start: 103, frames: 100, sampleRate: 100).isEmpty)
        let left = timeline.audioSlices(start: 101.5, frames: 100, sampleRate: 100)
        precondition(left.count == 1 && left[0].sourceFrames == 0..<50)
        precondition(timeline.resume(at: 105))
        precondition(!timeline.resume(at: 105))
        let right = timeline.audioSlices(start: 104.5, frames: 100, sampleRate: 100)
        precondition(right.count == 1 && right[0].sourceFrames == 50..<100)
        close(right[0].outputStart, 102)
        precondition(timeline.pause(at: 107))
        precondition(timeline.resume(at: 108))
        let spanning = timeline.audioSlices(start: 101, frames: 800, sampleRate: 100)
        precondition(spanning.map(\.sourceFrames) == [0..<100, 400..<600, 700..<800])
        precondition(spanning.map(\.outputStart) == [101, 102, 104])
        precondition(timeline.pause(at: 110))
        timeline.stop(at: 120)
        close(timeline.activeDuration, 6)
        close(timeline.pausedDuration, 14)
        precondition(!timeline.resume(at: 121))
        precondition(timeline.activeSlices(from: 110, to: 122).isEmpty)
        // Late, out-of-order callbacks use historical boundaries, not current paused state.
        precondition(timeline.audioSlices(start: 104.5, frames: 100, sampleRate: 100) == right)
        print("PASS exact cuts, duplicate commands, late callbacks, multiple pauses and Stop while paused")
    }

    static func pcm(format: AVAudioFormat, frames: Int, value: Float) -> AVAudioPCMBuffer {
        let result = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))!
        result.frameLength = AVAudioFrameCount(frames)
        for channel in 0..<Int(format.channelCount) {
            for index in 0..<frames { result.floatChannelData![channel][index] = value * sin(Float(index) * 0.13) }
        }
        return result
    }

    static func audioFiles(_ root: URL) throws {
        let host = RecordingCaptureTimeline.now
        let clock = Clock(host - 1)
        let timeline = RecordingCaptureTimeline(clock: clock.read)
        let mono = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 1, interleaved: false)!
        let stereo = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 2, interleaved: false)!
        let micURL = root.appendingPathComponent("mic.caf")
        let systemURL = root.appendingPathComponent("system.caf")
        let mic = try MicAudioSink(url: micURL, format: mono, captureTimeline: timeline)
        let system = try SystemAudioSink(url: systemURL, sampleRate: 48_000,
            clock: { host + 1 }, captureTimeline: timeline)
        let micConverter = try MicPCMConverter(input: mono, output: mono)
        let systemConverter = try MicPCMConverter(input: stereo, output: stereo)
        mic.activate(generation: 1, voiceCheck: false)
        system.activate(generation: 1)
        // A callback straddles each edge. The centre carries a loud sentinel
        // that must never enter either file, even when delivered after Resume.
        timeline.pause(at: host - 0.75)
        timeline.resume(at: host - 0.25)
        for (start, amplitude) in [(host - 1, Float(0.2)), (host - 0.75, 0.95), (host - 0.5, 0.95), (host - 0.25, 0.3)] {
            let time = AVAudioTime(hostTime: AVAudioTime.hostTime(forSeconds: start))
            _ = mic.append(pcm(format: mono, frames: 12_000, value: amplitude), time: time, generation: 1, converter: micConverter)
            system.append(pcm(format: stereo, frames: 12_000, value: amplitude), hostSeconds: start, generation: 1, converter: systemConverter)
        }
        timeline.pause(at: host)
        clock.set(host + 20)
        mic.close(at: host + 20)
        system.close(at: host + 20)
        for report in [mic.report, system.report] {
            // Converting a Double host timestamp back to integer host ticks
            // may move an edge by one PCM frame; the cut rounds inward.
            precondition(abs((report["captured_frames"] as! Int64) - 24_000) <= 2)
            precondition(report["silence_frames"] as? Int64 == 0)
            precondition(report["coverage_complete"] as? Bool == true)
            close(report["duration_seconds"] as! Double, 0.5)
        }
        for url in [micURL, systemURL] {
            let file = try AVAudioFile(forReading: url)
            close(Double(file.length) / file.processingFormat.sampleRate, 0.5, tolerance: 0.03)
            let read = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length))!
            try file.read(into: read)
            var peak: Float = 0
            for channel in 0..<Int(read.format.channelCount) {
                for frame in 0..<Int(read.frameLength) { peak = max(peak, abs(read.floatChannelData![channel][frame])) }
            }
            precondition(peak < 0.6, "Paused sentinel leaked into saved audio")
        }
        print("PASS both PCM files discard paused content, retain active duration and do not invent recovery gaps")
    }

    @MainActor
    static func screenTransitions(_ root: URL) async throws {
        let clock = Clock(100)
        let timeline = RecordingCaptureTimeline(clock: clock.read)
        var segments: [FakeSegment] = []
        var joins = 0
        let recorder = ScreenVideoRecorder(makeSegment: {
            let segment = FakeSegment()
            segments.append(segment)
            return segment
        }, assemble: { parts, _, destination in
            joins += 1
            precondition(parts.count == 2)
            try Data("joined".utf8).write(to: destination)
            return 100
        })
        let url = root.appendingPathComponent("transition.mp4")
        try await recorder.start(target: .display(id: 0), writingTo: url, timeline: timeline)
        precondition(recorder.isRecording)
        timeline.pause(at: 102)
        try await recorder.pause()
        precondition(!recorder.isRecording && !recorder.needsCleanup && joins == 0)
        precondition(segments[0].stopCount == 1)
        try await recorder.resume()
        precondition(recorder.isRecording && timeline.isPaused, "Audio resumed before UI coordinator committed resume")
        timeline.resume(at: 105)
        timeline.pause(at: 107)
        try await recorder.pause()
        timeline.stop(at: 120)
        let result = try await recorder.stop()
        let repeated = try await recorder.stop()
        precondition(result == url && repeated == url && joins == 1)
        precondition(!recorder.needsCleanup)

        let raceTimeline = RecordingCaptureTimeline(clock: clock.read)
        var raceSegments: [FakeSegment] = []
        let race = ScreenVideoRecorder(makeSegment: {
            let segment = FakeSegment()
            segment.blockStart = !raceSegments.isEmpty
            raceSegments.append(segment)
            return segment
        }, assemble: { _, _, _ in preconditionFailure("Cancelled resume must not publish a successful joined output") })
        try await race.start(target: .display(id: 0), writingTo: root.appendingPathComponent("race.mp4"), timeline: raceTimeline)
        raceTimeline.pause(at: 102)
        try await race.pause()
        let resume = Task { try await race.resume() }
        while raceSegments.count < 2 { await Task.yield() }
        let stop = Task { try await race.stop() }
        while !raceSegments[1].stopRequested { await Task.yield() }
        raceSegments[1].releaseStart()
        do { try await resume.value; preconditionFailure("Stopped resume succeeded") } catch {}
        do { _ = try await stop.value; preconditionFailure("Cancelled resume was published as complete") } catch {}
        precondition(!race.isRecording && !race.needsCleanup)
        print("PASS screen pause owns no stream, resume waits for shared audio commit, repeated Stop and Stop-during-resume")
    }

    @MainActor
    static func screenAssembly(_ root: URL) async throws {
        let first = root.appendingPathComponent("synthetic-one.mp4")
        let second = root.appendingPathComponent("synthetic-two.mp4")
        try await makeVideo(first, color: 40)
        try await makeVideo(second, color: 180)
        let clock = Clock(100)
        let timeline = RecordingCaptureTimeline(clock: clock.read)
        timeline.pause(at: 101.25)
        timeline.resume(at: 105.5)
        timeline.stop(at: 107.75)
        let output = root.appendingPathComponent("screen.mp4")
        progress("START screen assembly: trim native stop/start latency")
        let origin = try await ScreenVideoPauseAssembler.assemble(segments: [
            .init(url: first, firstFrameHostSeconds: 100), .init(url: second, firstFrameHostSeconds: 105)
        ], timeline: timeline, destination: output)
        close(origin, 100)
        let asset = AVURLAsset(url: output)
        let duration = try await asset.load(.duration).seconds
        close(duration, 3.5, tolerance: 0.08)
        let audio = try await asset.loadTracks(withMediaType: .audio)
        let video = try await asset.loadTracks(withMediaType: .video)
        precondition(audio.count == 1 && video.count == 1)
        close(try await audio[0].load(.timeRange).duration.seconds, 3.5, tolerance: 0.08)
        // Native stop tail (1.75 s) and native resume lead (0.5 s) were removed,
        // not appended to the other active segment or hidden as silent padding.
        print("PASS real MP4 assembly cuts native stop/start latency with the same audio clock (3.5 seconds, audio + video)")

        let initiallyPaused = RecordingCaptureTimeline(clock: clock.read)
        initiallyPaused.pause(at: 100)
        initiallyPaused.resume(at: 105)
        initiallyPaused.stop(at: 107.75)
        let trimmedOutput = root.appendingPathComponent("screen-initial-segment-removed.mp4")
        progress("START screen assembly: exclude the initially paused segment")
        let trimmedOrigin = try await ScreenVideoPauseAssembler.assemble(segments: [
            .init(url: first, firstFrameHostSeconds: 100.25),
            .init(url: second, firstFrameHostSeconds: 104.75)
        ], timeline: initiallyPaused, destination: trimmedOutput)
        close(trimmedOrigin, 100)
        let trimmedAsset = AVURLAsset(url: trimmedOutput)
        close(try await trimmedAsset.load(.duration).seconds, 2.75, tolerance: 0.08)
        print("PASS a fully excluded first screen segment cannot shift the saved video origin or add a blank prefix")
    }

    @MainActor
    static func makeVideo(_ url: URL, color: UInt8) async throws {
        let raw = url.deletingLastPathComponent().appendingPathComponent("raw-\(UUID().uuidString).mp4")
        let audio = url.deletingLastPathComponent().appendingPathComponent("tone-\(UUID().uuidString).caf")
        defer { try? FileManager.default.removeItem(at: raw); try? FileManager.default.removeItem(at: audio) }
        progress("START synthetic video writer: \(url.lastPathComponent) (software H.264)")
        let writer = try AVAssetWriter(outputURL: raw, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 64, AVVideoHeightKey: 64,
            // Fixtures must not require a hardware encoder on hosted macOS VMs.
            AVVideoEncoderSpecificationKey: [
                kVTVideoEncoderSpecification_EnableHardwareAcceleratedVideoEncoder as String: false
            ]])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input,
            sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                kCVPixelBufferWidthKey as String: 64, kCVPixelBufferHeightKey as String: 64])
        writer.add(input)
        precondition(writer.startWriting())
        writer.startSession(atSourceTime: .zero)
        for frame in 0..<30 {
            let deadline = Date().addingTimeInterval(5)
            while !input.isReadyForMoreMediaData {
                precondition(Date() < deadline)
                try await Task.sleep(for: .milliseconds(5))
            }
            var pixel: CVPixelBuffer?
            precondition(CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &pixel) == kCVReturnSuccess)
            CVPixelBufferLockBaseAddress(pixel!, [])
            memset(CVPixelBufferGetBaseAddress(pixel!), Int32(color), CVPixelBufferGetDataSize(pixel!))
            CVPixelBufferUnlockBaseAddress(pixel!, [])
            precondition(adaptor.append(pixel!, withPresentationTime: CMTime(value: Int64(frame), timescale: 10)))
        }
        writer.endSession(atSourceTime: CMTime(value: 3, timescale: 1))
        input.markAsFinished()
        await writer.finishWriting()
        precondition(writer.status == .completed)
        progress("PASS synthetic video writer: \(url.lastPathComponent)")
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 1, interleaved: false)!
        do {
            let file = try AVAudioFile(forWriting: audio, settings: format.settings)
            try file.write(from: pcm(format: format, frames: 144_000, value: 0.2))
        }
        let composition = AVMutableComposition()
        for (asset, media) in [(AVURLAsset(url: raw), AVMediaType.video), (AVURLAsset(url: audio), .audio)] {
            let source = try await asset.loadTracks(withMediaType: media)[0]
            let track = composition.addMutableTrack(withMediaType: media, preferredTrackID: kCMPersistentTrackID_Invalid)!
            try track.insertTimeRange(CMTimeRange(start: .zero, duration: CMTime(value: 3, timescale: 1)), of: source, at: .zero)
        }
        progress("START synthetic audio/video export: \(url.lastPathComponent)")
        let exporter = AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetHighestQuality)!
        try await exporter.export(to: url, as: .mp4)
        progress("PASS synthetic audio/video export: \(url.lastPathComponent)")
    }
}

@MainActor
private final class FakeSegment: ScreenVideoSegmentCapture {
    var onFailure: ((String) -> Void)?
    var onCleanup: (() -> Void)?
    var isRecording = false
    var needsCleanup = false
    var didStart = false
    var startedAt: Date? = Date()
    var firstFrameHostSeconds: Double? = 100
    var stopCount = 0
    var stopRequested = false
    var blockStart = false
    private var waiting: CheckedContinuation<Void, Never>?
    private var url: URL?
    func start(target: ScreenCaptureTarget, writingTo url: URL, validateStart: (() throws -> Void)?) async throws {
        self.url = url
        needsCleanup = true
        if blockStart { await withCheckedContinuation { waiting = $0 } }
        if stopRequested { needsCleanup = false; throw ScreenVideoError.cancelled }
        try validateStart?()
        try Data("synthetic-segment".utf8).write(to: url)
        isRecording = true
        didStart = true
    }
    func releaseStart() { waiting?.resume(); waiting = nil }
    func stop() async throws -> URL? {
        if !stopRequested { stopCount += 1 }
        stopRequested = true
        isRecording = false
        if waiting == nil { needsCleanup = false; onCleanup?() }
        return didStart ? url : nil
    }
}
