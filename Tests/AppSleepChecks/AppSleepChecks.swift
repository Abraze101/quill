import AVFoundation
// Appended after the unchanged production AppController in a temporary source
// file. Same-file access tests the real private paths without shipping test APIs.
extension AppController {
    private static func waitFor(_ condition: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(3)
        while !condition() {
            precondition(Date() < deadline, "App sleep lifecycle timed out")
            try await Task.sleep(for: .milliseconds(5))
        }
    }

    private static func withFixture(_ check: (AppController) async throws -> Void) async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("quill-app-sleep-\(UUID())")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        Fixture.reset(root: root)
        let app = AppController(root: root)
        defer {
            app.ticker?.invalidate()
            app.sleepController?.stopObserving()
            try? FileManager.default.removeItem(at: root)
        }
        try await check(app)
    }

    private func assertHeld(_ directory: URL, finished: Bool) throws {
        let record = try RecordingReviewGate.read(session: directory)!
        precondition(record.reviewStatus == .pending, "Sleep recording was approved without review")
        precondition(record.phase == (finished ? .finished : .active))
        precondition(record.reasons.contains { $0.contains("Mac went to sleep") }, "Sleep reason was not persisted")
        precondition(RecordingReviewGate.blocksProcessing(session: directory), "Sleep hold did not block processing")
        precondition(Fixture.enqueued.isEmpty && Fixture.registered.isEmpty, "Sleep recording escaped to processing")
    }

    private func assertSystemHeld(_ directory: URL, finished: Bool) throws {
        let record = try RecordingReviewGate.read(session: directory)!
        precondition(record.reviewStatus == .pending, "Interrupted system audio was approved without review")
        precondition(record.phase == (finished ? .finished : .active))
        precondition(record.reasons.contains { $0.lowercased().contains("system audio") },
                     "System audio interruption was not persisted")
        precondition(RecordingReviewGate.blocksProcessing(session: directory))
        precondition(Fixture.enqueued.isEmpty && Fixture.registered.isEmpty,
                     "Interrupted system audio escaped into transcription or completion")
    }

    static func activeAutomaticSleep() async throws {
        try await withFixture { app in
            app.meetingAutomation.join("test-meeting")
            try await waitFor { app.phase == .recording }
            let directory = app.session!.dir
            let sessionID = app.safety!.sessionID
            Fixture.stopGate = Gate(open: false)
            let beforeSleep = Date()
            Fixture.center.post(name: NSWorkspace.willSleepNotification, object: nil)
            precondition(app.sleepController?.isSleeping == true)
            precondition(app.phase == .stopping && Fixture.micStops == 1,
                         "Sleep did not synchronously leave recording and stop the microphone")
            precondition(Fixture.systemStopRequests == 1,
                         "Sleep did not synchronously invalidate system-audio recovery")
            precondition(app.recordingControls.snapshot.phase == .stopping)
            precondition(app.recordingControls.snapshot.microphoneStatus == "Stopped")
            try app.assertHeld(directory, finished: false)
            let shownElapsed = app.recordingControls.snapshot.elapsed
            Fixture.center.post(name: NSWorkspace.willSleepNotification, object: nil)
            precondition(Fixture.micStops == 1, "Repeated sleep double-stopped capture")
            try await waitFor { Fixture.systemStops == 1 }
            Fixture.center.post(name: NSWorkspace.didWakeNotification, object: nil)
            precondition(app.phase == .stopping, "Wake revived recording before cleanup")
            precondition(app.recordingControls.snapshot.elapsed == shownElapsed, "Wake resumed the timer")
            Fixture.stopGate.release()
            try await waitFor { app.phase == .idle }
            try app.assertHeld(directory, finished: true)
            precondition(app.recordingControls.snapshot.phase == .review)
            precondition(app.recordingControls.snapshot.sessionID == sessionID.uuidString)
            precondition(app.recordingControls.snapshot.detail?.contains("Mac went to sleep") == true)
            precondition(app.recordingControls.snapshot.processEnabled, "Finalized files could not be reviewed")
            let metadata = try JSONSerialization.jsonObject(with: Data(contentsOf: directory.appendingPathComponent("meta.json"))) as! [String: Any]
            let ended = ISO8601DateFormatter().date(from: metadata["ended"] as! String)!
            precondition(abs(ended.timeIntervalSince(beforeSleep)) < 2, "Cleanup changed the sleep end time")
            app.meetingAutomation.freshHeartbeat()
            await Task.yield()
            precondition(app.phase == .idle && Fixture.micStarts == 1 && Fixture.systemStarts == 1,
                         "A fresh same-call heartbeat restarted capture after wake")
            precondition(app.meetingAutomation.failedMeetingToRetry == nil,
                         "Sleep suppression became an automatic retry")
        }
    }

    static func permissionPendingSleep() async throws {
        try await withFixture { app in
            Fixture.startGate = Gate(open: false)
            app.meetingAutomation.join("pending-meeting")
            try await waitFor { Fixture.systemStarts == 1 }
            let directory = app.session!.dir
            precondition(app.phase == .starting && Fixture.micStarts == 0)
            Fixture.center.post(name: NSWorkspace.willSleepNotification, object: nil)
            precondition(app.cancelStart && Fixture.micStops == 1,
                         "Sleep did not invalidate the permission-pending start")
            precondition(Fixture.systemStopRequests == 1)
            try app.assertHeld(directory, finished: false)
            Fixture.center.post(name: NSWorkspace.didWakeNotification, object: nil)
            Fixture.startGate.release()
            try await waitFor { app.phase == .idle }
            try app.assertHeld(directory, finished: true)
            precondition(Fixture.micStarts == 0, "Permission completion started the microphone after sleep")
            precondition(!RecordingReviewGate.hasFinalizedMetadata(session: directory), "Cancelled start falsely published completed media")
            precondition(app.recordingControls.snapshot.phase == .review && !app.recordingControls.snapshot.processEnabled)
            app.meetingAutomation.freshHeartbeat()
            await Task.yield()
            precondition(app.phase == .idle && Fixture.systemStarts == 1, "Wake re-ran a cancelled capture")
        }
    }

    static func manualCancelDuringPermissionWait() async throws {
        try await withFixture { app in
            Fixture.startGate = Gate(open: false)
            app.meetingAutomation.join("cancelled-pending-meeting")
            try await waitFor { Fixture.systemStarts == 1 }
            let directory = app.session!.dir
            let recorder = Fixture.systemRecorders.last!
            precondition(app.phase == .starting && Fixture.micStarts == 0)
            // Exercise the user's Cancel action while native startup is still
            // held. It must invalidate capture without awaiting that startup.
            app.cancelPendingStart()
            precondition(app.cancelStart && recorder.stopRequested
                         && Fixture.systemStopRequests == 1 && Fixture.micStops == 1,
                         "Cancel deferred capture invalidation until permission returned")
            precondition(Fixture.systemStops == 0 && Fixture.micStarts == 0)
            precondition(RecordingReviewGate.blocksProcessing(session: directory))
            precondition(Fixture.enqueued.isEmpty && Fixture.registered.isEmpty)
            precondition(app.meetingAutomation.failedMeetingToRetry == nil)
            Fixture.startGate.release()
            try await waitFor { app.phase == .idle }
            precondition(Fixture.micStarts == 0 && Fixture.systemStops == 1,
                         "Late native success started the microphone after Cancel")
            let review = try RecordingReviewGate.read(session: directory)!
            precondition(review.phase == .finished && review.reviewStatus == .pending)
            precondition(RecordingReviewGate.blocksProcessing(session: directory))
            precondition(!RecordingReviewGate.hasFinalizedMetadata(session: directory))
            precondition(Fixture.enqueued.isEmpty && Fixture.registered.isEmpty)
            app.meetingAutomation.freshHeartbeat()
            await Task.yield()
            precondition(app.phase == .idle && Fixture.systemStarts == 1 && Fixture.micStarts == 0,
                         "The cancelled meeting automatically restarted after a fresh heartbeat")
            precondition(app.meetingAutomation.failedMeetingToRetry == nil)
        }
    }

    static func sleepWhileNormalStopPending() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let directory = app.session!.dir
            Fixture.stopGate = Gate(open: false)
            app.requestStop(manually: true)
            try await waitFor { Fixture.systemStops == 1 }
            precondition(Fixture.micStops == 1)
            Fixture.center.post(name: NSWorkspace.willSleepNotification, object: nil)
            try app.assertHeld(directory, finished: false)
            Fixture.center.post(name: NSWorkspace.didWakeNotification, object: nil)
            precondition(app.phase == .stopping && Fixture.micStops == 1)
            Fixture.stopGate.release()
            try await waitFor { app.phase == .idle }
            try app.assertHeld(directory, finished: true)
            precondition(Fixture.systemStops == 1, "Sleep duplicated pending native cleanup")
        }
    }

    static func cleanStopPositiveControl() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let directory = app.session!.dir
            // A display turning off is not a system-sleep interruption.
            Fixture.center.post(name: NSWorkspace.screensDidSleepNotification, object: nil)
            precondition(app.phase == .recording && Fixture.micStops == 0)
            app.requestStop(manually: true)
            try await waitFor { app.phase == .idle }
            precondition(!RecordingReviewGate.blocksProcessing(session: directory))
            precondition(Fixture.registered == [directory] && Fixture.enqueued == [directory],
                         "Positive control never reached processing; negative tests could pass vacuously")
            precondition(Fixture.systemStopRequests == 1 && Fixture.systemStops == 1)
        }
    }

    static func recoveredSystemAudioNeedsReview() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let directory = app.session!.dir
            let recorder = Fixture.systemRecorders.last!
            recorder.emit(.recovering(attempt: 1))
            try app.assertSystemHeld(directory, finished: false)
            precondition(app.recordingControls.snapshot.systemAudioStatus == "Reconnecting")
            precondition(app.recordingControls.snapshot.stopEnabled)
            let reasons = try RecordingReviewGate.read(session: directory)!.reasons
            recorder.emit(.recovering(attempt: 2))
            let repeatedReasons = try RecordingReviewGate.read(session: directory)!.reasons
            precondition(repeatedReasons == reasons,
                         "Recovery attempts added duplicate review reasons")
            recorder.emit(.recording)
            precondition(app.recordingControls.snapshot.systemAudioStatus == "Recording")
            precondition(app.recordingControls.snapshot.captureNeedsAttention,
                         "Fresh audio concealed the earlier recording gap")
            try app.assertSystemHeld(directory, finished: false)
            app.requestStop(manually: true)
            try await waitFor { app.phase == .idle }
            try app.assertSystemHeld(directory, finished: true)
            precondition(app.recordingControls.snapshot.phase == .review && app.recordingControls.snapshot.processEnabled)
            let metadata = try JSONSerialization.jsonObject(with:
                Data(contentsOf: directory.appendingPathComponent("meta.json"))) as! [String: Any]
            precondition(metadata["files"] as? [String: String] == ["mic": "mic.caf", "system": "system.caf"],
                         "Recovery changed the downstream media contract")
            precondition((metadata["system_capture"] as? [String: Any])?["coverage_complete"] as? Bool == false)
        }
    }

    static func terminalSystemAudioFailureNeedsReview() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let directory = app.session!.dir
            Fixture.systemRecorders.last!.emit(.failed(message: "System audio could not reconnect."))
            try app.assertSystemHeld(directory, finished: false)
            precondition(app.recordingControls.snapshot.systemAudioStatus == "Unavailable")
            precondition(app.recordingControls.snapshot.stopEnabled && app.phase == .recording,
                         "System audio loss prevented the user from saving the other track")
            app.requestStop(manually: true)
            try await waitFor { app.phase == .idle }
            try app.assertSystemHeld(directory, finished: true)
        }
    }

    static func stopDuringSystemRecoveryKeepsHold() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let directory = app.session!.dir
            let recorder = Fixture.systemRecorders.last!
            recorder.emit(.recovering(attempt: 1))
            Fixture.stopGate = Gate(open: false)
            app.requestStop(manually: true)
            precondition(recorder.stopRequested && Fixture.systemStopRequests == 1,
                         "Stop deferred cancellation of a queued recovery")
            try await waitFor { Fixture.systemStops == 1 }
            let stoppingSnapshot = app.recordingControls.snapshot
            let notices = Fixture.notices
            recorder.emit(.recording)
            recorder.emit(.failed(message: "System audio stale failure after Stop."))
            precondition(app.recordingControls.snapshot == stoppingSnapshot && Fixture.notices == notices,
                         "A late recovery callback changed a stopping session")
            try app.assertSystemHeld(directory, finished: false)
            Fixture.stopGate.release()
            try await waitFor { app.phase == .idle }
            try app.assertSystemHeld(directory, finished: true)
        }
    }

    static func sleepDuringSystemRecoveryKeepsHold() async throws {
        try await withFixture { app in
            app.meetingAutomation.join("recovering-meeting")
            try await waitFor { app.phase == .recording }
            let directory = app.session!.dir
            let recorder = Fixture.systemRecorders.last!
            recorder.emit(.recovering(attempt: 1))
            Fixture.stopGate = Gate(open: false)
            Fixture.center.post(name: NSWorkspace.willSleepNotification, object: nil)
            precondition(recorder.stopRequested && Fixture.systemStopRequests == 1)
            try app.assertHeld(directory, finished: false)
            try app.assertSystemHeld(directory, finished: false)
            try await waitFor { Fixture.systemStops == 1 }
            let stoppingSnapshot = app.recordingControls.snapshot
            recorder.emit(.recording)
            Fixture.center.post(name: NSWorkspace.didWakeNotification, object: nil)
            precondition(app.phase == .stopping && app.recordingControls.snapshot.elapsed == stoppingSnapshot.elapsed)
            Fixture.stopGate.release()
            try await waitFor { app.phase == .idle }
            try app.assertHeld(directory, finished: true)
            try app.assertSystemHeld(directory, finished: true)
            app.meetingAutomation.freshHeartbeat()
            await Task.yield()
            precondition(app.phase == .idle && Fixture.systemStarts == 1 && Fixture.micStarts == 1,
                         "Wake or recovery completion restarted the same meeting")
        }
    }

    static func staleSystemCallbackCannotAlterNextSession() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            // Keep the old session alive so its weak callback reference still
            // resolves; identity checking, not deallocation, must reject it.
            let oldSession = app.session!
            let oldRecorder = Fixture.systemRecorders.last!
            app.requestStop(manually: true)
            try await waitFor { app.phase == .idle }
            precondition(Fixture.registered == [oldSession.dir] && Fixture.enqueued == [oldSession.dir])
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let current = app.session!
            let snapshot = app.recordingControls.snapshot
            let notices = Fixture.notices
            oldRecorder.emit(.recovering(attempt: 1))
            oldRecorder.emit(.failed(message: "System audio failure from old recording."))
            oldRecorder.emit(.recording)
            precondition(app.session === current && app.recordingControls.snapshot == snapshot)
            precondition(app.safetyReasons.isEmpty && Fixture.notices == notices,
                         "Old capture poisoned the new recording")
            let currentReview = try RecordingReviewGate.read(session: current.dir)!
            precondition(currentReview.reasons.isEmpty)
            precondition(!RecordingReviewGate.blocksProcessing(session: oldSession.dir),
                         "Stale callback revoked an already completed recording")
            app.requestStop(manually: true)
            try await waitFor { app.phase == .idle }
            precondition(Fixture.registered == [oldSession.dir, current.dir]
                         && Fixture.enqueued == [oldSession.dir, current.dir])
        }
    }

    static func incompleteSystemReportCannotProcessWithoutCallback() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let directory = app.session!.dir
            // A status callback may still be queued when Stop arrives. The
            // final saved report must independently catch incomplete coverage.
            Fixture.systemRecorders.last!.coverageComplete = false
            precondition(app.safetyReasons.isEmpty)
            app.requestStop(manually: true)
            try await waitFor { app.phase == .idle }
            try app.assertSystemHeld(directory, finished: true)
        }
    }
    static func pauseResumeSameSession() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let owned = app.session!
            let id = app.safety!.sessionID.uuidString
            let revision = app.recordingActionRevision
            app.pauseDisplayedRecording(id, revision: revision, expectedPaused: false)
            precondition(owned.isPaused && app.phase == .pausing)
            try await waitFor { app.phase == .paused }
            let pausedDuration = app.elapsedInterval
            try await Task.sleep(for: .milliseconds(40))
            precondition(app.elapsedInterval == pausedDuration, "Paused timer advanced")
            precondition(Fixture.enqueued.isEmpty && Fixture.registered.isEmpty,
                         "Pause sent unfinished media to processing")
            precondition(!FileManager.default.fileExists(atPath: owned.dir.appendingPathComponent("meta.json").path))
            precondition(app.recordingControls.snapshot.phase == .paused && app.recordingControls.snapshot.pauseEnabled)
            app.pauseDisplayedRecording(id, revision: revision, expectedPaused: false)
            app.stopDisplayedRecording(id, revision: revision)
            precondition(app.phase == .paused, "Stale controls altered a paused recording")
            app.pauseDisplayedRecording(id, revision: app.recordingActionRevision, expectedPaused: true)
            try await waitFor { app.phase == .recording }
            precondition(app.session === owned && !owned.isPaused)
            precondition(Fixture.micStarts == 1 && Fixture.systemStarts == 1,
                         "Resume started a second session")
            precondition(app.elapsedInterval >= pausedDuration)
            app.requestStop(manually: true)
            try await waitFor { app.phase == .idle }
            precondition(Fixture.enqueued == [owned.dir] && Fixture.registered == [owned.dir])
        }
    }

    static func stopWhilePaused() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let owned = app.session!
            let id = app.safety!.sessionID.uuidString
            app.pauseDisplayedRecording(id, revision: app.recordingActionRevision, expectedPaused: false)
            try await waitFor { app.phase == .paused }
            let revision = app.recordingActionRevision
            let elapsed = app.elapsedInterval
            app.stopDisplayedRecording(id, revision: revision)
            try await waitFor { app.phase == .idle }
            precondition(Fixture.enqueued == [owned.dir] && Fixture.registered == [owned.dir])
            precondition(owned.activeDuration == elapsed)
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let next = app.session!
            app.pauseDisplayedRecording(id, revision: revision, expectedPaused: true)
            app.stopDisplayedRecording(id, revision: revision)
            precondition(app.session === next && app.phase == .recording && !next.isPaused,
                         "Stale paused controls changed the next recording")
            app.requestStop(manually: true)
            try await waitFor { app.phase == .idle }
        }
    }

    static func stopSupersedesPauseTransition() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let owned = app.session!
            app.pauseDisplayedRecording(app.safety!.sessionID.uuidString,
                revision: app.recordingActionRevision, expectedPaused: false)
            precondition(app.phase == .pausing)
            app.requestStop(manually: true)
            try await waitFor { app.phase == .idle }
            precondition(owned.isPaused && Fixture.enqueued == [owned.dir])
            precondition(Fixture.micStops == 1 && Fixture.systemStops == 1)
        }
    }

    static func sleepSupersedesResume() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let owned = app.session!
            let id = app.safety!.sessionID.uuidString
            app.pauseDisplayedRecording(id, revision: app.recordingActionRevision, expectedPaused: false)
            try await waitFor { app.phase == .paused }
            app.pauseDisplayedRecording(id, revision: app.recordingActionRevision, expectedPaused: true)
            precondition(app.phase == .resuming)
            Fixture.center.post(name: NSWorkspace.willSleepNotification, object: nil)
            Fixture.center.post(name: NSWorkspace.didWakeNotification, object: nil)
            try await waitFor { app.phase == .idle }
            precondition(owned.isPaused, "Late resume reopened capture after sleep")
            try app.assertHeld(owned.dir, finished: true)
            precondition(Fixture.micStarts == 1 && Fixture.systemStarts == 1)
        }
    }

    static func pausedAutomaticMeetingLeaves() async throws {
        try await withFixture { app in
            app.meetingAutomation.join("paused-meeting")
            try await waitFor { app.phase == .recording }
            let owned = app.session!
            app.pauseDisplayedRecording(app.safety!.sessionID.uuidString,
                revision: app.recordingActionRevision, expectedPaused: false)
            try await waitFor { app.phase == .paused }
            app.meetingAutomation.onStop?(owned.dir.path)
            try await waitFor { app.phase == .idle }
            precondition(Fixture.enqueued == [owned.dir] && Fixture.registered == [owned.dir])
        }
    }

    static func stopWhileScreenPauseAwaitsNativeCleanup() async throws {
        try await withFixture { app in
            Fixture.screenEnabled = true
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let owned = app.session!
            let video = Fixture.screenRecorders[0]
            Fixture.screenPauseGate = Gate(open: false)
            app.pauseDisplayedRecording(app.safety!.sessionID.uuidString,
                revision: app.recordingActionRevision, expectedPaused: false)
            try await waitFor { video.pauses == 1 }
            precondition(app.phase == .pausing && owned.isPaused && video.needsCleanup)
            app.requestStop(manually: true)
            try await waitFor { video.stopRequested && Fixture.systemStops == 1 }
            precondition(app.phase == .stopping && Fixture.enqueued.isEmpty)
            precondition(!FileManager.default.fileExists(atPath: owned.dir.appendingPathComponent("meta.json").path),
                         "Metadata was published before paused screen cleanup")
            Fixture.screenPauseGate.release()
            try await waitFor { app.phase == .idle }
            precondition(Fixture.enqueued == [owned.dir] && !video.needsCleanup)
            precondition(Fixture.screenRecorders.count == 1 && video.resumes == 0)
        }
    }

    static func sleepWhileScreenResumeAwaitsNativeStartup() async throws {
        try await withFixture { app in
            Fixture.screenEnabled = true
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let owned = app.session!
            let video = Fixture.screenRecorders[0]
            let id = app.safety!.sessionID.uuidString
            app.pauseDisplayedRecording(id, revision: app.recordingActionRevision, expectedPaused: false)
            try await waitFor { app.phase == .paused }
            Fixture.screenResumeGate = Gate(open: false)
            app.pauseDisplayedRecording(id, revision: app.recordingActionRevision, expectedPaused: true)
            try await waitFor { video.resumes == 1 }
            precondition(app.phase == .resuming && owned.isPaused)
            Fixture.center.post(name: NSWorkspace.willSleepNotification, object: nil)
            try await waitFor { video.stopRequested }
            precondition(app.phase == .stopping && owned.isPaused && Fixture.micStops == 1)
            Fixture.center.post(name: NSWorkspace.didWakeNotification, object: nil)
            Fixture.screenResumeGate.release()
            try await waitFor { app.phase == .idle }
            precondition(!video.isRecording && !video.needsCleanup && owned.isPaused)
            precondition(Fixture.screenRecorders.count == 1)
            try app.assertHeld(owned.dir, finished: true)
        }
    }

    static func automaticRecordingResumesAfterAutoStartDisabled() async throws {
        try await withFixture { app in
            app.meetingAutomation.join("owned-meeting")
            try await waitFor { app.phase == .recording }
            let owned = app.session!
            let id = app.safety!.sessionID.uuidString
            app.pauseDisplayedRecording(id, revision: app.recordingActionRevision, expectedPaused: false)
            try await waitFor { app.phase == .paused }
            Fixture.automationEnabled = false
            precondition(!app.meetingAutomation.mayStart(meeting: "owned-meeting"))
            app.pauseDisplayedRecording(id, revision: app.recordingActionRevision, expectedPaused: true)
            try await waitFor { app.phase == .recording }
            precondition(app.session === owned && !owned.isPaused && app.safetyReasons.isEmpty)
            app.requestStop(manually: true)
            try await waitFor { app.phase == .idle }
            precondition(Fixture.enqueued == [owned.dir])
        }
    }

    static func pausedSafetyDeadlineStillApplies() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let owned = app.session!
            let id = app.safety!.sessionID
            app.pauseDisplayedRecording(id.uuidString, revision: app.recordingActionRevision, expectedPaused: false)
            try await waitFor { app.phase == .paused }
            app.safety = RecordingSafety(sessionID: id, owner: .manual,
                now: RecordingSafetyClock.now - 100, checkInAfter: 1, responseWindow: 1)
            app.pauseDisplayedRecording(id.uuidString, revision: app.recordingActionRevision, expectedPaused: true)
            try await waitFor { app.phase == .idle }
            precondition(owned.isPaused && RecordingReviewGate.blocksProcessing(session: owned.dir),
                         "Resume bypassed the unattended-session deadline")
            precondition(Fixture.enqueued.isEmpty && Fixture.registered.isEmpty)
        }
    }

    /// Seed an observation eleven seconds ago, then let the unchanged
    /// production coordinator evaluate it at the current time. Evidence is
    /// still fresh, and no real meeting, clock, file or device is involved.
    private func showReminderFixture(_ meeting: String) {
        let now = Date().timeIntervalSince1970
        let signal = MeetingSignal(source: "reminder-test-browser", meeting: meeting,
            provider: "google-meet", state: .joined, timestamp: now - 11)
        meetingAutomation.observe([signal], now: now)
        meetingWatchdog.reset()
        precondition(meetingWatchdog.evaluate(signals: [signal], capture: .idle,
            deliberatelySkipped: [], now: now - 11) == nil)
        refreshUI()
        precondition(uncoveredMeeting == meeting && meetingReminder.meetingID == meeting,
                     "Fresh joined idle call did not display its exact reminder")
    }

    static func idleMeetingReminderAndConfirmedStartupHeartbeat() async throws {
        try await withFixture { app in
            Fixture.automationEnabled = false
            app.showReminderFixture("idle-call")
            precondition(app.meetingReminder.canStart && app.meetingReminder.canSkip)
            precondition(app.recordingHeartbeat.latest?.phase == "idle")
            precondition(Fixture.notices.filter { $0.contains("meeting is not recording") }.count == 1)
            app.refreshUI()
            app.refreshUI()
            precondition(Fixture.notices.filter { $0.contains("meeting is not recording") }.count == 1,
                         "Persistent reminder repeated its notification")
            Fixture.startGate = Gate(open: false)
            app.meetingReminder.onStart?("idle-call")
            precondition(app.phase == .starting && app.recordingHeartbeat.latest?.phase == "starting")
            try await waitFor { Fixture.systemStarts == 1 }
            precondition(Fixture.micStarts == 0 && app.recordingHeartbeat.latest?.phase == "starting",
                         "Permission-pending capture was falsely confirmed as recording")
            Fixture.startGate.release()
            try await waitFor { app.phase == .recording }
            precondition(app.recordingHeartbeat.latest?.phase == "recording")
            precondition(app.uncoveredMeeting == nil && app.meetingReminder.meetingID == nil)
            precondition(app.automaticMeeting == nil && !Fixture.automationEnabled)
            app.requestStop(manually: true)
            try await waitFor { app.phase == .idle }
            precondition(app.meetingReminder.meetingID == nil,
                         "Manual Stop was undone by a meeting reminder")
            precondition(app.recordingHeartbeat.latest?.deliberatelySkipped == ["idle-call"])
        }
    }

    static func idleSleepClearsReminderAndHeartbeatWithoutStartingCapture() async throws {
        try await withFixture { app in
            Fixture.automationEnabled = false
            app.showReminderFixture("sleeping-idle-call")
            let invalidations = app.recordingHeartbeat.invalidations
            let noticeCount = Fixture.notices.count
            Fixture.center.post(name: NSWorkspace.willSleepNotification, object: nil)
            precondition(app.phase == .idle && app.sleepController?.isSleeping == true)
            precondition(app.uncoveredMeeting == nil && app.meetingReminder.meetingID == nil)
            precondition(app.recordingHeartbeat.latest == nil && app.recordingHeartbeat.invalidations > invalidations,
                         "Idle sleep retained a browser heartbeat")
            Fixture.center.post(name: NSWorkspace.didWakeNotification, object: nil)
            precondition(app.sleepController?.isSleeping == false)
            precondition(app.meetingReminder.meetingID == nil && app.uncoveredMeeting == nil,
                         "Wake revived the previous warning without a new grace period")
            precondition(Fixture.notices.count == noticeCount && Fixture.micStarts == 0 && Fixture.systemStarts == 0)
            precondition(app.recordingHeartbeat.latest?.phase == "idle")
        }
    }

    static func idleQuitClearsReminderAndHeartbeat() async throws {
        try await withFixture { app in
            Fixture.automationEnabled = false
            app.showReminderFixture("quitting-idle-call")
            let invalidations = app.recordingHeartbeat.invalidations
            precondition(app.shouldTerminate() == .terminateNow)
            precondition(app.terminationAllowed)
            precondition(app.meetingReminder.meetingID == nil)
            precondition(app.recordingHeartbeat.latest == nil && app.recordingHeartbeat.invalidations > invalidations)
            precondition(Fixture.micStarts == 0 && Fixture.systemStarts == 0)
        }
    }

    static func staleReminderActionsCannotChangeAnotherMeeting() async throws {
        try await withFixture { app in
            Fixture.automationEnabled = false
            app.showReminderFixture("old-call")
            let oldStart = app.meetingReminder.onStart
            let oldSkip = app.meetingReminder.onSkip
            let now = Date().timeIntervalSince1970
            app.meetingAutomation.observe([
                .init(source: "reminder-test-browser", meeting: "old-call", provider: "google-meet", state: .left, timestamp: now),
                .init(source: "new-browser", meeting: "new-call", provider: "google-meet", state: .joined, timestamp: now)
            ], now: now)
            // Simulate dispatch before the next visual refresh: signal identity
            // must reject the stale action even while the old banner remains.
            oldStart?("old-call")
            oldSkip?("old-call")
            precondition(app.phase == .idle && Fixture.systemStarts == 0 && Fixture.micStarts == 0)
            precondition(app.meetingAutomation.deliberatelySkipped.isEmpty)
            app.showReminderFixture("new-call")
            oldStart?("old-call")
            oldSkip?("old-call")
            precondition(app.phase == .idle && app.uncoveredMeeting == "new-call")
            precondition(app.meetingAutomation.deliberatelySkipped.isEmpty)
            app.meetingReminder.onSkip?("new-call")
            precondition(app.meetingAutomation.deliberatelySkipped == ["new-call"])
            precondition(app.uncoveredMeeting == nil && app.meetingReminder.meetingID == nil)
            precondition(app.recordingHeartbeat.latest?.deliberatelySkipped == ["new-call"])
            precondition(Fixture.systemStarts == 0 && Fixture.micStarts == 0)
        }
    }

    static func failedAutomaticStartCanBeManuallyStartedAfterAutomationDisabled() async throws {
        try await withFixture { app in
            app.meetingAutomation.startFailed(meeting: "failed-auto-call")
            precondition(app.meetingAutomation.hasFailedStart(meeting: "failed-auto-call"))
            Fixture.automationEnabled = false
            app.showReminderFixture("failed-auto-call")
            app.meetingReminder.onStart?("failed-auto-call")
            try await waitFor { app.phase == .recording }
            precondition(app.automaticMeeting == nil && !Fixture.automationEnabled,
                         "Explicit manual Start changed automation or reused a disabled retry")
            precondition(Fixture.systemStarts == 1 && Fixture.micStarts == 1)
            app.requestStop(manually: true)
            try await waitFor { app.phase == .idle }
            precondition(app.meetingAutomation.deliberatelySkipped == ["failed-auto-call"])
            precondition(!app.meetingAutomation.hasFailedStart(meeting: "failed-auto-call"))
        }
    }
}

