import AppKit
import CoreGraphics
import Foundation
import MeetingWorkflow

/// All menu actions and capture transitions are serialized on the main actor.
/// A screen recorder belongs to this session only; OBS is never controlled.
@MainActor
final class AppController {
    private enum Phase { case idle, starting, recording, pausing, paused, resuming, stopping, attention }
    private let root: URL
    private let menuBar = MenuBarController()
    private let settings = SettingsWindowController()
    private let loginItem = LoginItemController()
    private let transcription = TranscriptionCoordinator()
    private let meetingAutomation = MeetingAutomationController()
    private let recordingControls = RecordingStatusController()
    private let meetingReminder = MeetingReminderController()
    private let recordingHeartbeat = RecordingStatusHeartbeat()
    private var meetingWatchdog = MeetingRecordingWatchdog()
    private var uncoveredMeeting: String?
    private var safety: RecordingSafety?
    private var pendingStartID: UUID?
    private var safetyReasons: [String] = []
    private var safetyStartedAt: TimeInterval?
    private var stoppedElapsed: TimeInterval?
    private var localReviews: [URL] = []
    private var sleepController: RecordingSleepController?
    private var terminationAllowed = false
    private var dockVisible: Bool?
    private var phase: Phase = .idle
    private var recordingActionRevision = 1
    private var hasActiveRecording: Bool {
        [.recording, .pausing, .paused, .resuming].contains(phase)
    }
    private var canChangePause: Bool {
        (phase == .recording || phase == .paused) && session != nil
            && screenFinishTask == nil && (!requestedScreenVideo || screenError == nil)
    }
    private var session: RecordingSession?
    private var captureLease: RecordingCrashRecovery.CaptureLease?
    private var automaticMeeting: String?
    private var captureMeeting: String?
    private var captureSource: String?
    private var browserWindowID: Int?
    private var audioStarted = false
    private var requestedScreenVideo = false
    private var cancelStart = false
    private var screenRecorder: ScreenVideoRecorder?
    private var screenURL: URL?
    private var screenError: String?
    private var screenFinishTask: Task<Void, Never>?
    private var operationTask: Task<Void, Never>?
    private var ticker: Timer?
    private var shuttingDown = false
    private var transcriptionRetryInFlight = false
    private var completionWorker: Process?
    private var completionWorkerID: UUID?
    private var completionRecovery = CompletionWorkerRecovery()
    private var completionLaunchIssue: String?
    private var reviewProcess: Process?
    private var completionStatus: CompletionUIStatus?
    private var completionStatusChecked = Date.distantPast
    private var micStatus: MicCaptureStatus = .recording
    private var micInterruptionNotified = false
    private var systemAudioStatus: SystemAudioCaptureStatus = .recording
    private var systemInterruptionNotified = false
    private var captureChoices: [ScreenCaptureChoice] = []
    private var captureLoading = false
    private var captureDetail: String?

    init(root: URL) {
        self.root = root
        menuBar.onToggle = { [weak self] in self?.toggle() }
        menuBar.onStopRecording = { [weak self] id, revision in self?.stopDisplayedRecording(id, revision: revision) }
        menuBar.onPauseRecording = { [weak self] id, revision, paused in
            self?.pauseDisplayedRecording(id, revision: revision, expectedPaused: paused)
        }
        menuBar.onOpenFolder = { [weak self] in self?.openFolder() }
        menuBar.onRetryTranscriptions = { [weak self] in self?.retryTranscriptions() }
        menuBar.onQuit = { [weak self] in self?.shutdown() }
        menuBar.onSettings = { [weak self] in self?.showSettings() }
        menuBar.onShowControls = { [weak self] in self?.showRecordingControls() }
        recordingControls.onSettings = { [weak self] in self?.showSettings() }
        recordingControls.onOpenFolder = { [weak self] _ in self?.openReviewFolder() }
        recordingControls.onStop = { [weak self] id, revision in self?.stopDisplayedRecording(id, revision: revision) }
        recordingControls.onPause = { [weak self] id, revision, paused in
            self?.pauseDisplayedRecording(id, revision: revision, expectedPaused: paused)
        }
        recordingControls.onContinue = { [weak self] id, revision in
            self?.extendRecording(sessionID: id, revision: revision)
        }
        recordingControls.onKeepLocal = { [weak self] id in self?.reviewRecording(sessionID: id, process: false) }
        recordingControls.onProcess = { [weak self] id in self?.reviewRecording(sessionID: id, process: true) }
        meetingReminder.onStart = { [weak self] meeting in self?.startFromMeetingReminder(meeting) }
        meetingReminder.onSkip = { [weak self] meeting in
            guard let self, uncoveredMeeting == meeting, phase == .idle,
                  meetingAutomation.signal(for: meeting) != nil else { return }
            meetingAutomation.skip(meeting: meeting)
            refreshUI()
        }
        settings.onCheckInHoursChange = { [weak self] hours in
            self?.savePreference { try Config.setRecordingCheckInHours(hours) }
        }
        settings.onShowInDockChange = { [weak self] enabled in
            self?.savePreference { try Config.setShowInDock(enabled) }
        }
        menuBar.onReviewMeetings = { [weak self] in self?.reviewMeetings() }
        settings.onReviewProcessing = { [weak self] in self?.reviewMeetings() }
        menuBar.onAutomaticRecordingChange = { [weak self] enabled in
            self?.savePreference { try Config.setMeetingAutomationEnabled(enabled) }
        }
        menuBar.onIncludeScreenVideoChange = { [weak self] enabled in self?.setScreenVideo(enabled, revealSettings: enabled) }
        settings.onToggleRecording = { [weak self] in self?.toggle() }
        settings.onStopRecording = { [weak self] id, revision in self?.stopDisplayedRecording(id, revision: revision) }
        settings.onPauseRecording = { [weak self] id, revision, paused in
            self?.pauseDisplayedRecording(id, revision: revision, expectedPaused: paused)
        }
        settings.onIncludeScreenVideoChange = { [weak self] enabled in self?.setScreenVideo(enabled) }
        settings.onLaunchAtLoginChange = { [weak self] enabled in
            guard let self else { return }
            loginItem.setEnabled(enabled)
            refreshUI()
        }
        settings.onOpenLoginSettings = { [weak self] in self?.loginItem.openSystemSettings() }
        menuBar.onSkipMeeting = { [weak self] in
            guard let self else { return }
            meetingAutomation.skipCurrent()
            if phase == .starting, automaticMeeting != nil { cancelPendingStart() }
            refreshUI()
        }
        menuBar.onRetryMeeting = { [weak self] meeting in
            guard let self, phase == .idle, !shuttingDown else { return }
            meetingAutomation.retryFailedStart(meeting: meeting)
            refreshUI()
        }
        meetingAutomation.onStart = { [weak self] meeting in self?.requestStart(meeting: meeting) }
        meetingAutomation.onStop = { [weak self] id in
            guard let self, session?.dir.path == id, automaticMeeting != nil else { return }
            requestStop(manually: false)
        }
        meetingAutomation.onStatus = { [weak self] _ in self?.refreshUI() }
        settings.onCaptureScopeChange = { [weak self] scope in
            self?.savePreference { try Config.setScreenCaptureScope(scope == .entireScreen ? "display" : "window") }
        }
        settings.onCaptureTargetChange = { [weak self] target in
            self?.savePreference { try Config.setScreenCaptureSelection(target) }
        }
        settings.onRefreshCaptureTargets = { [weak self] in self?.refreshCaptureTargets(requestPermission: true) }
        settings.onTranscriptionChange = { [weak self] enabled in
            self?.savePreference {
                if !enabled, (try? WorkflowPreferences(url: Config.workflowSettingsURL()))?.notesActive == true {
                    throw WorkflowPreferences.PreferenceError("Turn off meeting notes before disabling local transcription.")
                }
                try Config.setTranscriptionEnabled(enabled)
            }
        }
        settings.onSpeakerIdentificationChange = { [weak self] enabled in
            self?.savePreference { try Config.setSpeakerIdentificationEnabled(enabled) }
        }
        settings.onSetupConnection = { [weak self] _ in
            self?.openWorkflowConfiguration()
        }
        settings.onSetupNotes = { [weak self] in
            self?.openWorkflowConfiguration()
        }
        settings.onNotesChange = { [weak self] enabled in
            self?.savePreference {
                try WorkflowPreferences.setNotes(enabled, at: Config.workflowSettingsURL(),
                    transcriptionEnabled: Config.transcriptionEnabled())
            }
        }
        settings.onDriveDocumentsChange = { [weak self] enabled in
            self?.savePreference {
                try WorkflowPreferences.setDriveDocuments(enabled, at: Config.workflowSettingsURL(),
                    transcriptionEnabled: Config.transcriptionEnabled())
            }
        }
        settings.onDriveRecordingsChange = { [weak self] enabled in
            self?.savePreference {
                try WorkflowPreferences.setDriveRecordings(enabled, at: Config.workflowSettingsURL(),
                    transcriptionEnabled: Config.transcriptionEnabled())
            }
        }
        ticker = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        if let ticker { RunLoop.main.add(ticker, forMode: .common) }
        sleepController = RecordingSleepController(
            onSleep: { [weak self] in self?.prepareForSystemSleep() },
            onWake: { [weak self] in self?.reconcileSystemWake() })
        // RunInstanceLock is held before this controller is constructed. The
        // per-session lease additionally prevents touching a live capture.
        let recovery = RecordingCrashRecovery.recoverPending(root: root)
        if recovery.recovered > 0 {
            notifyUser(title: "Quill — interrupted recording recovered",
                       body: "Saved audio is ready for local review. Transcription, notes and Drive filing wait for your decision.")
        }
        reloadLocalReviews()
        refreshUI()
        resumeCompletionWorker()
        Task { [transcription, root] in
            await transcription.setStatusHandler { status in
                Task { @MainActor [weak self] in self?.showTranscription(status) }
            }
            await transcription.resumePending(root: root)
        }
    }

