import Foundation

/// One meeting recording: a timestamped folder holding two independent tracks
/// (mic = you, system = them) plus a meta.json written on clean stop. Tracks
/// are separate to preserve each audio source. They do not identify individual
/// speakers, including people sharing a microphone or the system track.
@MainActor
final class RecordingSession {
    let dir: URL
    let startedAt: Date
    let captureTimeline = RecordingCaptureTimeline()
    var isPaused: Bool { captureTimeline.isPaused }
    var activeDuration: TimeInterval { captureTimeline.activeDuration }
    var systemCaptureFailure: String? { system.captureFailure }
    var systemHasAudio: Bool { system.hasCapturedAudio }
    var systemAudioCoverageComplete: Bool { system.captureReport["coverage_complete"] as? Bool == true }
    var microphoneCoverageComplete: Bool { mic.captureReport["coverage_complete"] as? Bool == true }
    private let recordingTimeZone: TimeZone
    private let speakerIdentificationRequested: Bool

    private let mic = MicRecorder()
    private let system = SystemAudioRecorder()
    var onMicStatusChange: (@MainActor @Sendable (MicCaptureStatus) -> Void)? {
        get { mic.onStatusChange }
        set { mic.onStatusChange = newValue }
    }
    var onSystemStatusChange: (@MainActor @Sendable (SystemAudioCaptureStatus) -> Void)? {
        get { system.onStatusChange }
        set { system.onStatusChange = newValue }
    }
    private var endedAt: Date?
    private enum State { case ready, starting, recording, stopping, stopped }
    private var state = State.ready
    private var stopTask: Task<Void, Never>?

