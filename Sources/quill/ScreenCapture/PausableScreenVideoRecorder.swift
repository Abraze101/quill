import AVFoundation
import Foundation

/// Native capture and the hardware-free lifecycle fixture share this boundary.
@MainActor
protocol ScreenVideoSegmentCapture: AnyObject {
    var onFailure: ((String) -> Void)? { get set }
    var onCleanup: (() -> Void)? { get set }
    var isRecording: Bool { get }
    var needsCleanup: Bool { get }
    var didStart: Bool { get }
    var startedAt: Date? { get }
    var firstFrameHostSeconds: Double? { get }
    func start(target: ScreenCaptureTarget, writingTo url: URL, validateStart: (() throws -> Void)?) async throws
    func stop() async throws -> URL?
}

/// Keeps the proven native writer as the unit of capture. A paused recording has
/// no running screen stream. Resume creates a new private segment; Stop trims
/// the native start/stop margins against the audio clock and joins the segments.
@MainActor
final class ScreenVideoRecorder {
    var onFailure: ((String) -> Void)?
    var onCleanup: (() -> Void)?
    var isRecording: Bool { current?.isRecording == true }
    var needsCleanup: Bool { owners.contains { $0.needsCleanup } }
    private(set) var didStart = false
    private(set) var startedAt: Date?
    private let makeSegment: @MainActor () -> any ScreenVideoSegmentCapture
    private let assemble: @MainActor ([ScreenVideoPauseAssembler.Segment], RecordingCaptureTimeline, URL) async throws -> Double
    private var current: (any ScreenVideoSegmentCapture)?
    private var owners: [any ScreenVideoSegmentCapture] = []
    private var segments: [ScreenVideoPauseAssembler.Segment] = []
    private var target: ScreenCaptureTarget?
    private var destination: URL?
    private var timeline: RecordingCaptureTimeline?
    private var partsDirectory: URL?
    private var paused = false
    private var everPaused = false
    private var stopped = false
    private var startTask: Task<Void, Error>?
    private var transitionTask: Task<Void, Error>?
    private var stopTask: Task<URL?, Error>?

    init(makeSegment: @escaping @MainActor () -> any ScreenVideoSegmentCapture = { NativeScreenVideoRecorder() },
         assemble: @escaping @MainActor ([ScreenVideoPauseAssembler.Segment], RecordingCaptureTimeline, URL) async throws -> Double = {
             try await ScreenVideoPauseAssembler.assemble(segments: $0, timeline: $1, destination: $2)
         }) {
        self.makeSegment = makeSegment
        self.assemble = assemble
    }

    func start(target: ScreenCaptureTarget, writingTo url: URL,
               timeline: RecordingCaptureTimeline? = nil,
               validateStart: (() throws -> Void)? = nil) async throws {
        guard startTask == nil, !stopped else { throw ScreenVideoError.alreadyStarted }
        self.target = target
        destination = url
        self.timeline = timeline
        let task = Task { try await beginSegment(at: url, validateStart: validateStart) }
        startTask = task
        try await task.value
    }

    func pause() async throws {
        guard !stopped, !paused, let destination, let timeline, timeline.isPaused else {
            throw ScreenVideoError.cancelled
        }
        if let transitionTask { try await transitionTask.value; return }
        everPaused = true
        paused = true
        let task = Task { @MainActor in
            try await self.finishCurrent()
            // Preserve the native segment before the final output takes its name.
            // A private, unique subfolder avoids collisions with earlier media.
            if self.partsDirectory == nil {
                let directory = destination.deletingLastPathComponent()
                    .appendingPathComponent(".screen-parts-\(UUID().uuidString)", isDirectory: true)
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
                self.partsDirectory = directory
                if let first = self.segments.first, first.url == destination {
                    let moved = directory.appendingPathComponent("part-000.mp4")
                    try FileManager.default.moveItem(at: first.url, to: moved)
                    self.segments[0] = .init(url: moved, firstFrameHostSeconds: first.firstFrameHostSeconds)
                }
            }
        }
        transitionTask = task
        defer { transitionTask = nil }
        do { try await task.value }
        catch { onFailure?(error.localizedDescription); throw error }
    }

