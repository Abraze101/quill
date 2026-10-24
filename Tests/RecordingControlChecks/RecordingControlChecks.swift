import AppKit

@main
struct RecordingControlChecks {
    @MainActor
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        checkMeetingReminder()
        checkRecordingHeartbeat()
        checkTranscriptionRetryMenu()
        let suiteName = "com.digimata.quill.control-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let control = RecordingStatusController(defaults: defaults, displayUpdates: false)
        guard let panel = app.windows.first(where: { $0.title == "Quill recording controls" }),
              let content = panel.contentView else { fatalError("Recording panel missing") }
        let stop = button("quill-status-stop", in: content)
        let compactStop = button("quill-status-compact-stop", in: content)
        let moreTime = button("quill-status-continue", in: content)
        let pause = button("quill-status-pause", in: content)
        let compactPause = button("quill-status-compact-pause", in: content)
        var stopped: [(String, Int?)] = []
        var extended: [(String, Int)] = []
        var pauseRequests: [(String, Int, Bool)] = []
        control.onStop = { stopped.append(($0, $1)) }
        control.onContinue = { extended.append(($0, $1)) }
        control.onPause = { pauseRequests.append(($0, $1, $2)) }

        let initial = RecordingStatusSnapshot(phase: .recording, sessionID: "first", elapsed: "00:42:15",
            microphoneStatus: "Recording", systemAudioStatus: "Recording", videoStatus: "Entire display", actionRevision: 4, stopEnabled: true)
        control.update(initial)
        render(content, name: "recording")
        expect(!panel.isVisible, "Offscreen checks must not reveal a panel")
        expect(stop.isEnabled && !stop.isHiddenOrHasHiddenAncestor, "Recording needs a visible Stop")
        let firstStop = stop.onClick
        control.update(.init(phase: .recording, sessionID: "second", elapsed: "00:00:03", actionRevision: 5, stopEnabled: true))
        firstStop?()
        expect(stopped.count == 1 && stopped[0].0 == "first" && stopped[0].1 == 4, "A displayed Stop must retain its original session")
        stop.performClick(nil)
        expect(stopped.count == 2 && stopped[1].0 == "second" && stopped[1].1 == 5, "New Stop must carry new session")
        expect(button("quill-status-stop", in: content) === stop, "Tick updates must preserve button identity")

        let collapse = allViews(content).compactMap { $0 as? RecordingActionButton }
            .first { $0.accessibilityLabel() == "Collapse recording controls" }!
        collapse.performClick(nil)
        expect(compactStop.isEnabled && !compactStop.isHiddenOrHasHiddenAncestor,
               "Collapsing must leave an enabled Stop")
        expect(labels(content).contains("REC"), "Compact indicator must contain REC text")
        expect(panel.frame.width < 366, "Compact recording control must be narrower")
        render(content, name: "recording-compact")