    /// Create the session folder under `root` (yyyy.MM.dd-HHmm, suffixed on
    /// collision) without starting capture yet.
    init(root: URL, startedAt: Date = Date(), timeZone: TimeZone = .current,
         speakerIdentificationRequested: Bool = false) throws {
        self.startedAt = startedAt
        self.speakerIdentificationRequested = speakerIdentificationRequested
        // Freeze location at the start. Ending, processing or uploading after
        // travel must not change this meeting's calendar date.
        recordingTimeZone = TimeZone(identifier: timeZone.identifier) ?? timeZone
        let folderFormat = DateFormatter()
        folderFormat.locale = Locale(identifier: "en_US_POSIX")
        folderFormat.calendar = Calendar(identifier: .gregorian)
        folderFormat.timeZone = recordingTimeZone
        folderFormat.dateFormat = "yyyy.MM.dd-HHmm"
        let base = folderFormat.string(from: startedAt)
        var candidate = root.appendingPathComponent(base, isDirectory: true)
        var n = 2
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = root.appendingPathComponent("\(base)-\(n)", isDirectory: true)
            n += 1
        }
        try FileManager.default.createDirectory(at: candidate, withIntermediateDirectories: true)
        dir = candidate
    }

    /// Start both tracks. If the mic fails after the system tap started, the
    /// tap is torn down so we never run half a session silently.
    func start(validateStart: (() throws -> Void)? = nil) async throws {
        guard state == .ready else { throw CancellationError() }
        state = .starting
        mic.captureTimeline = captureTimeline
        system.captureTimeline = captureTimeline
        do {
            try Task.checkCancellation()
            try await system.start(writingTo: dir.appendingPathComponent("system.caf"))
            try Task.checkCancellation()
            // Stop may have been requested while the system permission prompt
            // was open. Recheck ownership on the main actor before starting mic.
            guard state == .starting else { throw CancellationError() }
            try validateStart?()
            try mic.start(writingTo: dir.appendingPathComponent("mic.caf"))
            state = .recording
        } catch {
            await stopTracks()
            throw error
        }
    }

    /// Pausing keeps the same files and session. Only Stop publishes metadata
    /// or schedules post-processing. The shared clock cuts all captured tracks.
    @discardableResult
    func pause() -> Bool {
        guard state == .recording else { return false }
        return captureTimeline.pause()
    }

    @discardableResult
    func resume() -> Bool {
        guard state == .recording else { return false }
        return captureTimeline.resume()
    }

    /// Invalidate a pending start and stop microphone capture before returning.
    /// Sleep notification handlers call this before scheduling asynchronous
    /// cleanup. The first request fixes the meeting's end time, so time asleep
    /// or waiting for native cleanup never extends the recorded duration.
    func requestStop(at date: Date = Date()) {
        guard endedAt == nil else { return }
        endedAt = date
        captureTimeline.stop()
        state = .stopping
        // Fence callbacks and queued recovery before waiting for native teardown.
        // A permission dialog or route restart cannot revive this session.
        system.requestStop()
        mic.stop()
    }

    /// Concurrent callers await the same cleanup, including a stop requested
    /// while system start is still pending. Mic control stays on the main actor.
    func stopTracks() async {
        requestStop()
        if let stopTask {
            await stopTask.value
            return
        }
        guard state != .stopped else { return }
        let task = Task { @MainActor in
            await self.system.stop()
            self.state = .stopped
        }
        stopTask = task
        await task.value
    }

    /// Only publish completed metadata after every owned output has finalized.
    func finalize(screen: URL? = nil, screenStartedAt: Date? = nil, screenError: String? = nil,
                  transcriptionRequested: Bool = false, screenRequested: Bool = false,
                  screenStarted: Bool = false) async throws {
        await stopTracks()
        let meta = try metadata(screen: screen, screenStartedAt: screenStartedAt, screenError: screenError,
                                transcriptionRequested: transcriptionRequested, screenRequested: screenRequested,
                                screenStarted: screenStarted, checkpoint: false)
        let data = try JSONSerialization.data(withJSONObject: meta, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: dir.appendingPathComponent("meta.json"), options: .atomic)
    }

    /// A bounded checkpoint describes this capture, not a completed meeting.
    /// The crash journal holds it separately until media recovery and review.
    func recoveryMetadata(transcriptionRequested: Bool, screenRequested: Bool, screenStarted: Bool,
                          screenStartedAt: Date? = nil, screenError: String? = nil) throws -> [String: Any] {
        try metadata(screen: nil, screenStartedAt: screenStartedAt, screenError: screenError,
                     transcriptionRequested: transcriptionRequested, screenRequested: screenRequested,
                     screenStarted: screenStarted, checkpoint: true)
    }

    private func metadata(screen: URL?, screenStartedAt: Date?, screenError: String?,
                          transcriptionRequested: Bool, screenRequested: Bool,
                          screenStarted: Bool, checkpoint: Bool) throws -> [String: Any] {
        let ended = endedAt ?? Date()
        let iso = ISO8601DateFormatter()

        // The tracks don't start on the same buffer; record how far each
        // lags the earliest so transcript timestamps share one clock.
        let micStart = mic.firstBufferAt ?? startedAt
        let systemStart = system.firstBufferAt ?? startedAt
        let earliest = min(micStart, systemStart)

        var files = ["mic": "mic.caf", "system": "system.caf"]
        if let screen {
            guard screen.deletingLastPathComponent().standardizedFileURL == dir.standardizedFileURL else {
                throw CocoaError(.fileWriteInvalidFileName)
            }
            files["screen"] = screen.lastPathComponent
        }
        var offsets = ["mic": Int(micStart.timeIntervalSince(earliest) * 1000),
                       "system": Int(systemStart.timeIntervalSince(earliest) * 1000)]
        if let screenStartedAt { offsets["screen"] = Int(screenStartedAt.timeIntervalSince(earliest) * 1000) }
        let micReport = mic.captureReport
        let systemReport = system.captureReport
        var meta: [String: Any] = [
            "started": iso.string(from: startedAt),
            "recording_timezone": recordingTimeZone.identifier,
            "recording_utc_offset_seconds": recordingTimeZone.secondsFromGMT(for: startedAt),
            "ended": iso.string(from: ended),
            "duration_seconds": checkpoint || captureTimeline.hasPauses ? Int(activeDuration) : Int(ended.timeIntervalSince(startedAt)),
            "wall_duration_seconds": checkpoint ? Int(activeDuration + captureTimeline.pausedDuration) : Int(ended.timeIntervalSince(startedAt)),
            "paused_duration_seconds": captureTimeline.pausedDuration,
            "files": files,
            "capture_storage_format": "pcm_s16",
            "start_offset_ms": offsets,
            "mic_capture": micReport,
            "system_capture": systemReport,
            "transcription_requested": transcriptionRequested,
            "speaker_identification_requested": transcriptionRequested && speakerIdentificationRequested,
            "screen_recording_requested": screenRequested,
            "screen_recording_started": screenStarted,
        ]
        // Track timestamps stay relative to the first audio buffer. Recovery
        // also needs the monotonic startup delay to reconstruct the wall end.
        let captureOrigins = [micReport, systemReport].compactMap { report -> Double? in
            guard let origin = report["capture_start_offset_seconds"] as? Double,
                  origin.isFinite, origin >= 0 else { return nil }
            return origin
        }
        if let origin = captureOrigins.min() { meta["capture_start_offset_seconds"] = origin }
        if let screenError { meta["screen_error"] = screenError }
        return meta
    }
}