    func resume(validateStart: (() throws -> Void)? = nil) async throws {
        if let transitionTask { try await transitionTask.value }
        guard !stopped, paused, let partsDirectory, timeline?.isPaused == true else {
            throw ScreenVideoError.cancelled
        }
        let url = partsDirectory.appendingPathComponent("part-\(UUID().uuidString).mp4")
        let task = Task { try await self.beginSegment(at: url, validateStart: validateStart) }
        transitionTask = task
        defer { transitionTask = nil }
        do {
            try await task.value
            guard !stopped else { throw ScreenVideoError.cancelled }
            paused = false
        } catch { onFailure?(error.localizedDescription); throw error }
    }

    /// Stop remains legal while pausing/resuming. It invalidates a late start
    /// immediately, waits for native cleanup, then finalizes exactly once.
    func stop() async throws -> URL? {
        if let stopTask { return try await stopTask.value }
        stopped = true
        let native = current
        let stopNative = Task { try await native?.stop() }
        let task = Task<URL?, Error> { @MainActor in
            var operationError: Error?
            do { try await self.startTask?.value } catch { operationError = error }
            do { try await self.transitionTask?.value } catch { operationError = operationError ?? error }
            do { _ = try await stopNative.value } catch { operationError = operationError ?? error }
            do { try await self.finishCurrent() } catch { operationError = operationError ?? error }
            if let operationError { throw operationError }
            guard let destination = self.destination else { return nil }
            guard self.everPaused else { return self.segments.first?.url }
            guard let timeline = self.timeline else { throw ScreenVideoError.emptyRecording }
            let origin = try await self.assemble(self.segments, timeline, destination)
            self.startedAt = Date().addingTimeInterval(origin - RecordingCaptureTimeline.now)
            // Originals are removed only after the assembled MP4 was verified.
            if let directory = self.partsDirectory { try? FileManager.default.removeItem(at: directory) }
            return destination
        }
        stopTask = task
        return try await task.value
    }

    private func beginSegment(at url: URL, validateStart: (() throws -> Void)?) async throws {
        guard !stopped, let target else { throw ScreenVideoError.cancelled }
        let recorder = makeSegment()
        current = recorder
        owners.append(recorder)
        recorder.onFailure = { [weak self] in self?.onFailure?($0) }
        recorder.onCleanup = { [weak self] in
            guard let self, self.stopped, !self.needsCleanup else { return }
            self.onCleanup?()
        }
        try await recorder.start(target: target, writingTo: url, validateStart: { [weak self] in
            guard let self, !self.stopped else { throw ScreenVideoError.cancelled }
            try validateStart?()
        })
        guard !stopped else { _ = try? await recorder.stop(); throw ScreenVideoError.cancelled }
        didStart = didStart || recorder.didStart
        startedAt = startedAt ?? recorder.startedAt
    }

    private func finishCurrent() async throws {
        guard let current else { return }
        guard let url = try await current.stop() else { return }
        if !segments.contains(where: { $0.url == url }) {
            guard let firstFrame = current.firstFrameHostSeconds else { throw ScreenVideoError.emptyRecording }
            segments.append(.init(url: url, firstFrameHostSeconds: firstFrame))
        }
        if self.current === current { self.current = nil }
    }
}

/// Aligns the writer's asset clock with the first observed screen-frame host
/// timestamp, then applies exactly the same cuts as both independent audio files.
@MainActor
enum ScreenVideoPauseAssembler {
    struct Segment: Sendable {
        let url: URL
        let firstFrameHostSeconds: Double
    }