        control.update(.init(phase: .recording, sessionID: "pause-first", elapsed: "00:42:15",
            actionRevision: 6, stopEnabled: true, pauseEnabled: true))
        expect(compactPause.isEnabled && !compactPause.isHiddenOrHasHiddenAncestor,
            "Compact recording needs Pause beside Stop")
        let firstPause = compactPause.onClick
        compactPause.performClick(nil)
        expect(pauseRequests.last?.0 == "pause-first" && pauseRequests.last?.1 == 6 && pauseRequests.last?.2 == false,
            "Pause must carry the rendered identity, revision, and recording state")
        control.update(.init(phase: .paused, sessionID: "pause-first", elapsed: "00:42:15",
            microphoneStatus: "Paused", systemAudioStatus: "Paused", videoStatus: "Paused",
            actionRevision: 7, stopEnabled: true, pauseEnabled: true))
        expect(labels(content).contains("Paused") && labels(content).contains("00:42:15"),
            "Paused must stay explicit beside the supplied frozen elapsed time")
        expect(compactPause.title == "Resume" && compactPause.isEnabled && compactStop.isEnabled,
            "Paused recording needs Resume and Stop together")
        firstPause?()
        expect(pauseRequests.last?.1 == 6 && pauseRequests.last?.2 == false,
            "A captured Pause must not silently become Resume when state changes")
        compactPause.performClick(nil)
        expect(pauseRequests.last?.1 == 7 && pauseRequests.last?.2 == true,
            "Resume must carry the displayed paused state and new revision")
        expect(RecordingStatusSnapshot(phase: .paused).needsPersistentControl,
            "Paused recordings must keep their controls visible")
        render(content, name: "paused-compact")
        collapse.performClick(nil)
        expect(!pause.isHiddenOrHasHiddenAncestor && pause.title == "Resume", "Expanded controls need Resume")
        render(content, name: "paused")
        expect(!RecordingStatusSnapshot(phase: .paused, sessionID: "no-revision", pauseEnabled: true).canChangePause,
            "Pause/Resume cannot use an unversioned action")
        for phase in [RecordingStatusSnapshot.Phase.pausing, .resuming] {
            control.update(.init(phase: phase, sessionID: "pause-first", elapsed: "00:42:15",
                actionRevision: 8, stopEnabled: true, pauseEnabled: true))
            expect(!pause.isEnabled && stop.isEnabled, "Pause transitions must reject more pause requests but retain Stop")
            let count = pauseRequests.count
            pause.performClick(nil)
            expect(pauseRequests.count == count, "A disabled transition cannot request another pause action")
            render(content, name: phase == .pausing ? "pausing" : "resuming")
        }
        control.update(.init(phase: .paused, sessionID: "pause-first", elapsed: "00:42:15",
            detail: "System audio needs attention when recording resumes.", warningRevision: 4,
            actionRevision: 9, countdownSeconds: 120, stopEnabled: true, captureNeedsAttention: true, pauseEnabled: true))
        expect(labels(content).contains("Paused · Needs attention"), "Pausing must not hide capture warnings")
        expect(labels(content).contains(where: { $0.hasPrefix("Recording is paused.") && $0.contains("2:00") }),
            "Check-ins must describe a paused recording truthfully")
        expect(stop.isEnabled && pause.isEnabled && !moreTime.isHiddenOrHasHiddenAncestor,
            "Paused check-ins preserve Stop, Resume, and the separate safeguard extension")
        render(content, name: "paused-check-in")

        control.update(.init(phase: .recording, sessionID: "second", elapsed: "02:00:00",
            microphoneStatus: "Recording", systemAudioStatus: "Recording", videoStatus: "Entire display",
            warningRevision: 7, countdownSeconds: 300, stopEnabled: true))
        expect(!moreTime.isHiddenOrHasHiddenAncestor, "A new check-in must expand the Continue button")
        expect(labels(content).contains(where: { $0.contains("5:00") }), "Check-in must show a five-minute countdown")
        render(content, name: "check-in")
        collapse.performClick(nil)
        render(content, name: "check-in-compact")
        collapse.performClick(nil)
        let firstContinue = moreTime.onClick
        control.update(.init(phase: .recording, sessionID: "third", warningRevision: 8, countdownSeconds: 1, stopEnabled: true))
        firstContinue?()
        expect(extended.count == 1 && extended[0].0 == "second" && extended[0].1 == 7,
               "Continue must retain session and warning revision")
        expect(labels(content).contains(where: { $0.contains("0:01") }), "Deadline uses supplied countdown")

        control.update(.init(phase: .recording, sessionID: "third", elapsed: "03:00:00",
            microphoneStatus: "Unavailable", systemAudioStatus: "Recording", videoStatus: "Stopped",
            detail: "Microphone and screen stopped.", stopEnabled: true, captureNeedsAttention: true))
        render(content, name: "capture-problem")
        expect(labels(content).contains("Recording · Needs attention"), "Capture failure needs explicit text")
        expect(labels(content).contains("Unavailable"), "Actual microphone failure must remain visible")
        expect(!stop.isHiddenOrHasHiddenAncestor, "Capture warning must preserve Stop")
        control.update(.init(phase: .stopping, sessionID: "third", elapsed: "03:00:00",
                             microphoneStatus: "Stopped", systemAudioStatus: "Stopping", videoStatus: "Stopping"))
        expect(!stop.isHiddenOrHasHiddenAncestor && !stop.isEnabled, "Saving retains a disabled Stop")
        expect(pause.isHiddenOrHasHiddenAncestor && !pause.isEnabled, "Saving cannot expose Pause or Resume")
        expect(labels(content).contains("03:00:00"), "Saving must retain elapsed duration")
        expect(labels(content).contains("Stopping"), "Shutdown must not claim capture already stopped")

