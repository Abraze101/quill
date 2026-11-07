import Foundation
import MeetingWorkflow

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
    private var meetingContext: MeetingContext?
    private var speakerTimeline: MeetingSpeakerTimeline?
    private var speakerSignalOwner: (source: String, meeting: String)?
    private let activityWallAnchor: TimeInterval
    private let activityMonotonicAnchor: TimeInterval
    private var activityClockReliable = true
    private var savedSpeakerActivity = false

    func observeMeetingContext(_ context: MeetingContext) {
        guard endedAt == nil, let context = context.validated() else { return }
        meetingContext = meetingContext?.merging(context) ?? context
    }

    func observeMeetingSignal(_ signal: MeetingSignal, at date: Date = Date(),
                              monotonicNow: TimeInterval = RecordingCaptureTimeline.now) {
        guard endedAt == nil, signal.state == .joined,
              let context = signal.meetingContext?.validated(), context.provider == signal.provider else { return }
        if let owner = speakerSignalOwner,
           owner.source != signal.source || owner.meeting != signal.meeting { return }
        if let previous = meetingContext,
           previous.provider != context.provider || previous.key != context.key { return }
        speakerSignalOwner = (signal.source, signal.meeting)
        observeMeetingContext(context)
        guard speakerIdentificationRequested, activityClockReliable else { return }
        guard let activity = signal.speakerActivity else {
            speakerTimeline?.breakContinuity()
            return
        }
        if speakerTimeline == nil { speakerTimeline = MeetingSpeakerTimeline(provider: context.provider, key: context.key) }
        // Browser timestamps use wall time; audio uses a monotonic clock. A
        // wall-clock jump invalidates later alignment instead of moving labels.
        let expectedWall = activityWallAnchor + monotonicNow - activityMonotonicAnchor
        guard monotonicNow.isFinite, abs(date.timeIntervalSince1970 - expectedWall) <= 1 else {
            activityClockReliable = false
            speakerTimeline?.stopCollecting()
            return
        }
        speakerTimeline?.observe(activity, receivedAt: date.timeIntervalSince1970)
    }

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
        activityWallAnchor = Date().timeIntervalSince1970
        activityMonotonicAnchor = RecordingCaptureTimeline.now
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
        saveSpeakerActivity()
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
        if let meetingContext {
            meta["meeting_context"] = try JSONSerialization.jsonObject(with: JSONEncoder().encode(meetingContext))
        }
        if !checkpoint, savedSpeakerActivity { meta["speaker_activity_file"] = "speaker-activity.json" }
        return meta
    }

    private func saveSpeakerActivity() {
        guard speakerIdentificationRequested, let speakerTimeline,
              let micStart = mic.firstBufferAt, let systemStart = system.firstBufferAt else { return }
        let origins = [mic.captureReport, system.captureReport].compactMap { report -> Double? in
            guard let value = report["capture_start_offset_seconds"] as? Double,
                  value.isFinite, value >= 0 else { return nil }
            return value
        }
        // Metadata/transcription uses the earliest firstBufferAt date. If a
        // track never produced a buffer it falls back to startedAt; do not
        // attach a different clock to that incomplete transcript. Require both
        // measured origins and map the exact same date used by metadata.
        guard origins.count == 2 else { return }
        let earliest = min(micStart, systemStart).timeIntervalSince1970
        let base = activityMonotonicAnchor + earliest - activityWallAnchor
        guard base.isFinite, abs(base - captureTimeline.startedAt - origins.min()!) <= 0.1 else { return }
        let durationMS = Int64(max(0, (captureTimeline.startedAt + captureTimeline.activeDuration - base) * 1000).rounded(.down))
        let saved = speakerTimeline.saved { interval in
            let start = activityMonotonicAnchor + Double(interval.startMS) / 1000 - activityWallAnchor
            let end = activityMonotonicAnchor + Double(interval.endMS) / 1000 - activityWallAnchor
            return captureTimeline.activeSlices(from: start, to: end).compactMap { slice in
                let startMS = max(0, Int64(((slice.outputStart - base) * 1000).rounded(.up)))
                let endMS = min(durationMS, Int64(((slice.outputStart + slice.sourceEnd - slice.sourceStart - base) * 1000).rounded(.down)))
                guard endMS > startMS else { return nil }
                return MeetingSpeakerTimeline.Interval(startMS: startMS, endMS: endMS, active: interval.active)
            }
        }
        guard let saved, let data = try? JSONEncoder().encode(saved), data.count <= 5_000_000 else { return }
        do {
            try data.write(to: dir.appendingPathComponent("speaker-activity.json"), options: .atomic)
            savedSpeakerActivity = true
        } catch {
            // Recording outputs and completion remain usable without naming evidence.
            savedSpeakerActivity = false
        }
    }
}
