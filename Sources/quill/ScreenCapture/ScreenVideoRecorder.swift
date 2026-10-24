import AVFoundation
import Foundation
import ScreenCaptureKit

/// Owns exactly one native capture stream and one output file. The separate
/// Quill microphone/system tracks remain the transcription sources.
@MainActor
final class NativeScreenVideoRecorder: ScreenVideoSegmentCapture {
    var onFailure: ((String) -> Void)?
    /// Called once when a created stream has stopped or failed before capture.
    var onCleanup: (() -> Void)?
    private(set) var isRecording = false
    /// Remains true after Stop only if the writer and a complete video frame
    /// were both observed during this session.
    private(set) var didStart = false
    /// Delegate arrival time, which approximates the first written video frame.
    private(set) var startedAt: Date?
    private(set) var firstFrameHostSeconds: Double?
    /// Keep the session visible if a native failure leaves cleanup unconfirmed.
    var needsCleanup: Bool { stream != nil }

    private var stream: SCStream?
    private var output: SCRecordingOutput?
    private var delegate: ScreenVideoDelegate?
    private let sampleQueue = DispatchQueue(label: "com.digimata.quill.screen-samples")
    private var destination: URL?
    private var startTask: Task<Void, Error>?
    private var stopTask: Task<URL?, Error>?
    private var stopRequested = false
    private var failureReported = false
    private var nativeStartReturned = false
    private var nativeStartIssued = false
    private var nativeStopRequested = false
    private var nativeStopConfirmed = false
    private var stopWaitEnded = false
    private let captureStarted = ScreenCaptureRequest<Void>()
    private let captureStopped = ScreenCaptureRequest<Void>()
    private let startup = ScreenVideoStartup()
    private let finished = ScreenCaptureRequest<Void>()
    private let microphonePermission = ScreenMicrophonePermission()

    func start(target: ScreenCaptureTarget, writingTo url: URL,
               validateStart: (() throws -> Void)? = nil) async throws {
        guard startTask == nil else { throw ScreenVideoError.alreadyStarted }
        let task = Task { try await begin(target: target, writingTo: url, validateStart: validateStart) }
        startTask = task
        try await task.value
    }

    /// Returns only after SCRecordingOutput confirms the file is finalized.
    /// Repeated calls await the same owned stop operation.
    func stop() async throws -> URL? {
        if let stopTask { return try await stopTask.value }
        guard let startTask else { return nil }
        stopRequested = true
        if !isRecording {
            microphonePermission.cancel()
            captureStarted.resolve(.failure(.cancelled))
            startup.fail(.cancelled)
        }
        requestNativeStop()
        let task = Task<URL?, Error> {
            try await startTask.value
            return try await finishOwnedStream()
        }
        stopTask = task
        return try await task.value
    }

    private func begin(target: ScreenCaptureTarget, writingTo url: URL,
                       validateStart: (() throws -> Void)?) async throws {
        do {
            guard url.isFileURL && url.pathExtension.lowercased() == "mp4" else {
                throw ScreenVideoError.invalidDestination
            }
            guard (try? FileManager.default.attributesOfItem(atPath: url.path)) == nil else {
                throw ScreenVideoError.destinationExists
            }
            guard !stopRequested else { throw ScreenVideoError.cancelled }
            if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
                _ = try await microphonePermission.request { completion in
                    AVCaptureDevice.requestAccess(for: .audio, completionHandler: completion)
                }
            }
            guard !stopRequested else { throw ScreenVideoError.cancelled }
            guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
                throw ScreenVideoError.microphonePermissionRequired
            }
            let filter = try await ScreenCaptureCatalog.contentFilter(for: target)
            guard !stopRequested else { throw ScreenVideoError.cancelled }
            try validateStart?()