@main
private struct AppSleepChecks {
    @MainActor
    static func main() async throws {
        try await AppController.crashCheckpointAndRestartReview()
        try await AppController.crashCheckpointFailureStopsCapture()
        try await AppController.crashBoundaryCheckpointFailureStopsCapture()
        try await AppController.activeAutomaticSleep()
        try await AppController.permissionPendingSleep()
        try await AppController.manualCancelDuringPermissionWait()
        try await AppController.sleepWhileNormalStopPending()
        try await AppController.cleanStopPositiveControl()
        try await AppController.recoveredSystemAudioNeedsReview()
        try await AppController.terminalSystemAudioFailureNeedsReview()
        try await AppController.stopDuringSystemRecoveryKeepsHold()
        try await AppController.sleepDuringSystemRecoveryKeepsHold()
        try await AppController.staleSystemCallbackCannotAlterNextSession()
        try await AppController.incompleteSystemReportCannotProcessWithoutCallback()
        try await AppController.pauseResumeSameSession()
        try await AppController.stopWhilePaused()
        try await AppController.stopSupersedesPauseTransition()
        try await AppController.sleepSupersedesResume()
        try await AppController.pausedAutomaticMeetingLeaves()
        try await AppController.pausedSafetyDeadlineStillApplies()
        try await AppController.stopWhileScreenPauseAwaitsNativeCleanup()
        try await AppController.sleepWhileScreenResumeAwaitsNativeStartup()
        try await AppController.automaticRecordingResumesAfterAutoStartDisabled()
        try await AppController.idleMeetingReminderAndConfirmedStartupHeartbeat()
        try await AppController.idleSleepClearsReminderAndHeartbeatWithoutStartingCapture()
        try await AppController.idleQuitClearsReminderAndHeartbeat()
        try await AppController.staleReminderActionsCannotChangeAnotherMeeting()
        try await AppController.failedAutomaticStartCanBeManuallyStartedAfterAutomationDisabled()
        try await AppController.transcriptionRetryRequestGuards()
        try await AppController.transcriptionRetrySuppressesLateAlerts()
        print("App sleep/recovery/pause/reminder/retry integration checks passed (31 production-coordinator scenarios; no devices, UI, user configuration, or external processing).")
    }
}