    func shutdown() {
        guard !shuttingDown else { return }
        shuttingDown = true
        meetingAutomation.stopPolling()
        switch phase {
        case .idle: finishShutdown()
        case .starting: cancelPendingStart()
        case .recording, .pausing, .paused, .resuming: requestStop(manually: true)
        case .stopping: break
        case .attention: showCaptureAttention()
        }
    }

    /// Dock Quit and Cmd-Q must not bypass asynchronous capture finalization.
    /// Cancel this termination request; finishShutdown issues a new one only
    /// after the owned recorders confirm shutdown and metadata is handled.
    func shouldTerminate() -> NSApplication.TerminateReply {
        if terminationAllowed { return .terminateNow }
        if phase == .idle {
            recordingHeartbeat.invalidate()
            meetingWatchdog.reset()
            meetingReminder.update(meetingID: nil)
            terminationAllowed = true
            return .terminateNow
        }
        shutdown()
        return .terminateCancel
    }

    private func finishShutdown() {
        recordingHeartbeat.invalidate()
        meetingReminder.update(meetingID: nil)
        sleepController?.stopObserving()
        terminationAllowed = true
        NSApp.terminate(nil)
    }

    private func prepareForSystemSleep() {
        meetingWatchdog.reset()
        uncoveredMeeting = nil
        meetingReminder.update(meetingID: nil)
        recordingHeartbeat.invalidate()
        guard phase != .idle else { return }
        // Persist the hold and stop the mic before yielding to native cleanup.
        // Even if the process sleeps first, restart recovery stays fail-closed.
        let reason = "The Mac went to sleep. This recording needs review before processing. Start a new recording after waking."
        addSafetyReason(reason)
        if stoppedElapsed == nil { stoppedElapsed = elapsedInterval }
        session?.requestStop()
        switch phase {
        case .starting: cancelPendingStart()
        case .recording, .pausing, .paused, .resuming: requestStop(manually: true, safetyReason: reason)
        case .stopping, .attention:
            // A normal Stop may already be waiting on native cleanup. Keep its
            // sleep hold and suppress the exact still-joined automatic meeting.
            meetingAutomation.skipCurrent()
        case .idle: break
        }
        refreshUI()
    }

    private func reconcileSystemWake() {
        // Never restart capture here. Pending native cleanup owns its session;
        // finished or interrupted captures remain visible in local review.
        reloadLocalReviews()
        tick()
        if phase != .idle || !localReviews.isEmpty { recordingControls.show() }
    }

    func reopen() {
        if phase != .idle || !localReviews.isEmpty { showRecordingControls() }
        else { showSettings() }
    }

    func restoreRecordingVisibility() {
        guard phase != .idle else { return }
        NSApp.unhideWithoutActivation()
        recordingControls.show()
    }

    func showRecordingControls() {
        refreshUI()
        recordingControls.show()
    }

    private func toggle() {
        switch phase {
        case .idle: if !shuttingDown { requestStart() }
        case .starting: cancelPendingStart()
        case .recording, .pausing, .paused, .resuming: requestStop(manually: true)
        case .stopping: break
        case .attention: showCaptureAttention()
        }
    }

    private func stopDisplayedRecording(_ id: String, revision: Int?) {
        if phase == .starting, safety == nil, pendingStartID?.uuidString == id,
           revision == nil || revision == 1 {
            cancelPendingStart()
            return
        }
        guard safety?.sessionID.uuidString == id,
              revision == nil || recordingActionRevision == revision else { return }
        toggle()
    }

    /// UI actions bind to the displayed session and pause state. A late click
    /// can never resume a stopped session or toggle the next recording.
    private func pauseDisplayedRecording(_ id: String, revision: Int, expectedPaused: Bool) {
        guard safety?.sessionID.uuidString == id, revision == recordingActionRevision,
              canChangePause, phase == (expectedPaused ? .paused : .recording),
              let ownedSession = session else { return }
        tick()
        guard hasActiveRecording, safety?.stage != .expired, session === ownedSession,
              canChangePause else { return }
        recordingActionRevision += 1
        if !expectedPaused {
            guard ownedSession.pause() else { return }
            phase = .pausing
        } else { phase = .resuming }
        do { try checkpointCapture() }
        catch {
            requestStop(manually: true, safetyReason: "The recording recovery checkpoint could not be saved.")
            return
        }
        refreshUI()
        let ownedScreen = screenRecorder
        operationTask = Task {
            guard session === ownedSession, phase == (expectedPaused ? .resuming : .pausing) else { return }
            do {
                if expectedPaused {
                    try validateResume(ownedSession)
                    if requestedScreenVideo {
                        guard let recorder = ownedScreen else { throw ScreenVideoError.cancelled }
                        try await recorder.resume(validateStart: { [weak self, weak ownedSession] in
                            guard let self, let ownedSession else { throw ScreenVideoError.cancelled }
                            try self.validateResume(ownedSession)
                        })
                    }
                    try validateResume(ownedSession)
                    guard ownedSession.resume() else { throw ScreenVideoError.cancelled }
                    try checkpointCapture()
                    phase = .recording
                } else {
                    if requestedScreenVideo {
                        guard let recorder = ownedScreen else { throw ScreenVideoError.cancelled }
                        try await recorder.pause()
                    }
                    guard session === ownedSession, phase == .pausing else { return }
                    try checkpointCapture()
                    phase = .paused
                }
                refreshUI()
            } catch {
                // Stop/sleep/leave supersedes a transition. Its cleanup owns
                // the session now; never turn a late callback into a restart.
                guard session === ownedSession, phase == .pausing || phase == .resuming else { return }
                let message = "Recording could not \(expectedPaused ? "resume" : "pause") safely. Saved files need review. \(error.localizedDescription)"
                if requestedScreenVideo { screenError = message }
                requestStop(manually: true, safetyReason: message)
                notifyUser(title: "Quill — recording saved for review", body: message)
            }
        }
    }