            let configuration = SCStreamConfiguration()
            let scale = Double(filter.pointPixelScale)
            let width = max(2, Double(filter.contentRect.width) * scale)
            let height = max(2, Double(filter.contentRect.height) * scale)
            let reduction = min(1, min(1920 / width, 1080 / height))
            configuration.width = max(2, Int(width * reduction) / 2 * 2)
            configuration.height = max(2, Int(height * reduction) / 2 * 2)
            configuration.minimumFrameInterval = CMTime(value: 1, timescale: 30)
            configuration.preservesAspectRatio = true
            configuration.scalesToFit = true
            // The native H.264 writer stalled after four frames with the
            // three-surface default on this Mac. Allow encoder buffering within
            // Apple's documented maximum; keep sample callbacks off MainActor.
            configuration.queueDepth = 8
            configuration.showsCursor = true
            configuration.ignoreShadowsSingleWindow = true
            configuration.ignoreGlobalClipSingleWindow = true
            configuration.includeChildWindows = false
            configuration.capturesAudio = true
            configuration.captureMicrophone = true
            configuration.excludesCurrentProcessAudio = true
            configuration.sampleRate = 48_000
            configuration.channelCount = 2
            configuration.streamName = "Quill meeting recording"

            let delegate = ScreenVideoDelegate { [weak self] event in
                Task { @MainActor [weak self] in self?.receive(event) }
            }
            let stream = SCStream(filter: filter, configuration: configuration, delegate: delegate)
            let recording = SCRecordingOutputConfiguration()
            recording.outputURL = url
            recording.videoCodecType = .h264
            recording.outputFileType = .mp4
            let output = SCRecordingOutput(configuration: recording, delegate: delegate)
            self.delegate = delegate
            self.stream = stream
            self.output = output
            destination = url