extension AppController {
    static func transcriptionRetryRequestGuards() async throws {
        try await withFixture { app in
            Fixture.allowInformationAlerts = true
            Fixture.transcriptionEnabled = false
            precondition(!app.canRetryTranscriptions)
            app.retryTranscriptions()
            precondition(Fixture.retryCalls == 0 && Fixture.informationAlerts.count == 1)
            precondition(Fixture.informationAlerts[0].contains("Local transcription is off"))

            Fixture.transcriptionEnabled = true
            Fixture.retryGate = Gate(open: false)
            app.meetingAutomation.join("retry-during-capture")
            try await waitFor { app.phase == .recording }
            let recording = app.session
            app.retryTranscriptions()
            precondition(app.transcriptionRetryInFlight && !app.canRetryTranscriptions)
            app.retryTranscriptions()
            app.menuBar.onRetryTranscriptions?()
            try await waitFor { Fixture.retryCalls == 1 }
            precondition(app.session === recording && app.phase == .recording && Fixture.micStops == 0,
                         "Transcription retry changed the active recording")
            Fixture.retryGate.release()
            try await waitFor { !app.transcriptionRetryInFlight }
            precondition(Fixture.retryCalls == 1 && app.canRetryTranscriptions)
            precondition(Fixture.informationAlerts.last?.contains("1 recording was added") == true)
            precondition(Fixture.enqueued.isEmpty && Fixture.registered.isEmpty)

            Fixture.retryResult = .queued(0)
            app.retryTranscriptions()
            try await waitFor { !app.transcriptionRetryInFlight }
            precondition(Fixture.retryCalls == 2)
            precondition(Fixture.informationAlerts.last?.contains("No additional recordings are eligible") == true)
            app.requestStop(manually: true)
            try await waitFor { app.phase == .idle }
        }
    }