    private func validateResume(_ ownedSession: RecordingSession) throws {
        guard session === ownedSession, phase == .resuming, !shuttingDown,
              sleepController?.isSleeping != true, screenFinishTask == nil else { throw ScreenVideoError.cancelled }
        if let safety, RecordingSafetyClock.now >= safety.stopAt { throw ScreenVideoError.cancelled }
        if let automaticMeeting, meetingAutomation.signal(for: automaticMeeting) == nil {
            throw CaptureSelectionError("The meeting is no longer confirmed.")
        }
        if let captureMeeting, let captureSource, let browserWindowID,
           meetingAutomation.windowContinuity(meeting: captureMeeting, source: captureSource,
                                              windowID: browserWindowID) != .keepRecording {
            throw CaptureSelectionError("The meeting window is no longer confirmed.")
        }
    }

    private func requestStart(meeting: String? = nil) {
        guard phase == .idle, !shuttingDown, sleepController?.isSleeping != true else {
            if let meeting { meetingAutomation.startFailed(meeting: meeting, retryable: sleepController?.isSleeping != true) }
            return
        }
        if let meeting, hasUnfinishedReview {
            // Restart recovery never silently opens a new recording for the
            // same still-joined call. Review/keep local first, then explicitly
            // retry the suppressed meeting or leave and join a new one.
            meetingAutomation.startFailed(meeting: meeting)
            screenError = "Review the interrupted recording before restarting automatic capture."
            refreshUI()
            recordingControls.show()
            return
        }
        phase = .starting
        recordingActionRevision = 1
        pendingStartID = UUID()
        automaticMeeting = meeting
        cancelStart = false
        screenError = nil
        safetyReasons = []
        stoppedElapsed = nil
        requestedScreenVideo = Config.screenVideoEnabled()
        refreshUI()
        operationTask = Task { await beginSession() }
    }

    private func beginSession() async {
        do {
            let target = requestedScreenVideo ? try await resolveCaptureTarget() : nil
            try validatePendingStart()
            let newSession = try RecordingSession(root: root,
                                                 speakerIdentificationRequested: Config.speakerIdentificationEnabled())
            session = newSession
            let now = RecordingSafetyClock.now
            safetyStartedAt = now
            safety = RecordingSafety(sessionID: pendingStartID ?? UUID(), owner: automaticMeeting == nil ? .manual : .automatic,
                                     now: now, startedAt: newSession.startedAt,
                                     checkInAfter: TimeInterval(Config.recordingCheckInHours() * 3600))
            try persistSafety()
            guard let safety else { throw CancellationError() }
            captureLease = try RecordingCrashRecovery.begin(session: newSession.dir, sessionID: safety.sessionID,
                metadata: newSession.recoveryMetadata(transcriptionRequested: Config.transcriptionEnabled(),
                    screenRequested: requestedScreenVideo, screenStarted: false))
            refreshUI()
            newSession.onMicStatusChange = { [weak self, weak newSession] status in
                guard let self, let newSession, self.session === newSession,
                      phase == .starting || hasActiveRecording else { return }
                micStatus = status
                switch status {
                case .recording:
                    micInterruptionNotified = false
                    do { try checkpointCapture() }
                    catch { stopForSafety("The microphone recovery checkpoint could not be saved.") }
                case .recovering:
                    if !micInterruptionNotified {
                        micInterruptionNotified = true
                        notifyUser(title: "quill — microphone interrupted",
                            body: "Quill is reconnecting the microphone. Audio already saved is preserved; the interruption will be marked in the recording.")
                    }
                case .failed(let message):
                    addSafetyReason(message)
                    notifyUser(title: "quill — microphone needs attention", body: message)
                }
                refreshUI()
            }
            newSession.onSystemStatusChange = { [weak self, weak newSession] status in
                guard let self, let newSession, self.session === newSession,
                      phase == .starting || hasActiveRecording else { return }
                systemAudioStatus = status
                switch status {
                case .recording:
                    systemInterruptionNotified = false
                    do { try checkpointCapture() }
                    catch { stopForSafety("The system-audio recovery checkpoint could not be saved.") }
                case .recovering:
                    // Persist before Stop/sleep can finish this session. Fresh
                    // audio later changes the display, not this review hold.
                    addSafetyReason("System audio was interrupted or incomplete.")
                    if !systemInterruptionNotified {
                        systemInterruptionNotified = true
                        notifyUser(title: "Quill — system audio interrupted",
                            body: "Quill is reconnecting system audio. Saved audio is preserved. Review this recording before processing.")
                    }
                case .failed(let message):
                    addSafetyReason("System audio was interrupted or incomplete.")
                    addSafetyReason(message)
                    notifyUser(title: "Quill — system audio needs attention", body: message)
                }
                refreshUI()
            }
            if let target {
                let recorder = ScreenVideoRecorder()
                screenRecorder = recorder
                recorder.onFailure = { [weak self, weak newSession] message in
                    guard let self, let newSession, self.session === newSession else { return }
                    screenError = message
                    addSafetyReason(message)
                    notifyUser(title: "quill — screen recording needs attention", body: message)
                    if phase == .recording { stopScreenOnly() }
                    else if hasActiveRecording { requestStop(manually: true, safetyReason: message) }
                    refreshUI()
                }
                recorder.onCleanup = { [weak self, weak newSession] in
                    guard let self, let newSession, self.session === newSession, phase == .attention else { return }
                    phase = .stopping
                    operationTask = Task { await completeSession() }
                }
                try await recorder.start(target: target, writingTo: newSession.dir.appendingPathComponent("screen.mp4"),
                    timeline: newSession.captureTimeline, validateStart: { [weak self] in
                        guard let self else { throw ScreenVideoError.cancelled }
                        try self.validatePendingStart()
                    })
            }
            try validatePendingStart()
            try await newSession.start(validateStart: { [weak self] in
                guard let self else { throw ScreenVideoError.cancelled }
                try self.validatePendingStart()
            })
            try validatePendingStart()
            if requestedScreenVideo && screenRecorder?.isRecording != true {
                throw CaptureSelectionError("Screen video could not be confirmed. Recording was cancelled. Check the selected window or display and try again.")
            }
            audioStarted = true
            try checkpointCapture()
            phase = .recording
            meetingAutomation.recordingStarted(id: newSession.dir.path, meeting: automaticMeeting)
            log("● recording → \(newSession.dir.path)")
            refreshUI()
        } catch {
            if let meeting = automaticMeeting { meetingAutomation.startFailed(meeting: meeting, retryable: !cancelStart) }
            async let stoppedAudio: Void? = session?.stopTracks()
            if let recorder = screenRecorder { _ = try? await recorder.stop() }
            _ = await stoppedAudio
            if !cancelStart {
                screenError = error.localizedDescription
                notifyUser(title: "quill — recording did not start", body: error.localizedDescription)
                log("recording start failed: \(error)")
            }
            if screenRecorder?.needsCleanup == true {
                phase = .attention
                refreshUI()
            } else {
                if let session, let safety {
                    try? RecordingReviewGate.finish(session: session.dir, sessionID: safety.sessionID,
                                                   reasons: ["Recording did not finish starting."])
                }
                reloadLocalReviews()
                resetSession()
            }
        }
    }

    private func validatePendingStart() throws {
        guard !cancelStart, !shuttingDown, sleepController?.isSleeping != true else { throw ScreenVideoError.cancelled }
        if let automaticMeeting, !meetingAutomation.mayStart(meeting: automaticMeeting) {
            throw CaptureSelectionError("The meeting is no longer confirmed. Start recording again when you are ready.")
        }
        if let captureMeeting, let browserWindowID,
           meetingAutomation.signal(for: captureMeeting)?.browserWindow?.id != browserWindowID {
            throw CaptureSelectionError("The meeting window changed while recording was starting. Choose it again.")
        }
    }