            // Apple requires adding the output before capture to retain the
            // first samples. It records screen, system audio, and microphone
            // according to the stream configuration (macOS 15 and later).
            nativeStartReturned = true
            // Consume and release sample callbacks on a dedicated queue. Keep
            // the capture surface queue moving while SCRecordingOutput encodes.
            try stream.addStreamOutput(delegate, type: .screen, sampleHandlerQueue: sampleQueue)
            try stream.addStreamOutput(delegate, type: .audio, sampleHandlerQueue: sampleQueue)
            try stream.addStreamOutput(delegate, type: .microphone, sampleHandlerQueue: sampleQueue)
            try stream.addRecordingOutput(output)
            nativeStartReturned = false
            Task { [self, stream] in
                do {
                    guard !stopRequested else { throw ScreenVideoError.cancelled }
                    try validateStart?()
                    nativeStartIssued = true
                    try await stream.startCapture()
                    nativeStartReturned = true
                    captureStarted.resolve(.success(()))
                    // Stop can arrive while the system is still starting. A
                    // late successful start must not leave capture running.
                    if stopRequested {
                        nativeStopConfirmed = false
                        requestNativeStop(allowAnother: true)
                    }
                } catch {
                    nativeStartReturned = true
                    captureStarted.resolve(.failure(.captureFailed(error.localizedDescription)))
                    if stopWaitEnded && nativeStopConfirmed { releaseOwnedObjects() }
                }
            }
            try await captureStarted.wait(seconds: 15, timeout: .startTimedOut)
            try await startup.wait()
            guard !stopRequested else { throw ScreenVideoError.cancelled }
            guard !finished.isResolved else {
                throw ScreenVideoError.captureFailed("The screen output ended while capture was starting.")
            }
            isRecording = true
            didStart = true
        } catch {
            stopRequested = true
            if !nativeStartIssued {
                nativeStopConfirmed = true
                releaseOwnedObjects()
            } else if stream != nil {
                requestNativeStop()
                try? await captureStopped.wait(seconds: 20, timeout: .finalizationTimedOut)
            }
            stopWaitEnded = true
            isRecording = !nativeStopConfirmed && startedAt != nil
            if nativeStartReturned && nativeStopConfirmed { releaseOwnedObjects() }
            throw error
        }
    }

    private func finishOwnedStream() async throws -> URL? {
        defer {
            stopWaitEnded = true
            isRecording = !nativeStopConfirmed && startedAt != nil
            if nativeStartReturned && nativeStopConfirmed { releaseOwnedObjects() }
        }
        guard stream != nil, let destination else { return nil }
        requestNativeStop()
        try await finished.wait(seconds: 20, timeout: .finalizationTimedOut)
        try await captureStopped.wait(seconds: 20, timeout: .finalizationTimedOut)
        let values = try destination.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
        guard values.isRegularFile == true, values.isSymbolicLink != true, (values.fileSize ?? 0) > 0 else {
            throw ScreenVideoError.emptyRecording
        }
        let asset = AVURLAsset(url: destination)
        let duration = try await asset.load(.duration).seconds
        let tracks = try await asset.loadTracks(withMediaType: .video)
        guard tracks.count == 1, let video = tracks.first else { throw ScreenVideoError.emptyRecording }
        let range = try await video.load(.timeRange)
        guard ScreenVideoIntegrity.coversRecording(duration: duration, videoStart: range.start.seconds,
                                                   videoDuration: range.duration.seconds) else {
            throw ScreenVideoError.captureFailed("The saved video ended early. Its partial file was preserved; audio remains available.")
        }
        return destination
    }

    private func requestNativeStop(allowAnother: Bool = false) {
        guard nativeStartIssued, let stream, !nativeStopRequested || allowAnother else { return }
        nativeStopRequested = true
        // Retain this recorder while the system call is pending. In particular,
        // a late start callback still has an owner that can stop its stream.
        Task { [self, stream] in
            do {
                try await stream.stopCapture()
                nativeStopConfirmed = true
                isRecording = false
                captureStopped.resolve(.success(()))
                if stopWaitEnded && nativeStartReturned { releaseOwnedObjects() }
            } catch {
                let failure = ScreenVideoError.captureFailed(error.localizedDescription)
                captureStopped.resolve(.failure(failure))
                if nativeStartReturned { finished.resolve(.failure(failure)) }
                if !nativeStopConfirmed { reportFailure("macOS has not confirmed screen capture stopped. Keep this session open and stop sharing from the macOS recording indicator if needed.") }
            }
        }
    }

    private func receive(_ event: ScreenVideoDelegate.Event) {
        switch event {
        case .started:
            startedAt = startedAt ?? Date()
            startup.outputStarted()
        case .videoFrame(let hostSeconds):
            firstFrameHostSeconds = hostSeconds
            startup.videoFrameReceived()
        case .finished:
            finished.resolve(.success(()))
            startup.fail(.captureFailed("The video output ended before screen capture was confirmed."))
            if isRecording && !stopRequested {
                reportFailure("Screen recording ended before the meeting recording stopped.")
                Task { try? await stop() }
            }
        case .failed(let message), .streamStopped(let message):
            if case .streamStopped = event {
                nativeStopConfirmed = true
                isRecording = false
                captureStopped.resolve(.success(()))
            }
            let error = ScreenVideoError.captureFailed(message)
            startup.fail(error)
            finished.resolve(.failure(error))
            reportFailure(error.localizedDescription)
            // If an earlier Stop already failed or timed out, it is cached in
            // stopTask. A later stop from the macOS sharing indicator must
            // release ownership here rather than waiting on that failed task.
            if case .streamStopped = event, stopWaitEnded && nativeStartReturned {
                releaseOwnedObjects()
            }
            Task { try? await stop() }
        }
    }

    private func reportFailure(_ message: String) {
        guard !failureReported else { return }
        failureReported = true
        onFailure?(message)
    }

    private func releaseOwnedObjects() {
        guard stream != nil else { return }
        output = nil
        stream = nil
        delegate = nil
        onCleanup?()
    }
}

/// The OS permission dialog is not cancellable. Our wait is: a late response
/// resolves only this settled gate and never resumes a cancelled recording.
@MainActor
final class ScreenMicrophonePermission {
    private let response = ScreenCaptureRequest<Bool>()
    private var requested = false