    static func transcriptionRetrySuppressesLateAlerts() async throws {
        for shuttingDown in [false, true] {
            try await withFixture { app in
                Fixture.retryGate = Gate(open: false)
                app.retryTranscriptions()
                try await waitFor { Fixture.retryCalls == 1 }
                if shuttingDown { app.shuttingDown = true }
                else { Fixture.center.post(name: NSWorkspace.willSleepNotification, object: nil) }
                precondition(!app.canRetryTranscriptions)
                app.retryTranscriptions()
                Fixture.retryGate.release()
                try await waitFor { !app.transcriptionRetryInFlight }
                precondition(Fixture.retryCalls == 1 && Fixture.informationAlerts.isEmpty,
                             "A late retry result showed UI during sleep or shutdown")
            }
        }
    }
}


extension AppController {
    static func crashCheckpointAndRestartReview() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let dir = app.session!.dir
            let id = app.safety!.sessionID
            let marker = dir.appendingPathComponent(RecordingCrashRecovery.journalFilename)
            precondition(FileManager.default.fileExists(atPath: marker.path), "Capture began without a crash journal")
            // Real local PCM files, without opening a microphone or system tap.
            let format = AVAudioFormat(standardFormatWithSampleRate: 16_000, channels: 1)!
            for name in ["mic.caf", "system.caf"] {
                let file = try AVAudioFile(forWriting: dir.appendingPathComponent(name), settings: [
                    AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16_000,
                    AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16,
                    AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false])
                let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 16_000)!
                buffer.frameLength = 16_000
                buffer.floatChannelData![0].initialize(repeating: 0.1, count: 16_000)
                try file.write(from: buffer)
            }
            app.pauseDisplayedRecording(id.uuidString, revision: app.recordingActionRevision, expectedPaused: false)
            try await waitFor { app.phase == .paused }
            Fixture.micCaptureOrigin = 2.5
            Fixture.systemCaptureOrigin = 1.5
            let snapshot = try app.session!.recoveryMetadata(transcriptionRequested: true,
                                                             screenRequested: false, screenStarted: false)
            precondition(snapshot["capture_start_offset_seconds"] as? Double == 1.5,
                         "Crash checkpoint lost the earliest monotonic audio origin")
            try app.checkpointCapture()
            precondition(!FileManager.default.fileExists(atPath: dir.appendingPathComponent("meta.json").path))
            // Lose controller ownership without running Stop/finalize. Core and
            // audio suites separately exercise OS process termination.
            app.ticker?.invalidate()
            app.sleepController?.stopObserving()
            app.captureLease = nil
            app.session = nil
            Fixture.reset(root: app.root)
            let reopened = AppController(root: app.root)
            defer { reopened.ticker?.invalidate(); reopened.sleepController?.stopObserving() }
            let review = try RecordingReviewGate.read(session: dir)!
            precondition(review.sessionID == id && review.phase == .finished && review.reviewStatus == .pending)
            precondition(reopened.recordingControls.snapshot.phase == .review && reopened.recordingControls.snapshot.processEnabled)
            precondition(reopened.hasUnfinishedReview, "Recovered meeting lost restart suppression")
            precondition(Fixture.enqueued.isEmpty && Fixture.registered.isEmpty, "Recovery triggered processing")
            reopened.meetingAutomation.join("still-joined-call")
            await Task.yield()
            precondition(reopened.phase == .idle && Fixture.micStarts == 0, "Restart recovered and immediately re-recorded the call")
            reopened.reviewRecording(sessionID: id.uuidString, process: false)
            let kept = try RecordingReviewGate.read(session: dir)!
            precondition(kept.reviewStatus == .keptLocal)
            precondition(!reopened.hasUnfinishedReview && Fixture.enqueued.isEmpty && Fixture.registered.isEmpty)
        }
    }

    static func crashCheckpointFailureStopsCapture() async throws {
        try await withFixture { app in
            app.requestStart()
            try await waitFor { app.phase == .recording }
            let dir = app.session!.dir
            // A mismatched on-disk identity must stop checkpointing and place
            // capture on review, never overwrite a foreign recovery record.
            let marker = dir.appendingPathComponent(RecordingCrashRecovery.journalFilename)
            try Data("{}".utf8).write(to: marker)
            app.tick()
            try await waitFor { app.phase == .idle }
            let record = try RecordingReviewGate.read(session: dir)!
            precondition(record.reviewStatus == .pending)
            precondition(Fixture.enqueued.isEmpty && Fixture.registered.isEmpty)
            precondition(record.reasons.contains { $0.contains("checkpoints") || $0.contains("checkpoint") })
        }
    }
}


