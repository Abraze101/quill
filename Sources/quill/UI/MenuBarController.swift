import AppKit

/// AppController supplies persisted preferences and the current meeting state.
/// Menu actions request changes; they never start capture or write settings here.
struct MenuBarOptions {
    var recordAutomatically = false
    var includeScreenVideo = false
    var canSkipMeeting = false
    var failedMeetingToRetry: String?
    var configurationEnabled = true
    var automationStatus: String?
    var stateOverride: String?
    var recordingActionTitle: String?
    var recordingActionEnabled: Bool?
    var pendingReviews = 0
    var reviewAvailable = false
    var transcriptionRetryEnabled = false
    var transcriptionRetryInFlight = false
    var processingTitle: String?
    var processingDetail = ""
    var processingNeedsAttention = false
}

/// A compact menu-bar entry point, backed by the floating recording control
/// and optional Dock icon when macOS has insufficient menu-bar space.
@MainActor
final class MenuBarController {
    private let statusItem: NSStatusItem
    private let stateLabel: NSMenuItem
    private let transcriptionLabel: NSMenuItem
    private let processingItem: NSMenuItem
    private let toggleItem: NSMenuItem
    private let pauseItem: NSMenuItem
    private let automaticItem: NSMenuItem
    private let screenVideoItem: NSMenuItem
    private let skipItem: NSMenuItem
    private let retryItem: NSMenuItem
    private let reviewItem: NSMenuItem
    private let transcriptionRetryItem: NSMenuItem
    private var options = MenuBarOptions()
    private var isRecording = false
    private var elapsed: String?
    private var statusSnapshot: RecordingStatusSnapshot?
    private let menuDelegate = RecordingMenuDelegate()
    private var menuIsTracking = false
    private var renderedStopID: String?
    private var renderedStopRevision: Int?
    private var renderedIsRecording = false
    private var renderedPaused = false
    private var renderedPauseEnabled = false

    var onToggle: (() -> Void)?
    var onOpenFolder: (() -> Void)?
    var onQuit: (() -> Void)?
    var onAutomaticRecordingChange: ((Bool) -> Void)?
    var onIncludeScreenVideoChange: ((Bool) -> Void)?
    var onSkipMeeting: (() -> Void)?
    var onRetryMeeting: ((String) -> Void)?
    var onSettings: (() -> Void)?
    var onReviewMeetings: (() -> Void)?
    var onRetryTranscriptions: (() -> Void)?
    var onShowControls: (() -> Void)?
    var onWarningHistory: (() -> Void)?
    var onStopRecording: ((String, Int?) -> Void)?
    var onPauseRecording: ((String, Int, Bool) -> Void)?