        var kept: [String] = []
        var processed: [String] = []
        control.onKeepLocal = { kept.append($0) }
        control.onProcess = { processed.append($0) }
        control.update(.init(phase: .review, sessionID: "held", elapsed: "03:00:00", pendingReviews: 1, processEnabled: false))
        let buttons = allViews(content).compactMap { $0 as? RecordingActionButton }
        let keep = buttons.first { $0.title == "Keep local" }!
        let process = buttons.first { $0.title == "Process entire recording" }!
        expect(!keep.isHiddenOrHasHiddenAncestor && keep.isEnabled, "Review needs Keep local")
        expect(!process.isEnabled, "Interrupted capture without metadata cannot be released")
        keep.performClick(nil)
        expect(kept == ["held"] && processed.isEmpty, "Keep local cannot process a recording")
        control.update(.init(phase: .review, sessionID: "ready", pendingReviews: 1, processEnabled: true))
        render(content, name: "review")
        process.performClick(nil)
        expect(processed == ["ready"], "Process choice carries reviewed session identity")
        control.update(.init(phase: .review, sessionID: "unreadable", processEnabled: false, keepLocalEnabled: false))
        expect(!keep.isEnabled && !process.isEnabled, "An unreadable review must not offer unsafe disposition actions")
        control.update(.init())
        expect(!panel.isVisible, "Idle control must stay hidden after completed recording")
        expect(!stop.isEnabled, "Idle cannot stop a recording")

