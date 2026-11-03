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
        checkTranscriptionCancellationMenus()
        checkWarningHistoryMenu()
        checkApplicationMenus()
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
        checkPanelGeometry(panel, content: content, compact: false)
        render(content, name: "recording")
        expect(!panel.isVisible, "Offscreen checks must not reveal a panel")
        expect(stop.isEnabled && !stop.isHiddenOrHasHiddenAncestor, "Recording needs a visible Stop")
        checkStableRecordingClock(control, panel: panel, content: content)
        control.update(initial)
        var historyRequests = 0
        control.onWarningHistory = { historyRequests += 1 }
        let history = button("quill-status-warning-history", in: content)
        expect(!history.isHiddenOrHasHiddenAncestor, "Expanded controls must expose recent warnings")
        history.performClick(nil)
        expect(historyRequests == 1 && stopped.isEmpty, "Warning history must not change capture")
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
        checkPanelGeometry(panel, content: content, compact: true)
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
        driveSettingsChecks()
        print("Recording controls: session-bound stop/pause/resume, stable timer rendering, warning history, countdown, capture failures, saving, review and compact visibility passed.")
    }

    @MainActor
    static func checkStableRecordingClock(_ controller: RecordingStatusController, panel: NSWindow, content: NSView) {
        func state(_ elapsed: String, revision: Int = 20, attention: Bool = false) -> RecordingStatusSnapshot {
            .init(phase: .recording, sessionID: "clock-test", elapsed: elapsed,
                microphoneStatus: "Recording", systemAudioStatus: "Recording", videoStatus: "Entire display",
                detail: attention ? "Meeting detection is unavailable. Recording continues." : nil,
                actionRevision: revision, stopEnabled: true, captureNeedsAttention: attention, pauseEnabled: true)
        }
        controller.update(state("00:04:00"))
        let stop = button("quill-status-stop", in: content)
        let pause = button("quill-status-pause", in: content)
        let collapse = allViews(content).compactMap { $0 as? RecordingActionButton }
            .first { $0.accessibilityLabel() == "Collapse recording controls" }!
        let marker = allViews(content).compactMap { $0 as? NSImageView }.first!
        let originalStop = stop.onClick
        var preservedActionCalls = 0
        // A sentinel catches action rebinding, which object-identity checks
        // alone miss when a timer update rewrites an existing button.
        stop.onClick = { preservedActionCalls += 1 }
        let originalTooltip = pause.toolTip
        pause.toolTip = "Unchanged hover target"
        let pauseImage = pause.image, collapseImage = collapse.image, markerImage = marker.image
        let frame = panel.frame
        for seconds in 1...30 {
            let next = state(String(format: "00:04:%02d", seconds))
            controller.update(next)
            controller.update(next)
            expect(panel.frame == frame, "Timer-only and duplicate updates must preserve the panel frame")
            expect(pause.image === pauseImage && collapse.image === collapseImage && marker.image === markerImage,
                "Timer-only updates must not replace symbol images")
            expect(pause.toolTip == "Unchanged hover target", "Timer-only updates must not reset a hovered tooltip")
        }
        stop.performClick(nil)
        expect(preservedActionCalls == 1, "Timer-only updates must preserve existing action bindings")
        expect(labels(content).contains("00:04:30"), "The visible timer must keep advancing")
        pause.toolTip = originalTooltip
        stop.onClick = originalStop
        collapse.performClick(nil)
        controller.update(state("00:04:31", attention: true))
        expect(labels(content).contains("Recording · Needs attention")
            && labels(content).contains("Meeting detection is unavailable. Recording continues."),
            "A real warning must expand and render immediately despite the timer fast path")
        controller.update(state("00:04:32", revision: 21))
        expect(labels(content).contains("Recording") && !labels(content).contains("Recording · Needs attention"),
            "Warning recovery and action revisions must update immediately")
        expect(pause.isEnabled && stop.isEnabled && !panel.isVisible,
            "Clock checks must preserve functional controls without displaying a window")
    }

    @MainActor
    static func checkApplicationMenus() {
        let visibleWindows = Set(NSApp.windows.filter(\.isVisible).map(\.windowNumber))
        let menu = NSMenu()
        let controller = MenuBarController(menu: menu, layout: .application)
        expect(menu.items.map(\.title) == ["Quill", "Recording", "Meetings"],
            "Application actions need discoverable Quill, Recording, and Meetings menus")
        func submenu(_ title: String) -> NSMenu {
            guard let value = menu.item(withTitle: title)?.submenu else { fatalError("Missing \(title) menu") }
            expect(!value.autoenablesItems, "Application menus must preserve controller-supplied eligibility")
            return value
        }
        func item(_ title: String, in menu: NSMenu) -> NSMenuItem {
            guard let value = menu.item(withTitle: title), value.action != nil, value.target != nil else {
                fatalError("Missing application action: \(title)")
            }
            return value
        }
        func invoke(_ item: NSMenuItem) {
            guard let action = item.action else { fatalError("Menu action missing") }
            expect(NSApp.sendAction(action, to: item.target, from: item), "Menu action must reach its controller")
        }
        let application = submenu("Quill")
        let recording = submenu("Recording")
        let meetings = submenu("Meetings")
        let about = item("About Quill…", in: application)
        let settings = item("Settings…", in: application)
        guard let quit = application.items.first(where: { $0.title.lowercased() == "quit quill" }) else {
            fatalError("Application Quit action missing")
        }
        expect(about.isEnabled && settings.keyEquivalent == "," && quit.keyEquivalent == "q",
            "Application menu must retain About, Settings, and the standard Quit shortcut")
        let toggle = item("Start audio recording", in: recording)
        let pause = item("Pause recording", in: recording)
        let automatic = item("Record automatically", in: recording)
        let video = item("Include screen video", in: recording)
        let controls = item("Show recording controls", in: recording)
        let skip = item("Skip this meeting", in: recording)
        let retryMeeting = item("Retry this meeting", in: recording)
        let review = item("Review meetings…", in: meetings)
        let folder = item("Open recordings folder", in: meetings)
        let retryTranscription = item("Retry unfinished transcriptions…", in: meetings)
        let history = item("Recent warnings…", in: meetings)
        expect(toggle.keyEquivalent == "r" && folder.keyEquivalent == "o",
            "Application menus must retain recording and folder shortcuts")
        expect(pause.isHidden && !pause.isEnabled && retryMeeting.isHidden && !skip.isEnabled,
            "Idle menus must not offer pause, a failed-meeting retry, or an unavailable skip")

        var navigation: [String] = []
        var starts = 0, skips = 0, transcriptionRetries = 0
        var automaticChanges: [Bool] = [], videoChanges: [Bool] = [], meetingRetries: [String] = []
        var stops: [(String, Int?)] = []
        var pauses: [(String, Int, Bool)] = []
        controller.onSettings = { navigation.append("settings") }
        controller.onQuit = { navigation.append("quit") }
        controller.onShowControls = { navigation.append("controls") }
        controller.onReviewMeetings = { navigation.append("review") }
        controller.onOpenFolder = { navigation.append("folder") }
        controller.onWarningHistory = { navigation.append("history") }
        controller.onToggle = { starts += 1 }
        controller.onSkipMeeting = { skips += 1 }
        controller.onRetryMeeting = { meetingRetries.append($0) }
        controller.onRetryTranscriptions = { transcriptionRetries += 1 }
        controller.onAutomaticRecordingChange = { automaticChanges.append($0) }
        controller.onIncludeScreenVideoChange = { videoChanges.append($0) }
        controller.onStopRecording = { stops.append(($0, $1)) }
        controller.onPauseRecording = { pauses.append(($0, $1, $2)) }

        var options = MenuBarOptions()
        options.reviewAvailable = true
        options.pendingReviews = 2
        options.recordAutomatically = true
        options.includeScreenVideo = true
        options.canSkipMeeting = true
        options.failedMeetingToRetry = "failed-call"
        options.transcriptionRetryEnabled = true
        controller.updateOptions(options)
        expect(review.title == "Review meetings (2)…" && review.isEnabled,
            "Meetings menu must show pending reviews without losing its action")
        expect(toggle.title == "Start audio + screen" && automatic.state == .on && video.state == .on,
            "Recording menu must show the saved preferences and matching Start label")
        expect(skip.isEnabled && retryMeeting.isEnabled && !retryMeeting.isHidden && retryTranscription.isEnabled,
            "Eligible skip and retry actions must be available from application menus")
        for item in [settings, controls, review, folder, history, quit] { invoke(item) }
        expect(navigation == ["settings", "controls", "review", "folder", "history", "quit"],
            "Application navigation must call the same existing handlers without changing capture")
        expect(starts == 0 && stops.isEmpty && pauses.isEmpty, "Navigation must not start, stop, or pause a recording")
        invoke(toggle); invoke(automatic); invoke(video); invoke(skip); invoke(retryMeeting); invoke(retryTranscription)
        expect(starts == 1 && automaticChanges == [false] && videoChanges == [false] && skips == 1
            && meetingRetries == ["failed-call"] && transcriptionRetries == 1,
            "Application recording actions must dispatch their exact existing requests")

        options.configurationEnabled = false
        options.transcriptionRetryInFlight = true
        controller.updateOptions(options)
        expect(!automatic.isEnabled && !video.isEnabled && !skip.isEnabled && !retryMeeting.isEnabled
            && !retryTranscription.isEnabled, "Unavailable application actions must appear disabled")
        for item in [automatic, video, skip, retryMeeting, retryTranscription] { invoke(item) }
        expect(automaticChanges == [false] && videoChanges == [false] && skips == 1
            && meetingRetries == ["failed-call"] && transcriptionRetries == 1,
            "Direct dispatch must not bypass existing disabled-action guards")

        options.configurationEnabled = true
        options.transcriptionRetryInFlight = false
        controller.updateOptions(options)
        controller.updateStatus(.init(phase: .recording, sessionID: "application-first", elapsed: "00:00:10",
            actionRevision: 11, stopEnabled: true, pauseEnabled: true))
        controller.update(recording: true, elapsed: "00:00:10")
        expect(toggle.title == "Stop recording" && pause.isEnabled && !pause.isHidden,
            "Active recording must expose Stop and Pause in the application menu")
        expect(!video.isEnabled && !retryMeeting.isEnabled, "Capture must disable changing video and retrying another meeting")
        invoke(video); invoke(retryMeeting)
        expect(videoChanges == [false] && meetingRetries == ["failed-call"],
            "Application dispatch cannot change video or retry a meeting during capture")

        guard let tracking = recording.delegate else { fatalError("Recording submenu needs action tracking") }
        tracking.menuWillOpen?(recording)
        controller.updateStatus(.init(phase: .paused, sessionID: "application-second", elapsed: "00:00:20",
            actionRevision: 12, stopEnabled: true, pauseEnabled: true))
        invoke(toggle); invoke(pause)
        expect(stops.count == 1 && stops[0].0 == "application-first" && stops[0].1 == 11,
            "A Stop selected from an open application menu must retain its displayed session and revision")
        expect(pauses.count == 1 && pauses[0].0 == "application-first" && pauses[0].1 == 11 && !pauses[0].2,
            "An open-menu Pause must not become Resume for a different recording")
        expect(starts == 1, "A stale Stop must never become Start")
        tracking.menuDidClose?(recording)
        tracking.menuWillOpen?(recording)
        invoke(toggle); invoke(pause)
        expect(stops.count == 2 && stops[1].0 == "application-second" && stops[1].1 == 12,
            "Reopening the application menu must bind Stop to the current recording")
        expect(pauses.count == 2 && pauses[1].0 == "application-second" && pauses[1].1 == 12 && pauses[1].2,
            "Reopening the application menu must bind Resume to the current paused revision")
        tracking.menuDidClose?(recording)
        controller.updateStatus(.init(phase: .resuming, sessionID: "application-second",
            actionRevision: 13, stopEnabled: true, pauseEnabled: true))
        expect(!pause.isEnabled && pause.title == "Resuming recording…",
            "Application Pause must remain disabled while resuming")
        invoke(pause)
        expect(pauses.count == 2, "A disabled transition cannot request another pause or resume")

        controller.updateStatus(.init())
        controller.update(recording: false, elapsed: nil)
        var shortcutActions: [String] = []
        controller.onToggle = { shortcutActions.append("record") }
        controller.onOpenFolder = { shortcutActions.append("folder") }
        controller.onSettings = { shortcutActions.append("settings") }
        controller.onQuit = { shortcutActions.append("quit") }
        let shortcuts: [(key: String, code: UInt16, action: String)] = [
            ("r", 15, "record"), ("o", 31, "folder"), (",", 43, "settings"), ("q", 12, "quit")
        ]
        for (index, shortcut) in shortcuts.enumerated() {
            guard let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: .command,
                timestamp: 0, windowNumber: 0, context: nil, characters: shortcut.key,
                charactersIgnoringModifiers: shortcut.key, isARepeat: false, keyCode: shortcut.code) else {
                fatalError("Could not construct application shortcut event")
            }
            expect(menu.performKeyEquivalent(with: event), "Application menu must handle Command-\(shortcut.key)")
            expect(shortcutActions == shortcuts.prefix(index + 1).map(\.action),
                "Each application shortcut must dispatch its intended callback exactly once")
        }
        expect(stops.count == 2 && pauses.count == 2,
            "Idle shortcut checks must not stop or pause a recording")
        expect(Set(NSApp.windows.filter(\.isVisible).map(\.windowNumber)) == visibleWindows,
            "Application menus must not create another status surface or open a window")
        print("Application menus: grouped actions, callbacks, keyboard shortcuts, state, guarded actions, and session-bound Stop/Pause passed")
    }

    @MainActor
    static func checkTranscriptionCancellationMenus() {
        for layout in [MenuBarController.Layout.statusItem, .application] {
            let top = NSMenu()
            let controller = MenuBarController(menu: top, layout: layout)
            let menu = top.item(withTitle: "Meetings")?.submenu ?? top
            guard let item = menu.item(withTitle: "Cancel transcription"), let action = item.action,
                  let tracking = menu.delegate else { fatalError("Missing session-bound cancellation control") }
            var cancelled: [UUID] = []
            controller.onCancelTranscription = { _, id in cancelled.append(id) }
            let first = UUID(), next = UUID()
            controller.updateTranscription("Preparing", cancelTarget: ("same-name", first))
            expect(!item.isHidden && item.isEnabled, "Preparing transcription must be cancellable")
            tracking.menuWillOpen?(menu)
            // The next run reuses the folder name, but not its reservation.
            controller.updateTranscription("Transcribing", cancelTarget: ("same-name", next))
            NSApp.sendAction(action, to: item.target, from: item)
            expect(!item.isEnabled && cancelled.isEmpty, "A stale Cancel cannot move to the next recording")
            tracking.menuDidClose?(menu)
            tracking.menuWillOpen?(menu)
            NSApp.sendAction(action, to: item.target, from: item)
            expect(cancelled == [next], "A fresh menu must cancel the displayed transcription")
            tracking.menuDidClose?(menu)
            controller.updateTranscription(nil)
            NSApp.sendAction(action, to: item.target, from: item)
            expect(item.isHidden && !item.isEnabled && cancelled == [next], "Idle Cancel cannot dispatch")
        }
    }

    @MainActor
    static func checkWarningHistoryMenu() {
        let menu = NSMenu()
        let controller = MenuBarController(menu: menu)
        guard let history = menu.item(withTitle: "Recent warnings…"), let action = history.action,
              let controls = menu.item(withTitle: "Show recording controls") else {
            fatalError("Warning history menu missing")
        }
        expect(history.isEnabled && history.keyEquivalent.isEmpty,
            "Recent warnings should be available without a capture shortcut")
        expect(menu.index(of: history) == menu.index(of: controls) + 1,
            "Warning history belongs beside recording controls")
        var historyRequests = 0, captureRequests = 0
        controller.onWarningHistory = { historyRequests += 1 }
        controller.onToggle = { captureRequests += 1 }
        controller.onStopRecording = { _, _ in captureRequests += 1 }
        controller.onPauseRecording = { _, _, _ in captureRequests += 1 }
        NSApp.sendAction(action, to: history.target, from: history)
        controller.updateStatus(.init(phase: .recording, sessionID: "menu-history", elapsed: "00:10:00",
            actionRevision: 1, stopEnabled: true, pauseEnabled: true))
        expect(history.isEnabled, "Reading warning history must remain available during recording")
        NSApp.sendAction(action, to: history.target, from: history)
        expect(historyRequests == 2 && captureRequests == 0,
            "Warning history must open without toggling, stopping, or pausing capture")
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
    static func driveSettingsChecks() {
        let controller = SettingsWindowController()
        let content = controller.window!.contentView!
        let buttons = allViews(content).compactMap { $0 as? NSButton }
        let documents = buttons.first { $0.accessibilityIdentifier() == "quill-drive-documents" }!
        let recordings = buttons.first { $0.accessibilityIdentifier() == "quill-drive-recordings" }!
        var state = QuillSettingsState()
        var documentChanges: [Bool] = [], recordingChanges: [Bool] = []
        controller.onDriveDocumentsChange = { enabled in
            documentChanges.append(enabled)
            state.driveDocumentsActive = enabled
            controller.update(state)
        }
        controller.onDriveRecordingsChange = { enabled in
            recordingChanges.append(enabled)
            state.driveRecordingsActive = enabled
            controller.update(state)
        }
        controller.update(state)
        documents.performClick(nil)
        recordings.performClick(nil)
        expect(documentChanges.isEmpty && recordingChanges.isEmpty,
               "Unconfigured Drive controls must not request activation")

        let accountButton = buttons.first { $0.accessibilityIdentifier() == "quill-drive-account-choose" }!
        var accountChoices = 0
        controller.onChooseDriveAccount = { accountChoices += 1 }
        expect(accountButton.title == "Choose account…" && accountButton.isEnabled,
               "Unconfigured Drive must offer account selection")
        accountButton.performClick(nil)
        expect(accountChoices == 1 && documentChanges.isEmpty && recordingChanges.isEmpty,
               "Choosing an account must not enable filing")
        state.driveAccountEmail = "owner@example.com"
        state.driveAccountStatus = "Connected as owner@example.com."
        state.driveAccountBusy = true
        controller.update(state)
        expect(!accountButton.isEnabled && accountButton.title == "Checking…",
               "Account verification cannot be started twice")
        state.driveAccountBusy = false
        controller.update(state)
        expect(labels(content).contains("Connected as owner@example.com.") && accountButton.title == "Change / verify…",
               "Settings must show the selected email and offer verification")

        state.canEnableDriveDocuments = true
        controller.update(state)
        documents.performClick(nil)
        expect(documents.state == .on && recordings.state == .off && !recordings.isEnabled,
               "Document filing must work independently of an unconfigured recording uploader")
        expect(documentChanges == [true] && recordingChanges.isEmpty,
               "A document choice must reach only the document callback")

        state.canEnableDriveRecordings = true
        controller.update(state)
        recordings.performClick(nil)
        documents.performClick(nil)
        expect(documents.state == .off && recordings.state == .on,
               "Turning off documents must leave recording uploads checked")
        expect(documentChanges == [true, false] && recordingChanges == [true],
               "Each checkbox must send only its own explicit choice")

        // A rejected save restores the persisted value without touching its peer.
        controller.onDriveDocumentsChange = { _ in controller.update(state) }
        documents.performClick(nil)
        expect(documents.state == .off && recordings.state == .on,
               "A rejected document change must not appear saved or change recordings")
        controller.onDriveRecordingsChange = { _ in controller.update(state) }
        recordings.performClick(nil)
        expect(recordings.state == .on, "A rejected recording change must restore the saved checkbox")

        // Even if a configured adapter becomes unavailable, an active choice can
        // still be switched off. Neither control may trap a user in uploads.
        state.canEnableDriveDocuments = false
        state.canEnableDriveRecordings = false
        state.driveDocumentsActive = true
        controller.update(state)
        expect(documents.isEnabled && recordings.isEnabled,
               "Active upload choices must remain available to switch off")
        controller.onDriveDocumentsChange = { enabled in
            documentChanges.append(enabled)
            state.driveDocumentsActive = enabled
            controller.update(state)
        }
        documents.performClick(nil)
        expect(documents.state == .off && !documents.isEnabled && recordings.state == .on,
               "Disabling unavailable document filing must preserve active media uploads")
        state.driveDocumentsTitle = "Save notes files to Drive"
        state.driveRecordingsTitle = "Upload recordings and transcripts to Drive"
        state.driveDocumentsStatus = "Saves separate Markdown and JSON notes files."
        state.driveRecordingsStatus = "Includes recording metadata and available transcripts."
        controller.update(state)
        expect(documents.title == state.driveDocumentsTitle && recordings.title == state.driveRecordingsTitle,
               "Legacy formats must display their actual upload choices")
        expect(labels(content).contains(state.driveDocumentsStatus) && labels(content).contains(state.driveRecordingsStatus),
               "The displayed explanations must retain legacy transcript scope")
        expect(!controller.window!.isVisible, "Drive control checks must not open the real settings window")
        print("Independent Drive controls: isolated actions, unavailable adapters and rejected saves passed")
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
        let originalStart = start.onClick
        var unchangedCalls = 0
        start.onClick = { unchangedCalls += 1 }
        let frame = panel.frame
        for _ in 0..<30 {
            reminder.update(meetingID: "first", canStart: true,
                detail: "You are in a meeting, but Quill has not started recording. Start it now, or skip this meeting.")
        }
        start.performClick(nil)
        expect(unchangedCalls == 1 && panel.frame == frame,
            "An unchanged reminder must not rebind controls or repeatedly reposition its window")
        start.onClick = originalStart
        expect(panel.animationBehavior == .none, "Reminders should not animate repeated presentation changes")
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

    @MainActor static func checkPanelGeometry(_ panel: NSWindow, content: NSView, compact: Bool) {
        content.layoutSubtreeIfNeeded()
        func hit(_ p: NSPoint) -> NSView? {
            content.hitTest(content.convert(p, to: content.superview))
        }
        let disclosure = button("quill-status-disclosure", in: content)
        let target = content.convert(disclosure.bounds, from: disclosure)
        let mode = compact ? "compact" : "expanded"
        expect(target.width >= 40 && target.height >= 28 && disclosure.isBordered,
            "Collapse and expand need a visible rectangular target: \(mode), target=\(target)")
        var clicks = 0
        let original = disclosure.onClick
        disclosure.onClick = { clicks += 1 }
        defer { disclosure.onClick = original }
        for x in [target.minX + 0.5, target.midX, target.maxX - 0.5] {
            for y in [target.minY + 0.5, target.midY, target.maxY - 0.5] {
                let point = NSPoint(x: x, y: y)
                let receiver = hit(point)
                let receiverClass = receiver.map { String(describing: type(of: $0)) } ?? "nil"
                let context = "\(mode), point=\(point), target=\(target), content=\(content.bounds), hit=\(receiverClass), descendant=\(receiver?.isDescendant(of: disclosure) == true)"
                expect(receiver === disclosure, "Button padding must accept the click: \(context)")
                // The panel stays offscreen. Dispatch to the view selected by
                // hitTest and let NSButton track the paired local mouse-up.
                func event(_ type: NSEvent.EventType) -> NSEvent {
                    NSEvent.mouseEvent(with: type, location: content.convert(point, to: nil), modifierFlags: [],
                        timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: panel.windowNumber,
                        context: nil, eventNumber: 1, clickCount: 1, pressure: 1)!
                }
                let previous = clicks
                NSApp.postEvent(event(.leftMouseUp), atStart: true)
                receiver?.mouseDown(with: event(.leftMouseDown))
                expect(clicks == previous + 1, "Routed padding click must invoke the action exactly once: \(context)")
            }
        }
        let stop = button(compact ? "quill-status-compact-stop" : "quill-status-stop", in: content)
        let stopFrame = content.convert(stop.bounds, from: stop)
        expect(!target.intersects(stopFrame) && hit(NSPoint(x: stopFrame.midX, y: stopFrame.midY)) === stop,
               "Disclosure must not intercept Stop")
        let b = content.bounds
        for x in [b.minX + 1, b.maxX - 1] {
            for y in [b.minY + 1, b.maxY - 1] {
                expect(hit(NSPoint(x: x, y: y)) == nil, "Transparent corners must not take clicks")
            }
        }
        let gutter = hit(NSPoint(x: b.midX, y: b.maxY - 4))
        expect(panel.isMovableByWindowBackground && gutter != nil && !(gutter is NSControl),
               "The top gutter must remain available for dragging")
        let center = NSPoint(x: target.midX, y: target.midY)
        disclosure.isEnabled = false
        let beforeDisabledClick = clicks
        (hit(center) as? NSButton)?.performClick(nil)
        expect(clicks == beforeDisabledClick, "Disabled disclosure must not invoke its action: \(mode)")
        disclosure.isEnabled = true
        disclosure.isHidden = true
        expect(hit(center) !== disclosure, "Hidden disclosure must not intercept its old location: \(mode)")
        disclosure.isHidden = false
        content.layoutSubtreeIfNeeded()
        guard let mask = (content as? NSVisualEffectView)?.maskImage,
              let bitmap = mask.tiffRepresentation.flatMap(NSBitmapImageRep.init(data:)) else {
            fatalError("A layer corner radius alone does not clip the behind-window material")
        }
        expect(bitmap.colorAt(x: 0, y: 0)!.alphaComponent < 0.02 &&
               bitmap.colorAt(x: bitmap.pixelsWide / 2, y: bitmap.pixelsHigh / 2)!.alphaComponent > 0.98 &&
               mask.capInsets.top == content.layer?.cornerRadius && mask.resizingMode == .stretch &&
               content.layer?.masksToBounds == true, "Material and child views need the same rounded corners")
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