    private func cancelPendingStart() {
        guard phase == .starting else { return }
        cancelStart = true
        session?.requestStop()
        meetingAutomation.skipCurrent()
        if let meeting = automaticMeeting { meetingAutomation.startFailed(meeting: meeting, retryable: false) }
        if let recorder = screenRecorder { Task { _ = try? await recorder.stop() } }
        refreshUI()
    }

    private func requestStop(manually: Bool, safetyReason: String? = nil) {
        guard hasActiveRecording else { return }
        // A click can arrive before a delayed timer callback. Reconcile the
        // deadline here too, so an overdue recording cannot take the clean path.
        _ = safety?.tick(now: RecordingSafetyClock.now)
        if let safetyReason { addSafetyReason(safetyReason) }
        else if safety?.stage == .warning || safety?.stage == .expired {
            addSafetyReason("Recording stopped after the unattended check-in. Review before processing.")
        }
        stoppedElapsed = elapsedInterval
        phase = .stopping
        session?.requestStop()
        do { try checkpointCapture() }
        catch { addSafetyReason("The final recording recovery checkpoint could not be saved.") }
        // Suppress at click time, before video saving can delay completion and
        // a different meeting can appear. The owned machine record stays busy.
        if manually { meetingAutomation.skipCurrent() }
        refreshUI()
        operationTask = Task {
            async let stoppedAudio: Void? = session?.stopTracks()
            await finishScreen()
            _ = await stoppedAudio
            if screenRecorder?.needsCleanup == true {
                phase = .attention
                refreshUI()
            } else { await completeSession() }
        }
    }

    private func stopScreenOnly() {
        guard screenFinishTask == nil, let recorder = screenRecorder else { return }
        screenFinishTask = Task {
            do { screenURL = try await recorder.stop() }
            catch { screenError = screenError ?? error.localizedDescription }
            refreshUI()
        }
    }

    private func finishScreen() async {
        if let screenFinishTask { await screenFinishTask.value; return }
        guard let recorder = screenRecorder else { return }
        do { screenURL = try await recorder.stop() }
        catch { screenError = screenError ?? error.localizedDescription }
    }

    private func completeSession() async {
        guard let session, audioStarted else { resetSession(); return }
        do {
            try await session.finalize(screen: screenURL, screenStartedAt: screenURL == nil ? nil : screenRecorder?.startedAt,
                                 screenError: screenError, transcriptionRequested: Config.transcriptionEnabled(),
                                 screenRequested: requestedScreenVideo, screenStarted: screenRecorder?.didStart == true)
            if let screenError { addSafetyReason(screenError) }
            if !session.microphoneCoverageComplete { addSafetyReason("Microphone audio was interrupted or incomplete.") }
            if !session.systemAudioCoverageComplete { addSafetyReason("System audio was interrupted or incomplete.") }
            if let issue = session.systemCaptureFailure { addSafetyReason(issue) }
            if !session.systemHasAudio { addSafetyReason("No system audio buffers were captured.") }
            if let safety {
                try RecordingReviewGate.finish(session: session.dir, sessionID: safety.sessionID, reasons: safetyReasons)
            }
            // Retain the crash manifest until both media and its review state
            // are durable. A stale manifest is never a second completion hook.
            do { try captureLease?.complete() }
            catch { log("recovery checkpoint cleanup deferred: \(error)") }
            captureLease = nil
            self.safety?.finish(now: RecordingSafetyClock.now)
            if RecordingReviewGate.blocksProcessing(session: session.dir) {
                notifyUser(title: "Quill — recording saved for review",
                           body: "The files are saved locally. Review them in Quill before transcription, notes, or Drive filing.")
            } else { await queueProcessing(session.dir) }
            log("○ stopped · \(session.dir.path)")
        } catch {
            notifyUser(title: "quill — recording could not finish saving",
                body: "The local files were preserved. Processing will wait until the saved recording is repaired.")
            log("recording finalization failed: \(error)")
        }
        meetingAutomation.recordingStopped(id: session.dir.path, manually: false)
        reloadLocalReviews()
        resetSession()
    }

    private func resetSession() {
        // Releasing the lease preserves an unfinished manifest for next launch.
        captureLease = nil
        session = nil
        screenRecorder = nil
        screenFinishTask = nil
        screenURL = nil
        automaticMeeting = nil
        captureMeeting = nil
        captureSource = nil
        browserWindowID = nil
        audioStarted = false
        requestedScreenVideo = false
        micStatus = .recording
        micInterruptionNotified = false
        systemAudioStatus = .recording
        systemInterruptionNotified = false
        cancelStart = false
        safety = nil
        pendingStartID = nil
        safetyStartedAt = nil
        stoppedElapsed = nil
        safetyReasons = []
        phase = .idle
        reloadLocalReviews()
        refreshUI()
        if shuttingDown { finishShutdown() }
    }

    private func resolveCaptureTarget() async throws -> ScreenCaptureTarget {
        let choices = try await ScreenCaptureCatalog.availableChoices()
        captureChoices = choices
        let selected = Config.screenCaptureSelection()
        if Config.screenCaptureScope() == "display" {
            guard let choice = choices.first(where: { identifier($0.target) == selected }),
                  case .display = choice.target else {
                throw CaptureSelectionError("Choose a display in Settings before recording the entire screen.")
            }
            return choice.target
        }
        // Chrome window IDs and CGWindowIDs are not interchangeable. Bounds
        // may corroborate an explicit selection, but cannot authorize one.
        guard selected != "automatic" else {
            throw CaptureSelectionError("Choose the meeting's Chrome window in Settings before including screen video. Automatic window selection is not enabled yet.")
        }
        if automaticMeeting == nil {
            guard let choice = choices.first(where: { identifier($0.target) == selected }),
                  case .window = choice.target else { throw ScreenVideoError.targetUnavailable }
            return choice.target
        }
        let joined = meetingAutomation.joinedSignals()
        let signal: MeetingSignal?
        if let automaticMeeting { signal = joined.first { $0.meeting == automaticMeeting } }
        else { signal = Set(joined.map(\.meeting)).count == 1 ? joined.first : nil }
        let candidates = choices.compactMap { choice -> MeetingWindowMatcher.Candidate? in
            guard case .window = choice.target else { return nil }
            let b = choice.bounds
            return .init(identifier: identifier(choice.target), left: b.left, top: b.top, width: b.width, height: b.height)
        }
        guard let signal, let window = signal.browserWindow else {
            throw CaptureSelectionError("The meeting window is not confirmed. Keep the meeting tab selected in its Chrome window and try again.")
        }
        let matched = MeetingWindowMatcher.uniqueIdentifier(for: window, candidates: candidates)
        if matched == nil, let selectedCandidate = candidates.first(where: { $0.identifier == selected }),
           MeetingWindowMatcher.uniqueIdentifier(for: window, candidates: [selectedCandidate]) != nil {
            throw CaptureSelectionError("Several Chrome windows have the same size and position. Resize or move the meeting window, then leave and rejoin the meeting to retry automatic recording.")
        }
        guard let matched,
              selected == matched,
              let choice = choices.first(where: { identifier($0.target) == matched }) else {
            throw CaptureSelectionError("Quill could not identify one meeting window. Keep the meeting in its own Chrome window, or choose a window in Settings for a manual recording.")
        }
        captureMeeting = signal.meeting
        captureSource = signal.source
        browserWindowID = window.id
        return choice.target
    }