        // Exercise long real-world errors without shrinking the timer or actions.
        control.update(.init(phase: .attention, sessionID: "error", elapsed: "19:51:00",
            microphoneStatus: "Unavailable after three recovery attempts", systemAudioStatus: "Recording",
            videoStatus: "Screen capture failed", detail: String(repeating: "Capture needs review. ", count: 14),
            stopEnabled: true, captureNeedsAttention: true))
        expect(panel.frame.height > 100 && panel.frame.height < 1000, "Long diagnostics need bounded readable layout")
        expect(stop.isEnabled && !stop.isHiddenOrHasHiddenAncestor, "Long diagnostics cannot remove Stop")
        expect(NSScreen.screens.contains { $0.visibleFrame.intersects(panel.frame) }, "Recording controls must remain on screen")
        let settings = SettingsWindowController()
        var settingsState = QuillSettingsState()
        settingsState.isRecording = true
        settingsState.sessionID = "settings-first"
        settingsState.recordingRevision = 10
        settingsState.pauseEnabled = true
        settingsState.recordingActionTitle = "Stop recording"
        settings.update(settingsState)
        let settingsStop = button("quill-recording-toggle", in: settings.window!.contentView!)
        let settingsPause = button("quill-recording-pause", in: settings.window!.contentView!)
        let priorSettingsStop = settingsStop.onClick
        let priorSettingsPause = settingsPause.onClick
        settings.onStopRecording = { stopped.append(($0, $1)) }
        settings.onPauseRecording = { pauseRequests.append(($0, $1, $2)) }
        settingsState.sessionID = "settings-second"
        settingsState.recordingRevision = 11
        settings.update(settingsState)
        priorSettingsStop?()
        expect(stopped.last?.0 == "settings-first" && stopped.last?.1 == 10,
               "Settings Stop must retain the displayed session and revision")
        settingsStop.performClick(nil)
        expect(stopped.last?.0 == "settings-second" && stopped.last?.1 == 11,
               "Settings Stop must bind updated session and revision")
        settingsState.isPaused = true
        settingsState.recordingStatus = "Paused · 00:42:15"
        settingsState.recordingRevision = 12
        settings.update(settingsState)
        expect(settingsPause.title == "Resume" && settingsPause.isEnabled && settingsStop.isEnabled,
            "Settings must show Resume and Stop while paused")
        priorSettingsPause?()
        expect(pauseRequests.last?.0 == "settings-first" && pauseRequests.last?.1 == 10 && pauseRequests.last?.2 == false,
            "Settings pause action must retain its displayed recording identity and state")
        settingsPause.performClick(nil)
        expect(pauseRequests.last?.0 == "settings-second" && pauseRequests.last?.1 == 12 && pauseRequests.last?.2 == true,
            "Settings Resume must carry the new paused state")
        settingsState.pauseEnabled = false
        settingsState.pauseActionTitle = "Resuming…"
        settings.update(settingsState)
        expect(!settingsPause.isEnabled && settingsPause.title == "Resuming…", "Settings transitions cannot be repeated")
        legacySettingsChecks()
        print("Recording controls: session-bound stop/pause/resume, stable controls, countdown, capture failures, saving, review and compact visibility passed.")
    }

    @MainActor
    static func checkTranscriptionRetryMenu() {
        let menu = NSMenu()
        let controller = MenuBarController(menu: menu)
        guard let retry = menu.item(withTitle: "Retry unfinished transcriptions…"),
              let action = retry.action,
              let review = menu.item(withTitle: "Review meetings…") else {
            fatalError("Transcription retry menu missing")
        }
        expect(menu.index(of: retry) == menu.index(of: review) + 1,
            "Transcription retry belongs beside Review meetings")
        expect(retry.keyEquivalent.isEmpty, "Transcription retry must not add a keyboard shortcut")
        expect(!retry.isEnabled, "Retry must remain disabled until AppController enables it")
        var calls = 0
        var options = MenuBarOptions()
        controller.onRetryTranscriptions = {
            calls += 1
            options.transcriptionRetryInFlight = true
            controller.updateOptions(options)
        }
        // Dispatch directly, bypassing disabled-item tracking. The selector
        // must enforce the same eligibility as the visible item.
        NSApp.sendAction(action, to: retry.target, from: retry)
        expect(calls == 0, "Disabled retry selector must not call recovery")
        options.transcriptionRetryEnabled = true
        controller.updateOptions(options)
        expect(retry.isEnabled, "Eligible retry must be available")
        NSApp.sendAction(action, to: retry.target, from: retry)
        expect(calls == 1 && !retry.isEnabled && retry.title == "Checking unfinished transcriptions…",
            "The pending scan must disable repeat requests and show its busy label")
        NSApp.sendAction(action, to: retry.target, from: retry)
        expect(calls == 1, "Repeated selector dispatch while recovery is busy must be ignored")
        options.transcriptionRetryInFlight = false
        options.transcriptionRetryEnabled = false
        controller.updateOptions(options)
        expect(!retry.isEnabled && retry.title == "Retry unfinished transcriptions…",
            "Completion must restore the action label without inventing eligibility")
        NSApp.sendAction(action, to: retry.target, from: retry)
        expect(calls == 1, "A no-longer-eligible retry must stay guarded")
        options.transcriptionRetryEnabled = true
        controller.updateOptions(options)
        NSApp.sendAction(action, to: retry.target, from: retry)
        expect(calls == 2, "A later eligible retry must be possible")
        controller.onRetryTranscriptions = nil
    }

    @MainActor
    static func legacySettingsChecks() {
        let controller = SettingsWindowController()
        let views = allViews(controller.window!.contentView!)
        let buttons = views.compactMap { $0 as? NSButton }
        let screen = buttons.first { $0.title == "Include screen video" }!
        let record = buttons.first { $0.accessibilityIdentifier() == "quill-recording-toggle" }!
        let scope = views.compactMap { $0 as? NSPopUpButton }.first {
            $0.accessibilityLabel() == "Screen capture scope"
        }!
        let target = views.compactMap { $0 as? NSPopUpButton }.first {
            $0.accessibilityLabel() == "Screen capture target"
        }!
        let refresh = buttons.first { $0.title == "Refresh" }!
        let speakers = buttons.first { $0.title == "Identify speakers locally" }!

        // Reproduce the misleading old combination: a selected whole display
        // must not look enabled when the actual screen-recording setting is off.
        var state = QuillSettingsState()
        state.captureScope = .entireScreen
        state.captureTargets = [.init(identifier: "display:1", label: "Entire screen 1")]
        state.selectedCaptureTargetID = "display:1"
        controller.update(state)
        precondition(screen.state == .off)
        precondition(!scope.isEnabled && !target.isEnabled && !refresh.isEnabled)
        precondition(record.title == "Start audio recording" && record.isEnabled)
        precondition(!speakers.isEnabled && speakers.state == .off)
        state.localTranscription = true
        controller.update(state)
        precondition(speakers.isEnabled)
        var speakerChanges = 0
        controller.onSpeakerIdentificationChange = { enabled in
            speakerChanges += 1
            state.identifySpeakers = enabled
            controller.update(state)
        }
        speakers.performClick(nil)
        precondition(speakerChanges == 1 && speakers.state == .on)
        controller.onSpeakerIdentificationChange = { _ in controller.update(state) }
        speakers.performClick(nil)
        precondition(speakers.state == .on, "A failed speaker setting save must restore persisted state")

        var recordingActions = 0
        controller.onToggleRecording = { recordingActions += 1 }
        record.performClick(nil)
        precondition(recordingActions == 1, "Visible Start did not reach the recording action")

        var changedVideo = false
        controller.onIncludeScreenVideoChange = { enabled in
            changedVideo = enabled
            state.includeScreenVideo = enabled
            state.recordingActionTitle = enabled ? "Start audio + screen" : "Start audio recording"
            controller.update(state)
        }
        screen.performClick(nil)
        precondition(changedVideo && screen.state == .on)
        precondition(scope.isEnabled && target.isEnabled && refresh.isEnabled)
        precondition(record.title == "Start audio + screen")

        // A failed settings save must render the persisted state again; it
        // cannot leave a checked control that never took effect.
        controller.onIncludeScreenVideoChange = { _ in controller.update(state) }
        screen.performClick(nil)
        precondition(screen.state == .on && record.title == "Start audio + screen")

        state.isRecording = true
        state.sessionID = "legacy-settings-session"
        state.recordingRevision = 1
        controller.onStopRecording = { id, revision in
            precondition(id == "legacy-settings-session" && revision == 1)
            recordingActions += 1
        }
        state.recordingStatus = "Starting audio + screen · waiting for video…"
        state.recordingActionTitle = "Cancel recording"
        controller.update(state)
        precondition(!screen.isEnabled && !scope.isEnabled && !target.isEnabled)
        precondition(!speakers.isEnabled, "A recording retains its start-time speaker preference")
        precondition(record.isEnabled && record.title == "Cancel recording")

        state.recordingStatus = "Recording audio + screen · 0:01"
        state.recordingActionTitle = "Stop recording"
        controller.update(state)
        record.performClick(nil)
        precondition(recordingActions == 2, "Visible Stop did not reach the recording action")

        state.recordingActionTitle = "Saving recording…"
        state.recordingActionEnabled = false
        controller.update(state)
        record.performClick(nil)
        precondition(recordingActions == 2, "Saving must reject another start/stop")

        state.isRecording = false
        state.includeScreenVideo = false
        state.recordingIssue = "Screen access was lost."
        state.recordingNeedsAttention = true
        controller.update(state)
        let issue = views.compactMap { $0 as? NSTextField }.first { $0.stringValue == state.recordingIssue }!
        precondition(!issue.isHidden, "Capture failure disappeared when video was disabled")
        let login = buttons.first { $0.accessibilityIdentifier() == "quill-launch-at-login" }!
        let loginSettings = buttons.first { $0.title == "Open Login Items…" }!
        precondition(!login.isEnabled && login.state == .off && loginSettings.isHidden)
        var loginChanges: [Bool] = []
        controller.onLaunchAtLoginChange = { enabled in
            loginChanges.append(enabled)
            state.loginItem.enabled = enabled
            state.loginItem.requiresApproval = false
            controller.update(state)
        }
        state.loginItem.canChange = true
        controller.update(state)
        login.performClick(nil)
        precondition(loginChanges == [true] && login.state == .on)
        // Failed OS registration/unregistration must not leave a guessed checkbox state.
        controller.onLaunchAtLoginChange = { _ in controller.update(state) }
        login.performClick(nil)
        precondition(login.state == .on)
        state.loginItem.enabled = false
        state.loginItem.requiresApproval = true
        state.loginItem.detail = "Waiting for approval"
        controller.update(state)
        precondition(login.state == .mixed && !loginSettings.isHidden)
        var openedLoginSettings = false
        controller.onOpenLoginSettings = { openedLoginSettings = true }
        loginSettings.performClick(nil)
        precondition(openedLoginSettings)
        controller.onLaunchAtLoginChange = { loginChanges.append($0) }
        login.performClick(nil)
        precondition(loginChanges == [true, false], "Clicking a pending item must cancel it")
        print("14 recording/startup-control checks passed (no recording or permission requests)")
    }

    @MainActor static func checkMeetingReminder() {
        let reminder = MeetingReminderController(displayUpdates: false)
        let panel = NSApp.windows.first { $0.title == "Quill meeting reminder" }!
        let content = panel.contentView!
        let start = button("quill-reminder-start", in: content)
        let skip = button("quill-reminder-skip", in: content)
        var starts: [String] = [], skips: [String] = []
        reminder.onStart = { starts.append($0) }
        reminder.onSkip = { skips.append($0) }
        reminder.update(meetingID: "first", canStart: true,
            detail: "You are in a meeting, but Quill has not started recording. Start it now, or skip this meeting.")
        expect(!panel.isVisible && start.title == "Start recording", "Reminder checks cannot open a real warning")
        expect(labels(content).contains("Meeting is not recording"), "Reminder needs an explicit missing-recording heading")
        let oldStart = start.onClick, oldSkip = skip.onClick
        reminder.update(meetingID: "second", canStart: false, canSkip: false,
            detail: "Quill has not finished starting. Check its recording controls and any permission request.")
        oldStart?(); oldSkip?()
        expect(starts == ["first"] && skips == ["first"], "Reminder actions must retain the displayed meeting")
        start.performClick(nil); skip.performClick(nil)
        expect(starts == ["first", "second"] && skips == ["first"], "Busy reminders check capture without offering a skip")
        expect(start.title == "Check recording…" && !skip.isEnabled, "Busy actions must describe their behavior")
        reminder.update(meetingID: "second", canStart: true,
            detail: "Automatic recording is off. Start this recording, or skip this meeting.")
        render(content, name: "meeting-not-recording")
        expect(panel.collectionBehavior.contains(.canJoinAllSpaces) && !panel.hidesOnDeactivate,
            "Reminder must remain available when switching apps or Spaces")
        reminder.update(meetingID: nil)
        expect(start.onClick == nil && skip.onClick == nil && !panel.isVisible, "Departed calls must lose actions and warning")
        print("Meeting reminder controls passed (no recording or visible windows)")
    }

    @MainActor static func checkRecordingHeartbeat() {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let path = directory.appendingPathComponent("recording-status.json")
        defer { try? FileManager.default.removeItem(at: directory) }
        let heartbeat = RecordingStatusHeartbeat(path: path)
        heartbeat.publish(phase: "idle", deliberatelySkipped: ["exact-call"], now: 100)
        let data = try! Data(contentsOf: path)
        let object = try! JSONSerialization.jsonObject(with: data) as! [String: Any]
        expect(object["phase"] as? String == "idle" && object["version"] as? Int == 1,
            "Browser heartbeat must distinguish idle from recording")
        expect(object["deliberatelySkipped"] as? [String] == ["exact-call"], "Skip identity must be preserved")
        let attributes = try! FileManager.default.attributesOfItem(atPath: path.path)
        expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600, "Heartbeat must be private")
        heartbeat.publish(phase: "idle", deliberatelySkipped: ["exact-call"], now: 100.2)
        expect(try! Data(contentsOf: path) == data, "Identical rapid refreshes should not rewrite the heartbeat")
        heartbeat.publish(phase: "recording", deliberatelySkipped: [], now: 100.3)
        let changed = try! JSONSerialization.jsonObject(with: Data(contentsOf: path)) as! [String: Any]
        expect(changed["phase"] as? String == "recording" && changed["timestamp"] as? Double == 100.3,
            "A real capture transition must publish immediately")
        heartbeat.publish(phase: "idle", deliberatelySkipped: Set((0..<80).map { "call-\($0)" } + ["../bad"]), now: 102)
        let bounded = try! JSONSerialization.jsonObject(with: Data(contentsOf: path)) as! [String: Any]
        let ids = bounded["deliberatelySkipped"] as! [String]
        expect(ids.count == 64 && !ids.contains("../bad"), "Published skips must fit the bridge's bounded identity contract")
        heartbeat.invalidate()
        expect(!FileManager.default.fileExists(atPath: path.path), "Quit must invalidate previous recording confirmation")
        print("Recording heartbeat publication checks passed")
    }

    @MainActor static func render(_ content: NSView, name: String) {
        guard let path = ProcessInfo.processInfo.environment["QUILL_CONTROL_SNAPSHOT_DIRECTORY"] else { return }
        let directory = URL(fileURLWithPath: path, isDirectory: true)
        try! FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        content.layoutSubtreeIfNeeded()
        guard let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) else {
            fatalError("Could not allocate native view image")
        }
        content.cacheDisplay(in: content.bounds, to: bitmap)
        guard let data = bitmap.representation(using: .png, properties: [:]) else {
            fatalError("Could not encode native view image")
        }
        try! data.write(to: directory.appendingPathComponent("native-\(name).png"))
    }

    @MainActor static func button(_ identifier: String, in view: NSView) -> RecordingActionButton {
        allViews(view).compactMap { $0 as? RecordingActionButton }
            .first { $0.accessibilityIdentifier() == identifier }!
    }
    @MainActor static func allViews(_ view: NSView) -> [NSView] {
        [view] + view.subviews.flatMap { allViews($0) }
    }
    @MainActor static func labels(_ view: NSView) -> [String] {
        allViews(view).compactMap { $0 as? NSTextField }
            .filter { !$0.isHiddenOrHasHiddenAncestor }.map(\.stringValue)
    }
    static func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }
}