    @discardableResult
    static func assemble(segments: [Segment], timeline: RecordingCaptureTimeline,
                         destination: URL) async throws -> Double {
        guard !segments.isEmpty, destination.isFileURL,
              (try? FileManager.default.attributesOfItem(atPath: destination.path)) == nil else {
            throw ScreenVideoError.destinationExists
        }
        let composition = AVMutableComposition()
        var outputTracks: [String: AVMutableCompositionTrack] = [:]
        var origin: Double?
        var outputEnd = 0.0
        var expectedAudioTracks: Int?
        for segment in segments {
            let asset = AVURLAsset(url: segment.url)
            let videos = try await asset.loadTracks(withMediaType: .video)
            let audio = try await asset.loadTracks(withMediaType: .audio)
            guard let video = videos.first, videos.count == 1,
                  segment.firstFrameHostSeconds.isFinite else { throw ScreenVideoError.emptyRecording }
            let videoRange = try await video.load(.timeRange)
            let duration = try await asset.load(.duration).seconds
            guard ScreenVideoIntegrity.coversRecording(duration: duration, videoStart: videoRange.start.seconds,
                                                       videoDuration: videoRange.duration.seconds) else {
                throw ScreenVideoError.emptyRecording
            }
            let hostOrigin = segment.firstFrameHostSeconds - videoRange.start.seconds
            let slices = timeline.activeSlices(from: hostOrigin, to: hostOrigin + duration)
            // A native startup segment can lie wholly inside a removed pause.
            // It must not establish either the output origin or empty tracks.
            guard let firstSlice = slices.first else { continue }
            if origin == nil { origin = firstSlice.outputStart }
            let outputOrigin = origin!
            // Native capture promises both sources; a segment losing one must
            // not silently produce a video with different audio coverage.
            if let expectedAudioTracks, expectedAudioTracks != audio.count {
                throw ScreenVideoError.captureFailed("Screen audio tracks changed across a pause. Original segments were preserved for review.")
            }
            expectedAudioTracks = audio.count
            for (type, tracks) in [(AVMediaType.video, videos), (.audio, audio)] {
                for (index, track) in tracks.enumerated() {
                    let key = "\(type.rawValue)-\(index)"
                    let output: AVMutableCompositionTrack
                    if let existing = outputTracks[key] { output = existing }
                    else {
                        guard let added = composition.addMutableTrack(withMediaType: type,
                                preferredTrackID: kCMPersistentTrackID_Invalid) else { throw ScreenVideoError.emptyRecording }
                        outputTracks[key] = added
                        output = added
                        if type == .video { output.preferredTransform = try await track.load(.preferredTransform) }
                    }
                    let trackRange = try await track.load(.timeRange)
                    for slice in slices {
                        let lower = max(slice.sourceStart - hostOrigin, trackRange.start.seconds)
                        let upper = min(slice.sourceEnd - hostOrigin, trackRange.end.seconds)
                        guard upper > lower else { continue }
                        let mappedStart = timeline.outputTime(for: hostOrigin + lower) - outputOrigin
                        guard mappedStart >= -0.001 else { throw ScreenVideoError.emptyRecording }
                        let range = CMTimeRange(start: CMTime(seconds: lower, preferredTimescale: 60_000),
                            duration: CMTime(seconds: upper - lower, preferredTimescale: 60_000))
                        try output.insertTimeRange(range, of: track,
                            at: CMTime(seconds: max(0, mappedStart), preferredTimescale: 60_000))
                        if type == .video { outputEnd = max(outputEnd, mappedStart + upper - lower) }
                    }
                }
            }
        }
        guard let origin, outputEnd > 0,
              let exporter = AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetHighestQuality) else {
            throw ScreenVideoError.emptyRecording
        }
        let temporary = destination.deletingLastPathComponent()
            .appendingPathComponent(".screen-joined-\(UUID().uuidString).mp4")
        defer { try? FileManager.default.removeItem(at: temporary) }
        try await exporter.export(to: temporary, as: .mp4)
        let result = AVURLAsset(url: temporary)
        let duration = try await result.load(.duration).seconds
        let tracks = try await result.loadTracks(withMediaType: .video)
        guard let video = tracks.first else { throw ScreenVideoError.emptyRecording }
        let range = try await video.load(.timeRange)
        guard abs(duration - composition.duration.seconds) < 0.25,
              ScreenVideoIntegrity.coversRecording(duration: duration, videoStart: range.start.seconds,
                                                   videoDuration: range.duration.seconds) else {
            throw ScreenVideoError.captureFailed("The paused screen recording could not be joined completely. Original segments were preserved for review.")
        }
        try FileManager.default.moveItem(at: temporary, to: destination)
        return origin
    }
}