    private func tick() {
        if !shuttingDown {
            let now = RecordingSafetyClock.now
            if completionWorker?.isRunning == true { completionRecovery.healthy(now: now) }
            else if completionRecovery.shouldRestart(now: now) { resumeCompletionWorker() }
        }
        if hasActiveRecording || phase == .starting {
            do { try checkpointCapture() }
            catch { stopForSafety("Recording recovery checkpoints could not be saved. Review this recording locally.") }
            if let issue = session?.systemCaptureFailure { addSafetyReason(issue) }
            if let event = safety?.tick(now: RecordingSafetyClock.now) {
                do { try persistSafety() }
                catch {
                    stopForSafety("Recording safeguards could not be saved. Review this recording locally.")
                }
                switch event {
                case .warning:
                    notifyUser(title: session?.isPaused == true ? "Quill — paused session still open?" : "Quill — still recording?",
                               body: "Choose Continue for 1 hour or Stop & save. Quill will stop in five minutes if you do not respond.")
                    recordingControls.show()
                case .stop:
                    stopForSafety("The unattended recording check-in expired.")
                }
            }
        }
        if phase == .starting, !cancelStart, let automaticMeeting,
           !meetingAutomation.mayStart(meeting: automaticMeeting) {
            cancelPendingStart()
        }
        if hasActiveRecording, screenFinishTask == nil,
           let captureMeeting, let captureSource, let browserWindowID {
            switch meetingAutomation.windowContinuity(meeting: captureMeeting, source: captureSource,
                                                       windowID: browserWindowID) {
            case .keepRecording: break
            case .meetingLeft:
                // Stop video immediately on leave without misreporting it as
                // a capture failure. Audio follows the normal leave debounce.
                stopScreenOnly()
            case .windowUnavailable:
                if phase == .recording {
                    screenError = "The meeting window is no longer confirmed. Screen video stopped; audio continues."
                    addSafetyReason(screenError!)
                    stopScreenOnly()
                    notifyUser(title: "quill — screen video stopped", body: screenError!)
                } else {
                    let reason = "The meeting window is no longer confirmed. The paused recording was saved for review."
                    screenError = reason
                    requestStop(manually: true, safetyReason: reason)
                }
            }
        }
        refreshUI()
    }

    private func refreshUI() {
        updateMeetingReminder()
        if Date().timeIntervalSince(completionStatusChecked) >= 5 {
            completionStatus = CompletionUIStatus(settingsURL: Config.workflowSettingsURL(), statusURL: Config.completionStatusURL())
            completionStatusChecked = Date()
            notifyCompletionIssueIfNeeded()
        }
        var options = MenuBarOptions()
        options.pendingReviews = completionStatus?.pendingReviews ?? 0
        options.reviewAvailable = Config.meetingReviewCommand() != nil
        options.processingTitle = completionLaunchIssue == nil ? completionStatus?.processingTitle
            : completionRecovery.exhausted ? "Processing needs attention" : "Restarting meeting processing"
        options.processingDetail = completionLaunchIssue ?? completionStatus?.processingDetail ?? ""
        options.processingNeedsAttention = completionLaunchIssue != nil || completionStatus?.needsAttention == true
        options.recordAutomatically = Config.meetingAutomationEnabled()
        options.includeScreenVideo = phase == .idle ? Config.screenVideoEnabled() : requestedScreenVideo
        options.recordingActionTitle = options.includeScreenVideo ? "Start audio + screen" : "Start audio recording"
        options.recordingActionEnabled = !shuttingDown && sleepController?.isSleeping != true
        options.canSkipMeeting = meetingAutomation.canSkipMeeting
        options.failedMeetingToRetry = phase == .idle ? meetingAutomation.failedMeetingToRetry : nil
        options.configurationEnabled = phase == .idle || phase == .recording || phase == .paused
        options.transcriptionRetryEnabled = canRetryTranscriptions
        options.transcriptionRetryInFlight = transcriptionRetryInFlight
        switch meetingAutomation.status {
        case .disabled: options.automationStatus = "Automatic recording is off"
        case .waiting: options.automationStatus = "Waiting for a meeting"
        case .joined: options.automationStatus = "Meeting detected"
        case .skipped: options.automationStatus = "This meeting is skipped"
        case .signalLost: options.automationStatus = "Meeting state unavailable"
        }
        switch phase {
        case .idle:
            if screenError != nil { options.stateOverride = "Recording needs attention · open Settings" }
            if uncoveredMeeting != nil { options.stateOverride = "Meeting is not recording" }
        case .starting:
            options.stateOverride = cancelStart ? "Cancelling recording…"
                : requestedScreenVideo ? "Starting audio + screen · waiting for video…" : "Starting audio recording…"
            options.recordingActionTitle = "Cancel recording"
            options.recordingActionEnabled = !cancelStart
        case .recording:
            options.recordingActionTitle = "Stop recording"
            let elapsed = Self.format(elapsedInterval)
            let videoActive = screenRecorder?.isRecording == true && screenFinishTask == nil && screenError == nil
            let media = videoActive ? "audio + screen" : "audio only"
            options.stateOverride = "Recording \(media) · \(elapsed)"
            if requestedScreenVideo && !videoActive {
                options.stateOverride = "Recording audio only · screen video stopped · \(elapsed)"
            }
            if case .recovering = micStatus { options.stateOverride! += " · reconnecting microphone…" }
            else if case .failed = micStatus { options.stateOverride! += " · microphone needs attention" }
            if case .recovering = systemAudioStatus { options.stateOverride! += " · reconnecting system audio…" }
            else if case .failed = systemAudioStatus { options.stateOverride! += " · system audio needs attention" }
            if automaticMeeting != nil && meetingAutomation.status == .signalLost {
                options.stateOverride! += " · meeting state unavailable"
            }
        case .pausing, .paused, .resuming:
            let label = phase == .paused ? "Paused" : phase == .pausing ? "Pausing…" : "Resuming…"
            options.stateOverride = "\(label) · \(Self.format(elapsedInterval))"
            options.recordingActionTitle = "Stop & save"
        case .stopping:
            options.stateOverride = "Saving recording…"
            options.recordingActionTitle = "Saving recording…"
            options.recordingActionEnabled = false
        case .attention:
            options.stateOverride = "Screen capture needs attention"
            options.recordingActionTitle = "Check recording status…"
            options.recordingActionEnabled = true
        }
        menuBar.update(recording: hasActiveRecording || screenRecorder?.isRecording == true
            || (phase == .attention && screenRecorder?.needsCleanup == true),
            elapsed: session == nil ? nil : Self.format(elapsedInterval))
        menuBar.updateOptions(options)
        var state = QuillSettingsState()
        state.loginItem = loginItem.snapshot()
        state.includeScreenVideo = phase == .idle ? options.includeScreenVideo : requestedScreenVideo
        state.recordingStatus = options.stateOverride ?? (options.includeScreenVideo ? "Ready · audio + screen" : "Ready · audio only")
        state.recordingActionTitle = options.recordingActionTitle ?? "Start audio recording"
        state.recordingActionEnabled = options.recordingActionEnabled ?? true
        state.recordingNeedsAttention = screenError != nil || phase == .attention || uncoveredMeeting != nil
        state.recordingIssue = screenError ?? (uncoveredMeeting == nil ? nil : "You are in a meeting. Start recording or skip this meeting.")
        state.captureScope = Config.screenCaptureScope() == "display" ? .entireScreen : .meetingWindow
        state.captureTargets = captureChoices.filter {
            switch $0.target {
            case .window: return state.captureScope == .meetingWindow
            case .display: return state.captureScope == .entireScreen
            }
        }.map { .init(identifier: identifier($0.target), label: $0.title) }
        if state.captureScope == .meetingWindow {
            state.captureTargets.insert(.init(identifier: "automatic", label: "Choose meeting window…"), at: 0)
        }
        state.selectedCaptureTargetID = Config.screenCaptureSelection()
        state.captureTargetsLoading = captureLoading
        state.captureTargetDetail = captureDetail
        state.localTranscription = Config.transcriptionEnabled()
        state.identifySpeakers = Config.speakerIdentificationEnabled()
        state.notesStatus = completionStatus?.notes ?? state.notesStatus
        state.processingTitle = options.processingTitle ?? "No processing status yet"
        state.processingDetail = options.processingDetail
        state.processingNeedsAttention = options.processingNeedsAttention
        state.processingReviewAvailable = options.reviewAvailable
        if let preferences = completionStatus?.preferences {
            state.notesActive = preferences.notesActive
            state.canEnableNotes = preferences.notesConfigured && state.localTranscription
            state.driveDocumentsActive = preferences.driveDocumentsEnabled
            state.driveRecordingsActive = preferences.driveRecordingsEnabled
            state.driveDocumentsTitle = preferences.driveDocumentsTitle
            state.driveRecordingsTitle = preferences.driveRecordingsTitle
            state.driveDocumentsStatus = preferences.driveDocumentsDescription
            state.driveRecordingsStatus = preferences.driveRecordingsDescription
            let canFile = preferences.notesConfigured && preferences.notesEnabled && state.localTranscription
            state.canEnableDriveDocuments = preferences.driveDocumentsConfigured && canFile
            state.canEnableDriveRecordings = preferences.driveRecordingsConfigured && canFile
        }
        state.isRecording = phase != .idle
        state.isPaused = phase == .paused
        state.pauseEnabled = canChangePause
        state.pauseActionTitle = phase == .pausing ? "Pausing…" : phase == .resuming ? "Resuming…" : nil
        state.sessionID = safety?.sessionID.uuidString ?? pendingStartID?.uuidString
        state.recordingRevision = safety == nil ? (pendingStartID == nil ? nil : 1) : recordingActionRevision
        state.checkInHours = Config.recordingCheckInHours()
        state.showInDock = Config.showInDock()
        settings.update(state)
        updateRecordingControls()
    }