    func request(timeoutSeconds: UInt64 = 60,
                 using requestAccess: (@escaping @Sendable (Bool) -> Void) -> Void) async throws -> Bool {
        if !requested && !response.isResolved {
            requested = true
            requestAccess { [response] allowed in
                Task { @MainActor in response.resolve(.success(allowed)) }
            }
        }
        return try await response.wait(seconds: timeoutSeconds,
            timeout: .captureFailed("Microphone permission was not confirmed in time. Allow microphone access, then start a new recording."))
    }

    func cancel() { response.resolve(.failure(.cancelled)) }
}

/// Delegate callbacks may arrive on framework queues; only immutable event
/// values cross to the recorder's main actor.
private final class ScreenVideoDelegate: NSObject, SCStreamDelegate, SCStreamOutput, SCRecordingOutputDelegate, Sendable {
    enum Event: Sendable { case started, videoFrame(Double), finished, failed(String), streamStopped(String) }
    let handler: @Sendable (Event) -> Void
    private let firstFrame = FirstVideoFrame()

    init(handler: @escaping @Sendable (Event) -> Void) { self.handler = handler }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of outputType: SCStreamOutputType) {
        // A writer-start callback alone can precede usable video. Confirm one
        // complete screen frame without retaining or transferring its buffer.
        guard outputType == .screen, sampleBuffer.isValid,
              CMSampleBufferGetImageBuffer(sampleBuffer) != nil,
              let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false)
                as? [[SCStreamFrameInfo: Any]],
              let status = attachments.first?[.status] as? Int,
              status == SCFrameStatus.complete.rawValue,
              firstFrame.claim() else { return }
        let seconds = CMSampleBufferGetPresentationTimeStamp(sampleBuffer).seconds
        guard seconds.isFinite, seconds >= 0 else { return }
        handler(.videoFrame(seconds))
    }

    func recordingOutputDidStartRecording(_ recordingOutput: SCRecordingOutput) { handler(.started) }
    func recordingOutputDidFinishRecording(_ recordingOutput: SCRecordingOutput) { handler(.finished) }
    func recordingOutput(_ recordingOutput: SCRecordingOutput, didFailWithError error: any Error) {
        handler(.failed(error.localizedDescription))
    }
    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        handler(.streamStopped(error.localizedDescription))
    }
}

/// Callback queues can change; the first-frame notification stays one-shot.
private final class FirstVideoFrame: @unchecked Sendable {
    private let lock = NSLock()
    private var received = false
    func claim() -> Bool {
        lock.withLock {
            guard !received else { return false }
            received = true
            return true
        }
    }
}

/// Startup is successful only after both independent native signals arrive.
/// Cancellation, failure and timeout are terminal even if callbacks arrive late.
@MainActor
final class ScreenVideoStartup {
    private let ready = ScreenCaptureRequest<Void>()
    private var writerStarted = false
    private var receivedFrame = false
    private(set) var isReady = false

    func outputStarted() {
        guard !ready.isResolved else { return }
        writerStarted = true
        resolveIfReady()
    }

    func videoFrameReceived() {
        guard !ready.isResolved else { return }
        receivedFrame = true
        resolveIfReady()
    }

    func fail(_ error: ScreenVideoError) { ready.resolve(.failure(error)) }

    func wait(seconds: UInt64 = 15) async throws {
        try await ready.wait(seconds: seconds, timeout: .startTimedOut)
    }

    private func resolveIfReady() {
        guard writerStarted && receivedFrame else { return }
        isReady = true
        ready.resolve(.success(()))
    }
}

enum ScreenVideoIntegrity {
    static func coversRecording(duration: Double, videoStart: Double, videoDuration: Double) -> Bool {
        guard duration.isFinite, duration > 0, videoStart.isFinite, videoStart >= 0,
              videoDuration.isFinite, videoDuration > 0 else { return false }
        // Allow normal stream startup/end skew without accepting an audio-only
        // tail as a completed video. Static content may have very few samples.
        return videoStart <= 2 && videoStart + videoDuration >= duration - 2
    }
}