extension AppController {
    static func crashBoundaryCheckpointFailureStopsCapture() async throws {
        for resume in [false, true] {
            try await withFixture { app in
                Fixture.screenEnabled = true
                app.requestStart()
                try await waitFor { app.phase == .recording }
                let dir = app.session!.dir
                let id = app.safety!.sessionID.uuidString
                if resume {
                    app.pauseDisplayedRecording(id, revision: app.recordingActionRevision, expectedPaused: false)
                    try await waitFor { app.phase == .paused }
                    Fixture.screenResumeGate = Gate(open: false)
                } else { Fixture.screenPauseGate = Gate(open: false) }
                app.pauseDisplayedRecording(id, revision: app.recordingActionRevision, expectedPaused: resume)
                try await waitFor { resume ? Fixture.screenRecorders.last!.resumes == 1 : Fixture.screenRecorders.last!.pauses == 1 }
                try Data("{}".utf8).write(to: dir.appendingPathComponent(RecordingCrashRecovery.journalFilename))
                if resume { Fixture.screenResumeGate.release() } else { Fixture.screenPauseGate.release() }
                try await waitFor { app.phase == .idle }
                let record = try RecordingReviewGate.read(session: dir)!
                precondition(record.reviewStatus == .pending)
                precondition(Fixture.enqueued.isEmpty && Fixture.registered.isEmpty)
                precondition(app.recordingControls.snapshot.phase == .review)
            }
        }
    }
}