    func showSettings() {
        refreshUI()
        settings.show()
        if phase == .idle, Config.screenVideoEnabled() { refreshCaptureTargets(requestPermission: false) }
    }

    private func setScreenVideo(_ enabled: Bool, revealSettings: Bool = false) {
        guard phase == .idle else { return }
        savePreference { try Config.setScreenVideoEnabled(enabled) }
        if revealSettings { showSettings() }
        else if enabled { refreshCaptureTargets(requestPermission: false) }
    }

    private func refreshCaptureTargets(requestPermission: Bool) {
        guard !captureLoading, phase == .idle else { return }
        if !requestPermission && !CGPreflightScreenCaptureAccess() {
            captureDetail = "Screen access is needed to list windows. Click Refresh to request it, then allow Quill in System Settings."
            refreshUI()
            return
        }
        captureLoading = true
        captureDetail = nil
        refreshUI()
        Task {
            do { captureChoices = try await ScreenCaptureCatalog.availableChoices(requestPermission: requestPermission) }
            catch { captureDetail = error.localizedDescription }
            captureLoading = false
            refreshUI()
        }
    }

    private func savePreference(_ change: () throws -> Void) {
        do { try change() }
        catch { notifyUser(title: "quill — setting was not saved", body: error.localizedDescription) }
        completionStatusChecked = .distantPast
        refreshUI()
    }

    private func openWorkflowConfiguration() {
        let url = Config.workflowSettingsURL()
        if FileManager.default.fileExists(atPath: url.path) {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        } else {
            showInformation("Completion workflow configuration",
                "The completion workflow has not been configured. Its settings file belongs at \(url.path). Use the meeting workflow setup guide to configure Call Summary and your company folders.")
        }
    }

    private func showCaptureAttention() {
        showInformation("Screen capture needs attention",
            "macOS has not confirmed that this recording stopped. Use the macOS recording indicator to stop Quill sharing. Quill will finish saving when capture ends.")
    }

    private func showInformation(_ title: String, _ message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        if let window = settings.window, window.isVisible { alert.beginSheetModal(for: window) }
        else { NSApp.activate(ignoringOtherApps: true); alert.runModal() }
    }

    private var elapsedInterval: TimeInterval {
        if let stoppedElapsed { return stoppedElapsed }
        return session?.activeDuration ?? safetyStartedAt.map { max(0, RecordingSafetyClock.now - $0) } ?? 0
    }

    private func checkpointCapture() throws {
        guard let session, let captureLease else { return }
        try captureLease.checkpoint(metadata: session.recoveryMetadata(
            transcriptionRequested: Config.transcriptionEnabled(), screenRequested: requestedScreenVideo,
            screenStarted: screenRecorder?.didStart == true, screenStartedAt: screenRecorder?.startedAt,
            screenError: screenError))
    }

    private func persistSafety() throws {
        guard let session, let safety else { return }
        try RecordingReviewGate.write(safety.gateRecord, session: session.dir)
    }

    private func addSafetyReason(_ reason: String) {
        guard !safetyReasons.contains(reason) else { return }
        safetyReasons.append(reason)
        guard let session, let safety else { return }
        do { try RecordingReviewGate.hold(session: session.dir, sessionID: safety.sessionID, reason: reason) }
        catch { log("recording safety hold could not be saved: \(error)") }
        // Until finalization the existing journal remains active, so even a
        // disk-write failure here cannot release it for processing on restart.
    }

    private func extendRecording(sessionID: String, revision: Int) {
        guard hasActiveRecording, let id = UUID(uuidString: sessionID),
              safety?.extend(token: .init(sessionID: id, revision: revision), now: RecordingSafetyClock.now) == true else {
            tick()
            return
        }
        recordingActionRevision += 1
        do { try persistSafety() }
        catch { requestStop(manually: true, safetyReason: "The extended recording deadline could not be saved.") }
        refreshUI()
    }

    private func stopForSafety(_ reason: String) {
        if phase == .starting {
            addSafetyReason(reason)
            cancelPendingStart()
        } else { requestStop(manually: true, safetyReason: reason) }
    }

    private func reloadLocalReviews() {
        let keptRecoveryFolders = Set(UserDefaults.standard.stringArray(forKey: "QuillKeptLocalRecoveryFolders") ?? [])
        localReviews = RecordingReviewGate.pendingSessions(root: root).filter {
            $0.resolvingSymlinksInPath().standardizedFileURL != session?.dir.resolvingSymlinksInPath().standardizedFileURL
                && !keptRecoveryFolders.contains($0.standardizedFileURL.path)
        }
    }

    private var hasUnfinishedReview: Bool {
        localReviews.contains { dir in
            guard let record = try? RecordingReviewGate.read(session: dir) else { return true }
            return record.phase == .active || (record.reviewStatus == .pending
                && record.reasons.contains(RecordingCrashRecovery.recoveryReason))
        }
    }

    private func openReviewFolder() {
        if let dir = session?.dir ?? localReviews.first { NSWorkspace.shared.activateFileViewerSelecting([dir]) }
        else { openFolder() }
    }

    private func reviewRecording(sessionID: String, process: Bool) {
        guard phase == .idle, let dir = localReviews.first else { return }
        // A damaged journal cannot be approved, but the user can dismiss its
        // recovery reminder while leaving every processing guard intact.
        if !process, sessionID == dir.path {
            var kept = Set(UserDefaults.standard.stringArray(forKey: "QuillKeptLocalRecoveryFolders") ?? [])
            kept.insert(dir.standardizedFileURL.path)
            UserDefaults.standard.set(Array(kept).sorted(), forKey: "QuillKeptLocalRecoveryFolders")
            reloadLocalReviews()
            refreshUI()
            return
        }
        do {
            guard let record = try RecordingReviewGate.read(session: dir), record.sessionID.uuidString == sessionID else { return }
            try RecordingReviewGate.decide(session: dir, sessionID: record.sessionID,
                                          status: process ? .approved : .keptLocal)
            reloadLocalReviews()
            refreshUI()
            if process {
                Task {
                    await queueProcessing(dir)
                    // A previously delivered hook stays deduplicated. Wake its
                    // existing worker job after releasing a durable safety hold.
                    resumeCompletionWorker()
                }
            }
        } catch {
            showInformation("Recording stays local", "Quill could not apply that review decision. Open the recording folder to inspect the saved files. \(error.localizedDescription)")
        }
    }