    init(menu: NSMenu = NSMenu()) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)

        statusItem.autosaveName = "QuillRecordingStatus"

        menu.autoenablesItems = false

        stateLabel = NSMenuItem(title: "idle", action: nil, keyEquivalent: "")
        stateLabel.isEnabled = false
        menu.addItem(stateLabel)

        transcriptionLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        transcriptionLabel.isEnabled = false
        transcriptionLabel.isHidden = true
        menu.addItem(transcriptionLabel)

        processingItem = NSMenuItem(title: "", action: #selector(reviewClicked), keyEquivalent: "")
        processingItem.isHidden = true
        menu.addItem(processingItem)

        let showControls = NSMenuItem(title: "Show recording controls", action: #selector(showControlsClicked), keyEquivalent: "")
        menu.addItem(showControls)
        let warningHistory = NSMenuItem(title: "Recent warnings…", action: #selector(warningHistoryClicked), keyEquivalent: "")
        menu.addItem(warningHistory)
        menu.addItem(.separator())

        automaticItem = NSMenuItem(
            title: "Record automatically", action: #selector(automaticClicked), keyEquivalent: ""
        )
        automaticItem.toolTip = "Start recording after joining a supported meeting in Chrome."
        menu.addItem(automaticItem)

        screenVideoItem = NSMenuItem(
            title: "Include screen video", action: #selector(screenVideoClicked), keyEquivalent: ""
        )
        screenVideoItem.toolTip = "Choose the meeting window or display in Settings before recording."
        menu.addItem(screenVideoItem)

        menu.addItem(.separator())

        toggleItem = NSMenuItem(
            title: "Start recording",
            action: #selector(toggleClicked),
            keyEquivalent: "r"
        )
        menu.addItem(toggleItem)

        pauseItem = NSMenuItem(title: "Pause recording", action: #selector(pauseClicked), keyEquivalent: "")
        pauseItem.isHidden = true
        pauseItem.isEnabled = false
        menu.addItem(pauseItem)

        skipItem = NSMenuItem(
            title: "Skip this meeting", action: #selector(skipClicked), keyEquivalent: ""
        )
        skipItem.isEnabled = false
        skipItem.toolTip = "Skip automatic recording for this meeting until you leave."
        menu.addItem(skipItem)

        retryItem = NSMenuItem(title: "Retry this meeting", action: #selector(retryClicked), keyEquivalent: "")
        retryItem.isHidden = true
        retryItem.isEnabled = false
        retryItem.toolTip = "After correcting Settings, retry the failed automatic recording. It will stop when you leave."
        menu.addItem(retryItem)

        menu.addItem(.separator())

        let openFolder = NSMenuItem(
            title: "Open recordings folder",
            action: #selector(openFolderClicked),
            keyEquivalent: "o"
        )
        menu.addItem(openFolder)

        reviewItem = NSMenuItem(title: "Review meetings…", action: #selector(reviewClicked), keyEquivalent: "")
        reviewItem.toolTip = "Choose company folders for completed meetings."
        menu.addItem(reviewItem)

        transcriptionRetryItem = NSMenuItem(title: "Retry unfinished transcriptions…",
                                           action: #selector(retryTranscriptionsClicked), keyEquivalent: "")
        transcriptionRetryItem.toolTip = "Check for unfinished local transcripts and retry eligible recordings."
        transcriptionRetryItem.isEnabled = false
        menu.addItem(transcriptionRetryItem)

        let settings = NSMenuItem(
            title: "Settings…", action: #selector(settingsClicked), keyEquivalent: ","
        )
        menu.addItem(settings)

        let about = NSMenuItem(title: "About Quill…", action: #selector(aboutClicked), keyEquivalent: "")
        menu.addItem(about)

        menu.addItem(.separator())

        let quit = NSMenuItem(
            title: "Quit quill",
            action: #selector(quitClicked),
            keyEquivalent: "q"
        )
        menu.addItem(quit)

        for item in [processingItem, showControls, warningHistory, automaticItem, screenVideoItem, toggleItem, pauseItem, skipItem, retryItem, openFolder, reviewItem, transcriptionRetryItem, settings, about, quit] {
            item.target = self
        }

        statusItem.menu = menu
        menu.delegate = menuDelegate
        menuDelegate.onWillOpen = { [weak self] in
            guard let self else { return }
            self.bindRecordingAction()
            self.menuIsTracking = true
        }
        menuDelegate.onDidClose = { [weak self] in self?.menuIsTracking = false }

        if let button = statusItem.button {
            let image = Self.featherImage()
            image?.isTemplate = true
            button.image = image
            button.imagePosition = .imageLeft
            button.setAccessibilityLabel("Quill recording controls")
        }
        refresh()
    }

    /// Reflect capture state in the compact REC marker and menu titles.
    func update(recording: Bool, elapsed: String?) {
        isRecording = recording
        self.elapsed = elapsed
        refresh()
    }

    func updateOptions(_ options: MenuBarOptions) {
        self.options = options
        refresh()
    }

    func updateStatus(_ snapshot: RecordingStatusSnapshot) {
        statusSnapshot = snapshot
        refresh()
    }

    private var captureIsOccupied: Bool {
        if isRecording || statusSnapshot?.stopEnabled == true { return true }
        switch statusSnapshot?.phase {
        case .starting, .recording, .pausing, .paused, .resuming, .stopping: return true
        default: return false
        }
    }

    private func bindRecordingAction() {
        renderedStopID = statusSnapshot?.sessionID
        renderedStopRevision = statusSnapshot?.actionRevision
        renderedIsRecording = captureIsOccupied
        renderedPaused = statusSnapshot?.phase == .paused
        renderedPauseEnabled = statusSnapshot?.canChangePause == true
    }

    private func refresh() {
        if !menuIsTracking { bindRecordingAction() }
        let paused = statusSnapshot?.phase == .paused
        let changingPause = statusSnapshot?.phase == .pausing || statusSnapshot?.phase == .resuming
        let sessionActive = isRecording || paused || changingPause
        let recordingState = sessionActive
            ? "\(paused ? "Paused" : "Recording") · \(statusSnapshot?.elapsed ?? elapsed ?? "0:00")"
            : (options.automationStatus ?? "Ready") + (options.includeScreenVideo ? " · audio + screen" : " · audio only")
        stateLabel.title = options.stateOverride ?? recordingState
        toggleItem.title = options.recordingActionTitle ?? (sessionActive ? "Stop recording"
            : options.includeScreenVideo ? "Start audio + screen" : "Start audio recording")
        toggleItem.isEnabled = options.recordingActionEnabled ?? options.configurationEnabled
        pauseItem.title = statusSnapshot?.phase == .pausing ? "Pausing recording…"
            : statusSnapshot?.phase == .resuming ? "Resuming recording…"
            : renderedPaused ? "Resume recording" : "Pause recording"
        pauseItem.isHidden = statusSnapshot?.phase != .recording && !paused && !changingPause
        pauseItem.isEnabled = renderedPauseEnabled && statusSnapshot?.canChangePause == true
        pauseItem.toolTip = renderedPaused ? "Continue the current recording." : "Pause all recording tracks."
        automaticItem.state = options.recordAutomatically ? .on : .off
        automaticItem.isEnabled = options.configurationEnabled
        screenVideoItem.state = options.includeScreenVideo ? .on : .off
        screenVideoItem.isEnabled = options.configurationEnabled && !sessionActive
        skipItem.isEnabled = options.configurationEnabled && options.canSkipMeeting
        retryItem.isHidden = options.failedMeetingToRetry == nil
        retryItem.isEnabled = options.configurationEnabled && !sessionActive && options.failedMeetingToRetry != nil
        reviewItem.title = options.pendingReviews > 0
            ? "Review meetings (\(options.pendingReviews))…" : "Review meetings…"
        reviewItem.isEnabled = options.reviewAvailable
        transcriptionRetryItem.title = options.transcriptionRetryInFlight
            ? "Checking unfinished transcriptions…" : "Retry unfinished transcriptions…"
        transcriptionRetryItem.isEnabled = options.transcriptionRetryEnabled && !options.transcriptionRetryInFlight
        processingItem.title = options.processingTitle.map { "\($0)…" } ?? ""
        processingItem.toolTip = options.processingDetail
        processingItem.isHidden = options.processingTitle == nil
        processingItem.isEnabled = options.reviewAvailable
        let needsAttention = statusSnapshot?.needsAttention == true || options.processingNeedsAttention
        let busy = statusSnapshot?.phase == .starting || statusSnapshot?.phase == .stopping
        statusItem.button?.title = changingPause ? " …" : paused ? (needsAttention ? " Paused !" : " Paused") : needsAttention ? (isRecording ? " REC !" : " !")
            : isRecording ? " REC" : busy ? " …" : ""
        statusItem.button?.font = .systemFont(ofSize: 11, weight: .semibold)
        statusItem.button?.contentTintColor = needsAttention || paused || changingPause ? .systemOrange : isRecording ? .systemRed : nil
        statusItem.button?.toolTip = "Quill — \(stateLabel.title)"
        statusItem.button?.setAccessibilityValue(stateLabel.title)
    }

    /// Show transcription progress/failure as a second status line in the
    /// menu; nil hides it. Independent of recording state — a new recording
    /// can run while the last one transcribes.
    func updateTranscription(_ text: String?) {
        transcriptionLabel.title = text ?? ""
        transcriptionLabel.isHidden = text == nil
    }

    // Inlined Lucide feather SVG. Keeping it in source means the executable
    // has no separate resource bundle to install alongside it — true
    // single-binary.
    private static let featherSVG = """
    <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" \
    viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" \
    stroke-linecap="round" stroke-linejoin="round">\
    <path d="M12.67 19a2 2 0 0 0 1.416-.588l6.154-6.172a6 6 0 0 0-8.49-8.49L5.586 9.914A2 2 0 0 0 5 11.328V18a1 1 0 0 0 1 1z"/>\
    <path d="M16 8 2 22"/>\
    <path d="M17.5 15H9"/>\
    </svg>
    """

    private static func featherImage() -> NSImage? {
        guard let data = featherSVG.data(using: .utf8),
              let image = NSImage(data: data)
        else { return nil }
        // Menu-bar status icons are nominally 18pt tall; size the SVG to match.
        image.size = NSSize(width: 16, height: 16)
        return image
    }

    @objc private func showControlsClicked() { onShowControls?() }
    @objc private func toggleClicked() {
        if renderedIsRecording {
            if let renderedStopID { onStopRecording?(renderedStopID, renderedStopRevision) }
        } else if !captureIsOccupied {
            onToggle?()
        }
    }
    @objc private func pauseClicked() {
        guard renderedPauseEnabled, let renderedStopID, let renderedStopRevision else { return }
        onPauseRecording?(renderedStopID, renderedStopRevision, renderedPaused)
    }
    @objc private func automaticClicked() {
        guard options.configurationEnabled else { return }
        onAutomaticRecordingChange?(!options.recordAutomatically)
    }
    @objc private func screenVideoClicked() {
        guard options.configurationEnabled, !captureIsOccupied else { return }
        onIncludeScreenVideoChange?(!options.includeScreenVideo)
    }
    @objc private func skipClicked() {
        guard options.configurationEnabled, options.canSkipMeeting else { return }
        onSkipMeeting?()
    }
    @objc private func retryClicked() {
        guard options.configurationEnabled, !captureIsOccupied,
              let meeting = options.failedMeetingToRetry else { return }
        onRetryMeeting?(meeting)
    }
    @objc private func settingsClicked() { onSettings?() }
    @objc private func warningHistoryClicked() { onWarningHistory?() }
    @objc private func aboutClicked() {
        NSApp.orderFrontStandardAboutPanel(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
    @objc private func reviewClicked() { onReviewMeetings?() }
    @objc private func retryTranscriptionsClicked() {
        guard options.transcriptionRetryEnabled, !options.transcriptionRetryInFlight else { return }
        onRetryTranscriptions?()
    }
    @objc private func openFolderClicked() { onOpenFolder?() }
    @objc private func quitClicked() { onQuit?() }
}

@MainActor
private final class RecordingMenuDelegate: NSObject, NSMenuDelegate {
    var onWillOpen: (() -> Void)?
    var onDidClose: (() -> Void)?
    func menuWillOpen(_ menu: NSMenu) { onWillOpen?() }
    func menuDidClose(_ menu: NSMenu) { onDidClose?() }
}