    private func queueProcessing(_ dir: URL) async {
        guard let lease = try? RecordingReviewGate.acquireProcessingLease(session: dir) else { return }
        defer { withExtendedLifetime(lease) {} }
        guard !RecordingReviewGate.blocksProcessing(session: dir) else { return }
        var completion: String?
        if let command = Config.onStop() {
            do {
                completion = try CompletionJournal().register(session: dir, command: command,
                    waitingForTranscript: Config.transcriptionEnabled(), maxAttempts: Config.onStopMaxAttempts())
            } catch {
                notifyUser(title: "quill — completion handoff failed",
                    body: "The recording is saved locally. Its completion handoff needs attention.")
                log("completion registration failed: \(error)")
            }
        }
        await transcription.enqueue(dir, completion: completion)
    }

    private func startFromMeetingReminder(_ meeting: String) {
        guard uncoveredMeeting == meeting, !shuttingDown,
              sleepController?.isSleeping != true,
              meetingAutomation.signal(for: meeting) != nil,
              !meetingAutomation.deliberatelySkipped.contains(meeting) else { return }
        guard phase == .idle, !hasUnfinishedReview else {
            showRecordingControls()
            return
        }
        if Config.meetingAutomationEnabled(), meetingAutomation.hasFailedStart(meeting: meeting) {
            meetingAutomation.retryFailedStart(meeting: meeting)
        } else {
            // With automatic capture off, this explicit button is an ordinary
            // manual start. It does not change the user's global preference.
            requestStart(meeting: Config.meetingAutomationEnabled() ? meeting : nil)
        }
    }

    private func updateMeetingReminder() {
        guard !shuttingDown, sleepController?.isSleeping != true else {
            meetingWatchdog.reset()
            uncoveredMeeting = nil
            meetingReminder.update(meetingID: nil)
            recordingHeartbeat.invalidate()
            return
        }
        let capture: MeetingRecordingWatchdog.CaptureState
        switch phase {
        case .idle: capture = .idle
        case .starting, .resuming: capture = .starting
        case .recording: capture = .recording
        case .paused, .pausing: capture = .paused
        case .stopping, .attention: capture = .busy
        }
        let now = Date().timeIntervalSince1970
        let skipped = meetingAutomation.deliberatelySkipped
        let signals = meetingAutomation.reminderSignals()
        let currentMeetingIDs = Set(signals.filter { $0.state == .joined && $0.timestamp.isFinite
            && now - $0.timestamp <= 20 && $0.timestamp <= now + 5
            && ["google-meet", "zoom-web"].contains($0.provider) }
            .map(\.meeting))
        let phaseName: String
        switch capture {
        case .idle: phaseName = "idle"
        case .starting: phaseName = "starting"
        case .recording: phaseName = "recording"
        case .paused: phaseName = "paused"
        case .busy: phaseName = "busy"
        }
        recordingHeartbeat.publish(phase: phaseName,
            deliberatelySkipped: skipped.intersection(currentMeetingIDs), now: now)
        let warning = meetingWatchdog.evaluate(signals: signals,
            capture: capture, deliberatelySkipped: skipped, now: now)
        uncoveredMeeting = warning?.meetingID
        let detail: String
        if hasUnfinishedReview {
            detail = "This call is not being recorded. Review the interrupted recording before starting another."
        } else if phase == .starting || phase == .resuming {
            detail = "Quill has not finished starting. Check its recording controls and any permission request."
        } else if phase != .idle {
            detail = "Quill has not confirmed a recording for this call. Check the recording controls."
        } else if !Config.meetingAutomationEnabled() {
            detail = "Automatic recording is off. Start this recording, or skip this meeting."
        } else {
            detail = "You are in a meeting, but Quill has not started recording. Start it now, or skip this meeting."
        }
        meetingReminder.update(meetingID: warning?.meetingID,
            canStart: phase == .idle && !hasUnfinishedReview, canSkip: phase == .idle, detail: detail)
        if warning?.notify == true {
            notifyUser(title: "Quill — meeting is not recording", body: detail)
        }
    }

    private func updateRecordingControls() {
        let showDock = Config.showInDock()
        if dockVisible != showDock {
            NSApp.setActivationPolicy(showDock ? .regular : .accessory)
            dockVisible = showDock
        }
        var controlPhase = RecordingStatusSnapshot.Phase.idle
        var controlSessionID: String?
        let elapsed = Self.format(elapsedInterval)
        var microphoneStatus = "Off"
        var systemAudioStatus = "Off"
        var videoStatus = "Off"
        var detail: String?
        var warningRevision: Int?
        var countdownSeconds: Int?
        var stopEnabled = false
        var processEnabled = true
        let captureNeedsAttention = !safetyReasons.isEmpty || micStatus != .recording || self.systemAudioStatus != .recording
            || (automaticMeeting != nil && meetingAutomation.status == .signalLost)
        controlSessionID = safety?.sessionID.uuidString ?? pendingStartID?.uuidString
        detail = screenError
        switch phase {
        case .idle:
            controlPhase = .idle
            if let dir = localReviews.first {
                controlPhase = .review
                if let record = try? RecordingReviewGate.read(session: dir) {
                    controlSessionID = record.sessionID.uuidString
                    let reasons = record.reasons.filter { $0 != "unattended_timeout" }
                    detail = "\(dir.lastPathComponent)\n" + (reasons.isEmpty
                        ? "This recording was interrupted. Review the saved files before processing."
                        : reasons.joined(separator: " "))
                    processEnabled = RecordingReviewGate.hasFinalizedMetadata(session: dir)
                    if !processEnabled {
                        detail! += " The recording did not finish saving. Keep it local until its media has been recovered."
                    }
                } else {
                    controlSessionID = dir.path
                    processEnabled = false
                    detail = "\(dir.lastPathComponent) needs recovery. Open the folder to inspect the saved recording."
                }
            }
        case .starting:
            controlPhase = .starting
            stopEnabled = !cancelStart && controlSessionID != nil
            detail = (safetyReasons.isEmpty ? "" : safetyReasons.joined(separator: " ") + " ")
                + (cancelStart ? "Cancelling capture…" : "Waiting for capture to start.")
        case .recording:
            controlPhase = .recording
            stopEnabled = true
            switch micStatus {
            case .recording: microphoneStatus = "Recording"
            case .recovering: microphoneStatus = "Reconnecting"
            case .failed: microphoneStatus = "Unavailable"
            }
            switch self.systemAudioStatus {
            case .recording:
                systemAudioStatus = session?.systemHasAudio == true ? "Recording" : "Waiting for audio"
            case .recovering: systemAudioStatus = "Reconnecting"
            case .failed: systemAudioStatus = "Unavailable"
            }
            let videoActive = screenRecorder?.isRecording == true && screenFinishTask == nil && screenError == nil
            videoStatus = !requestedScreenVideo ? "Off" : videoActive
                ? (Config.screenCaptureScope() == "display" ? "Entire display" : "Meeting window") : "Stopped"
            if automaticMeeting != nil && meetingAutomation.status == .signalLost {
                detail = "Meeting detection is unavailable. Recording continues; use Stop & save if the meeting has ended."
            }
            if !safetyReasons.isEmpty { detail = safetyReasons.joined(separator: " ") }
            if case .recovering = micStatus { detail = "Microphone interrupted. Quill is reconnecting it." }
            if case .recovering = self.systemAudioStatus {
                detail = micStatus == .recording
                    ? "System audio interrupted. Quill is reconnecting it. Review will be required before processing."
                    : "Audio capture needs attention. Quill is reconnecting system audio. Review will be required before processing."
            }
            if let safety, safety.stage == .warning {
                warningRevision = safety.revision
                countdownSeconds = Int(ceil(safety.remainingResponse(now: RecordingSafetyClock.now)))
            }
        case .pausing, .paused, .resuming:
            controlPhase = phase == .paused ? .paused : phase == .pausing ? .pausing : .resuming
            stopEnabled = true
            microphoneStatus = "Paused"
            systemAudioStatus = "Paused"
            videoStatus = requestedScreenVideo ? (phase == .paused ? "Paused" : phase == .pausing ? "Stopping" : "Starting") : "Off"
            detail = "Paused time is left out of the saved recording. Resume when ready, or Stop & save."
            if !safetyReasons.isEmpty { detail = safetyReasons.joined(separator: " ") }
            if case .recovering = micStatus { microphoneStatus = "Reconnecting" }
            if case .failed = micStatus { microphoneStatus = "Unavailable" }
            if case .recovering = self.systemAudioStatus { systemAudioStatus = "Reconnecting" }
            if case .failed = self.systemAudioStatus { systemAudioStatus = "Unavailable" }
            if let safety, safety.stage == .warning {
                warningRevision = safety.revision
                countdownSeconds = Int(ceil(safety.remainingResponse(now: RecordingSafetyClock.now)))
            }
        case .stopping:
            controlPhase = .stopping
            detail = (safetyReasons.isEmpty ? "" : safetyReasons.joined(separator: " ") + " ")
                + "Finishing the saved recording. Keep Quill open."
            microphoneStatus = "Stopped"
            systemAudioStatus = "Stopping"
            videoStatus = requestedScreenVideo ? "Saving" : "Off"
        case .attention:
            controlPhase = .attention
            stopEnabled = true
            detail = (safetyReasons.isEmpty ? "" : safetyReasons.joined(separator: " ") + " ")
                + "macOS has not confirmed that screen capture stopped. Open recording status for help."
            microphoneStatus = "Stopped"
            systemAudioStatus = "Stopped"
            videoStatus = "Stop unconfirmed"
        }
        let snapshot = RecordingStatusSnapshot(phase: controlPhase, sessionID: controlSessionID,
            elapsed: elapsed, microphoneStatus: microphoneStatus, systemAudioStatus: systemAudioStatus,
            videoStatus: videoStatus, detail: detail, warningRevision: warningRevision,
            actionRevision: safety == nil ? (pendingStartID == nil ? nil : 1) : recordingActionRevision, countdownSeconds: countdownSeconds, pendingReviews: localReviews.count,
            stopEnabled: stopEnabled, processEnabled: processEnabled, captureNeedsAttention: captureNeedsAttention,
            keepLocalEnabled: true, pauseEnabled: canChangePause)
        recordingControls.update(snapshot)
        menuBar.updateStatus(snapshot)
    }

    private func resumeCompletionWorker() {
        guard !shuttingDown, completionWorker?.isRunning != true,
              let command = Config.completionWorkerCommand() else { return }
        let process = Process()
        let identifier = UUID()
        completionWorkerID = identifier
        process.executableURL = URL(fileURLWithPath: command[0])
        process.arguments = Array(command.dropFirst())
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] child in
            let status = child.terminationStatus
            Task { @MainActor in self?.completionWorkerExited(identifier: identifier, status: status) }
        }
        do {
            try process.run()
            completionWorker = process
            completionRecovery.started(now: RecordingSafetyClock.now)
            completionLaunchIssue = nil
        } catch {
            completionWorkerExited(identifier: identifier, status: 1)
        }
    }

    private func completionWorkerExited(identifier: UUID, status: Int32) {
        guard completionWorkerID == identifier else { return }
        completionWorker = nil
        completionWorkerID = nil
        guard !shuttingDown else { return }
        completionRecovery.exited(status: status, now: RecordingSafetyClock.now)
        if status != 0 {
            completionLaunchIssue = completionRecovery.exhausted
                ? "Automatic recovery stopped after three retries. Open Review processing to inspect the problem. Recordings remain saved locally."
                : "The background processor stopped. Quill will retry automatically. Recordings remain saved locally."
            if completionRecovery.exhausted {
                notifyUser(title: "Quill — processing needs attention", body: completionLaunchIssue!)
            }
        } else { completionLaunchIssue = nil }
        refreshUI()
    }

    private func notifyCompletionIssueIfNeeded() {
        guard completionStatus?.needsAttention == true,
              let revision = completionStatus?.alertRevision else { return }
        let key = "QuillProcessingNotifiedRevisions"
        var saved = UserDefaults.standard.stringArray(forKey: key) ?? []
        guard !saved.contains(revision) else { return }
        saved.append(revision)
        UserDefaults.standard.set(Array(saved.suffix(50)), forKey: key)
        notifyUser(title: "Quill — processing needs attention",
                   body: (completionStatus?.attentionDetail ?? "") + " Open Review processing in Quill.")
    }

    private func reviewMeetings() {
        guard let command = Config.meetingReviewCommand() else { return }
        if completionRecovery.exhausted {
            completionRecovery = CompletionWorkerRecovery()
            resumeCompletionWorker()
        }
        if reviewProcess?.isRunning == true {
            showInformation("Meeting review", "The review page is already open in your browser. It stays available for 30 minutes; your choices are saved locally.")
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: command[0])
        process.arguments = Array(command.dropFirst())
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run(); reviewProcess = process }
        catch { showInformation("Meeting review unavailable", "The review page could not open. Your meetings remain saved locally.") }
    }

    var canRetryTranscriptions: Bool {
        !shuttingDown && sleepController?.isSleeping != true
            && !transcriptionRetryInFlight && Config.transcriptionEnabled()
    }

    func retryTranscriptions() {
        guard !shuttingDown, sleepController?.isSleeping != true, !transcriptionRetryInFlight else { return }
        guard Config.transcriptionEnabled() else {
            showInformation("Local transcription is off", "Turn on Transcribe locally in Settings before retrying.")
            return
        }
        transcriptionRetryInFlight = true
        refreshUI()
        Task { [weak self, transcription, root] in
            let result = await transcription.retryPending(root: root)
            guard let self else { return }
            transcriptionRetryInFlight = false
            refreshUI()
            guard !shuttingDown, sleepController?.isSleeping != true else { return }
            switch result {
            case .disabled:
                showInformation("Local transcription is off", "Turn on Transcribe locally in Settings before retrying.")
            case .queued(0):
                showInformation("Nothing new to retry",
                    "No additional recordings are eligible for transcription. Completed transcripts, work already queued, and recordings awaiting review are left as they are.")
            case .queued(let count):
                let recordings = count == 1 ? "1 recording was" : "\(count) recordings were"
                showInformation("Transcription retry queued",
                    "\(recordings) added to the local transcription queue. Follow progress in Quill’s feather menu.")
            }
        }
    }

    private func showTranscription(_ status: TranscriptionCoordinator.Status) {
        switch status {
        case .idle: menuBar.updateTranscription(nil)
        case .preparing: menuBar.updateTranscription("Preparing local transcription…")
        case .transcribing(let name, let queued):
            menuBar.updateTranscription(queued > 0 ? "transcribing \(name) · \(queued) queued" : "transcribing \(name)")
        case .failed(let name): menuBar.updateTranscription("transcription failed · \(name)")
        }
    }

    private func identifier(_ target: ScreenCaptureTarget) -> String {
        switch target {
        case .window(let id, let pid): return "window:\(id):\(pid)"
        case .display(let id): return "display:\(id)"
        }
    }

    private func openFolder() {
        try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        NSWorkspace.shared.open(root)
    }

    private func log(_ text: String) { FileHandle.standardError.write(Data((text + "\n").utf8)) }

    private static func format(_ interval: TimeInterval) -> String {
        let total = max(0, Int(interval))
        let h = total / 3600, m = (total % 3600) / 60, s = total % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }
}

private struct CaptureSelectionError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
