import AppKit

enum QuillCaptureScope: String {
    case meetingWindow
    case entireScreen
}

struct QuillCaptureTarget: Equatable {
    let identifier: String
    let label: String
}

enum QuillConnection {
    case googleDrive
}

/// The app owns persistence, capture discovery, and connector setup. This window
/// only displays that state and reports explicit user actions.
struct QuillSettingsState {
    var includeScreenVideo = false
    var recordingStatus = "Ready · audio only"
    var recordingActionTitle = "Start audio recording"
    var recordingActionEnabled = true
    var recordingNeedsAttention = false
    var recordingIssue: String?
    var captureScope: QuillCaptureScope = .meetingWindow
    var captureTargets: [QuillCaptureTarget] = []
    var selectedCaptureTargetID: String?
    var captureTargetsLoading = false
    var captureTargetDetail: String?
    var localTranscription = false
    var identifySpeakers = false
    var isRecording = false
    var isPaused = false
    var pauseEnabled = false
    var pauseActionTitle: String?
    var sessionID: String?
    var recordingRevision: Int?
    var notesStatus = "Not configured. Processing is off."
    var googleDriveStatus = "Not configured. Uploads are off."
    var processingTitle = "No processing status yet"
    var processingDetail = ""
    var processingNeedsAttention = false
    var processingReviewAvailable = false
    var notesActive = false
    var canEnableNotes = false
    var driveActive = false
    var canEnableDrive = false
    var loginItem = QuillLoginItemState()
    var checkInHours = 2
    var showInDock = true
}

@MainActor
final class SettingsWindowController: NSWindowController {
    var onToggleRecording: (() -> Void)?
    var onStopRecording: ((String, Int?) -> Void)?
    var onPauseRecording: ((String, Int, Bool) -> Void)?
    var onIncludeScreenVideoChange: ((Bool) -> Void)?
    var onCaptureScopeChange: ((QuillCaptureScope) -> Void)?
    var onCaptureTargetChange: ((String) -> Void)?
    var onRefreshCaptureTargets: (() -> Void)?
    var onTranscriptionChange: ((Bool) -> Void)?
    var onSpeakerIdentificationChange: ((Bool) -> Void)?
    var onSetupConnection: ((QuillConnection) -> Void)?
    var onSetupNotes: (() -> Void)?
    var onReviewProcessing: (() -> Void)?
    var onNotesChange: ((Bool) -> Void)?
    var onDriveChange: ((Bool) -> Void)?
    var onLaunchAtLoginChange: ((Bool) -> Void)?
    var onOpenLoginSettings: (() -> Void)?
    var onCheckInHoursChange: ((Int) -> Void)?
    var onShowInDockChange: ((Bool) -> Void)?

    private var state = QuillSettingsState()
    private let statusLabel = NSTextField(wrappingLabelWithString: "")
    private let issueLabel = NSTextField(wrappingLabelWithString: "")
    private let recordButton = RecordingActionButton(title: "Start audio recording")
    private let pauseButton = RecordingActionButton(title: "Pause")
    private let screenSwitch = NSButton(checkboxWithTitle: "Include screen video", target: nil, action: nil)
    private let scopePopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let targetPopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let targetLabel = NSTextField(labelWithString: "Meeting window")
    private let captureHelp = NSTextField(wrappingLabelWithString: "")
    private let captureDetail = NSTextField(wrappingLabelWithString: "")
    private let refreshButton = NSButton(title: "Refresh", target: nil, action: nil)
    private let transcriptionButton = NSButton(checkboxWithTitle: "Transcribe locally", target: nil, action: nil)
    private let speakersButton = NSButton(checkboxWithTitle: "Identify speakers locally", target: nil, action: nil)
    private let recordingDetail = NSTextField(wrappingLabelWithString: "")
    private let notesStatus = NSTextField(wrappingLabelWithString: "")
    private let processingStatus = NSTextField(wrappingLabelWithString: "")
    private let processingDetail = NSTextField(wrappingLabelWithString: "")
    private let processingButton = NSButton(title: "Review processing…", target: nil, action: nil)
    private let driveStatus = NSTextField(wrappingLabelWithString: "")
    private let notesSwitch = NSButton(checkboxWithTitle: "Create notes and suggest a company", target: nil, action: nil)
    private let driveSwitch = NSButton(checkboxWithTitle: "Save recordings and notes to Drive", target: nil, action: nil)
    private let loginSwitch = NSButton(checkboxWithTitle: "Launch at login", target: nil, action: nil)
    private let loginDetail = NSTextField(wrappingLabelWithString: "")
    private let checkInPopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let dockSwitch = NSButton(checkboxWithTitle: "Show Quill in Dock", target: nil, action: nil)
    private let loginSettingsButton = NSButton(title: "Open Login Items…", target: nil, action: nil)

    init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 500, height: 660),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Quill Settings"
        window.isReleasedWhenClosed = false
        super.init(window: window)
        buildContent()
        update(state)
        window.center()
    }

    required init?(coder: NSCoder) { nil }

    func show() {
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func update(_ state: QuillSettingsState) {
        self.state = state
        statusLabel.stringValue = state.recordingStatus
        statusLabel.textColor = state.recordingNeedsAttention ? .systemRed : state.isPaused ? .systemOrange : .labelColor
        issueLabel.stringValue = state.recordingIssue ?? ""
        issueLabel.isHidden = state.recordingIssue?.isEmpty != false
        recordButton.title = state.recordingActionTitle
        recordButton.isEnabled = state.recordingActionEnabled
        let renderedSessionID = state.sessionID
        let renderedRevision = state.recordingRevision
        let renderedRecording = state.isRecording || state.isPaused
        recordButton.onClick = { [weak self] in
            guard let self else { return }
            if renderedRecording, let renderedSessionID { self.onStopRecording?(renderedSessionID, renderedRevision) }
            else if !renderedRecording, !self.state.isRecording, !self.state.isPaused { self.onToggleRecording?() }
        }
        let renderedPaused = state.isPaused
        let canChangePause = state.pauseEnabled && renderedRecording && renderedSessionID != nil && renderedRevision != nil
        pauseButton.title = state.pauseActionTitle ?? (renderedPaused ? "Resume" : "Pause")
        pauseButton.isHidden = !renderedRecording
        pauseButton.isEnabled = canChangePause
        pauseButton.image = NSImage(systemSymbolName: renderedPaused ? "play.fill" : "pause.fill", accessibilityDescription: nil)
        pauseButton.imagePosition = .imageLeading
        pauseButton.setAccessibilityLabel(renderedPaused ? "Resume recording" : "Pause recording")
        pauseButton.toolTip = renderedPaused ? "Continue the current recording." : "Pause all recording tracks."
        pauseButton.onClick = { [weak self] in
            if canChangePause, let renderedSessionID, let renderedRevision {
                self?.onPauseRecording?(renderedSessionID, renderedRevision, renderedPaused)
            }
        }
        screenSwitch.state = state.includeScreenVideo ? .on : .off
        screenSwitch.isEnabled = !renderedRecording
        scopePopup.selectItem(at: state.captureScope == .meetingWindow ? 0 : 1)
        let canSelectScreen = state.includeScreenVideo && !renderedRecording && !state.captureTargetsLoading
        scopePopup.isEnabled = canSelectScreen
        targetLabel.stringValue = state.captureScope == .meetingWindow ? "Meeting window" : "Display"
        updateCaptureTargets()
        targetPopup.isEnabled = canSelectScreen && !state.captureTargets.isEmpty
        refreshButton.isEnabled = canSelectScreen
        refreshButton.title = state.captureTargetsLoading ? "Refreshing…" : "Refresh"
        captureHelp.stringValue = !state.includeScreenVideo
            ? "Screen video is off. Only microphone and system audio will be recorded. Turn on Include screen video to capture the selected window or display."
            : state.captureScope == .meetingWindow
            ? "Keep the meeting in its own Chrome window. Other windows stay out of the video. Switching tabs here may appear briefly before video stops."
            : "Records everything visible on the selected display, including other windows."
        captureDetail.stringValue = state.captureTargetDetail ?? ""
        captureDetail.isHidden = !state.includeScreenVideo || state.captureTargetDetail?.isEmpty != false
        transcriptionButton.state = state.localTranscription ? .on : .off
        transcriptionButton.isEnabled = !state.isRecording
        speakersButton.state = state.identifySpeakers ? .on : .off
        speakersButton.isEnabled = state.localTranscription && !state.isRecording
        recordingDetail.stringValue = state.isRecording
            ? "Capture and transcription settings can be changed after recording stops."
            : "Transcription runs on this Mac after recording stops."
        notesStatus.stringValue = state.notesStatus
        processingStatus.stringValue = state.processingTitle
        processingStatus.textColor = state.processingNeedsAttention ? .systemOrange : .labelColor
        processingDetail.stringValue = state.processingDetail
        processingDetail.isHidden = state.processingDetail.isEmpty
        processingButton.isEnabled = state.processingReviewAvailable
        driveStatus.stringValue = state.googleDriveStatus
        notesSwitch.state = state.notesActive ? .on : .off
        notesSwitch.isEnabled = state.notesActive || state.canEnableNotes
        driveSwitch.state = state.driveActive ? .on : .off
        driveSwitch.isEnabled = state.driveActive || state.canEnableDrive
        loginSwitch.state = state.loginItem.requiresApproval ? .mixed : state.loginItem.enabled ? .on : .off
        loginSwitch.isEnabled = state.loginItem.canChange
        loginDetail.stringValue = state.loginItem.detail
        loginSettingsButton.isHidden = !state.loginItem.requiresApproval
        checkInPopup.selectItem(withTag: state.checkInHours)
        checkInPopup.isEnabled = !renderedRecording
        dockSwitch.state = state.showInDock ? .on : .off
    }

    private func updateCaptureTargets() {
        let placeholder: String
        if state.captureTargetsLoading {
            placeholder = "Loading available targets…"
        } else if let selected = state.selectedCaptureTargetID,
                  !state.captureTargets.contains(where: { $0.identifier == selected }) {
            placeholder = "Selected target is unavailable"
        } else {
            placeholder = state.captureScope == .meetingWindow ? "Select a Chrome window…" : "Select a display…"
        }
        let displayedTargets = targetPopup.itemArray.dropFirst().map {
            QuillCaptureTarget(identifier: $0.representedObject as? String ?? "", label: $0.title)
        }
        // Elapsed-time updates must not tear down a popup the user is browsing.
        if targetPopup.itemArray.first?.title != placeholder || displayedTargets != state.captureTargets {
            targetPopup.removeAllItems()
            targetPopup.addItem(withTitle: placeholder)
            targetPopup.lastItem?.isEnabled = false
            for target in state.captureTargets {
                let item = NSMenuItem(title: target.label, action: nil, keyEquivalent: "")
                item.representedObject = target.identifier
                item.toolTip = target.label
                targetPopup.menu?.addItem(item)
            }
        }
        if let selected = state.selectedCaptureTargetID,
           let item = targetPopup.itemArray.first(where: { $0.representedObject as? String == selected }) {
            if targetPopup.selectedItem !== item { targetPopup.select(item) }
        } else if targetPopup.indexOfSelectedItem != 0 {
            targetPopup.selectItem(at: 0)
        }
        targetPopup.toolTip = targetPopup.selectedItem?.title
    }

    private func buildContent() {
        guard let content = window?.contentView else { return }
        // Connection messages and capture errors can wrap beyond the normal
        // window height. Keep every control reachable without resizing content.
        let scroll = NSScrollView()
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        content.addSubview(scroll)
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: content.topAnchor),
            scroll.bottomAnchor.constraint(equalTo: content.bottomAnchor)
        ])
        let document = SettingsDocumentView()
        document.translatesAutoresizingMaskIntoConstraints = false
        scroll.documentView = document
        document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor).isActive = true

        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 12
        root.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: document.leadingAnchor, constant: 24),
            root.trailingAnchor.constraint(equalTo: document.trailingAnchor, constant: -24),
            root.topAnchor.constraint(equalTo: document.topAnchor, constant: 22),
            root.bottomAnchor.constraint(equalTo: document.bottomAnchor, constant: -24)
        ])

        add(heading("Recording"), to: root)
        statusLabel.font = .systemFont(ofSize: NSFont.systemFontSize, weight: .semibold)
        statusLabel.setAccessibilityLabel("Recording status")
        statusLabel.setContentCompressionResistancePriority(.required, for: .vertical)
        add(statusLabel, to: root)
        add(detail(issueLabel), to: root)
        issueLabel.textColor = .systemRed
        screenSwitch.target = self
        screenSwitch.action = #selector(screenVideoChanged)
        add(screenSwitch, to: root)
        recordButton.bezelStyle = .rounded
        recordButton.setAccessibilityIdentifier("quill-recording-toggle")
        pauseButton.setAccessibilityIdentifier("quill-recording-pause")
        let recordingActions = NSStackView(views: [pauseButton, recordButton])
        recordingActions.orientation = .horizontal
        recordingActions.alignment = .centerY
        recordingActions.distribution = .fillEqually
        recordingActions.spacing = 8
        add(recordingActions, to: root)
        add(detail(NSTextField(wrappingLabelWithString: "Manual recording works with Discord and other apps. Use Stop recording when the call ends.")), to: root)

        let apps = NSTextField(labelWithString: "Meet & Zoom in Chrome")
        apps.textColor = .secondaryLabelColor
        add(row(label: "Automatic detection", control: apps), to: root)

        scopePopup.addItems(withTitles: ["Meeting window", "Entire screen"])
        scopePopup.target = self
        scopePopup.action = #selector(scopeChanged)
        scopePopup.setAccessibilityLabel("Screen capture scope")
        add(row(label: "Capture", control: scopePopup), to: root)

        targetPopup.target = self
        targetPopup.action = #selector(targetChanged)
        targetPopup.menu?.autoenablesItems = false
        targetPopup.setAccessibilityLabel("Screen capture target")
        targetPopup.cell?.lineBreakMode = .byTruncatingMiddle
        targetPopup.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        refreshButton.target = self
        refreshButton.action = #selector(refreshClicked)
        let targetControls = NSStackView(views: [targetPopup, refreshButton])
        targetControls.orientation = .horizontal
        targetControls.spacing = 8
        targetControls.alignment = .centerY
        add(targetLabel, to: root)
        add(targetControls, to: root)
        targetPopup.widthAnchor.constraint(greaterThanOrEqualToConstant: 260).isActive = true
        add(detail(captureHelp), to: root)
        add(detail(captureDetail), to: root)

        transcriptionButton.target = self
        transcriptionButton.action = #selector(transcriptionChanged)
        add(transcriptionButton, to: root)
        add(detail(recordingDetail), to: root)
        speakersButton.target = self
        speakersButton.action = #selector(speakersChanged)
        add(speakersButton, to: root)
        add(detail(NSTextField(wrappingLabelWithString: "Names come from clear introductions. Other voices keep speaker numbers; overlapping speech may remain unidentified.")), to: root)

        add(separator(), to: root)
        add(heading("Recording safeguards"), to: root)
        for hours in [1, 2, 4] {
            checkInPopup.addItem(withTitle: "\(hours) \(hours == 1 ? "hour" : "hours")")
            checkInPopup.lastItem?.tag = hours
        }
        checkInPopup.target = self
        checkInPopup.action = #selector(checkInChanged)
        checkInPopup.setAccessibilityLabel("Recording check-in after")
        add(row(label: "Check in after", control: checkInPopup), to: root)
        add(detail(NSTextField(wrappingLabelWithString: "After 5 minutes without a reply, Quill stops and saves locally for review. Continue adds 1 hour. This applies to automatic and manual Quill recordings.")), to: root)
        add(row(label: "Recording indicator", control: NSTextField(labelWithString: "Always visible")), to: root)
        dockSwitch.target = self
        dockSwitch.action = #selector(dockChanged)
        add(dockSwitch, to: root)
        add(detail(NSTextField(wrappingLabelWithString: "Click Quill in the Dock to show recording controls. macOS can hide menu-bar icons when space is limited.")), to: root)
        add(separator(), to: root)
        add(heading("After the meeting"), to: root)
        processingButton.target = self
        processingButton.action = #selector(processingClicked)
        processingButton.setAccessibilityIdentifier("quill-review-processing")
        add(row(label: "Processing", control: processingButton), to: root)
        add(detail(processingStatus), to: root)
        add(detail(processingDetail), to: root)
        let notesButton = NSButton(title: "Configuration…", target: self, action: #selector(notesSetupClicked))
        notesButton.setAccessibilityLabel("Open meeting notes configuration")
        add(row(label: "Meeting notes & action items", control: notesButton), to: root)
        notesSwitch.target = self
        notesSwitch.action = #selector(notesChanged)
        add(notesSwitch, to: root)
        add(detail(notesStatus), to: root)
        add(detail(NSTextField(wrappingLabelWithString: "Requires local transcription. Turning off notes also turns off Drive filing.")), to: root)

        add(separator(), to: root)
        add(heading("Connections"), to: root)
        let driveButton = NSButton(title: "Configuration…", target: self, action: #selector(driveSetupClicked))
        driveButton.setAccessibilityLabel("Open Google Drive configuration")
        add(row(label: "Google Drive", control: driveButton), to: root)
        driveSwitch.target = self
        driveSwitch.action = #selector(driveChanged)
        add(driveSwitch, to: root)
        add(detail(driveStatus), to: root)
        add(detail(NSTextField(wrappingLabelWithString: "Requires meeting notes. Filing keeps notes private; sharing with attendees needs a separate approval.")), to: root)
        add(detail(NSTextField(wrappingLabelWithString: "Switching on applies to new completed meetings. Work held while off stays on hold. Work already running may finish.")), to: root)
        add(separator(), to: root)
        add(heading("Startup"), to: root)
        loginSwitch.allowsMixedState = true
        loginSwitch.target = self
        loginSwitch.action = #selector(loginChanged)
        loginSwitch.setAccessibilityIdentifier("quill-launch-at-login")
        add(loginSwitch, to: root)
        add(detail(loginDetail), to: root)
        loginSettingsButton.target = self
        loginSettingsButton.action = #selector(loginSettingsClicked)
        add(loginSettingsButton, to: root)
    }

    private func add(_ view: NSView, to stack: NSStackView) {
        stack.addArrangedSubview(view)
        view.translatesAutoresizingMaskIntoConstraints = false
        view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }

    private func row(label: String, control: NSView) -> NSStackView {
        let label = NSTextField(labelWithString: label)
        let space = NSView()
        space.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let row = NSStackView(views: [label, space, control])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 12
        control.setContentHuggingPriority(.required, for: .horizontal)
        return row
    }

    private func heading(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: NSFont.systemFontSize, weight: .semibold)
        return label
    }

    private func detail(_ label: NSTextField) -> NSTextField {
        label.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        label.textColor = .secondaryLabelColor
        label.preferredMaxLayoutWidth = 452
        label.setContentCompressionResistancePriority(.required, for: .vertical)
        return label
    }

    private func separator() -> NSBox {
        let box = NSBox()
        box.boxType = .separator
        return box
    }

    @objc private func scopeChanged() {
        guard state.includeScreenVideo, !state.isRecording, !state.isPaused, !state.captureTargetsLoading else { return }
        let scope: QuillCaptureScope = scopePopup.indexOfSelectedItem == 0 ? .meetingWindow : .entireScreen
        onCaptureScopeChange?(scope)
        update(state)
    }

    @objc private func targetChanged() {
        guard state.includeScreenVideo, !state.isRecording, !state.isPaused, !state.captureTargetsLoading,
              let target = targetPopup.selectedItem?.representedObject as? String else { return }
        onCaptureTargetChange?(target)
        update(state)
    }

    @objc private func refreshClicked() {
        guard state.includeScreenVideo, !state.isRecording, !state.isPaused, !state.captureTargetsLoading else { return }
        onRefreshCaptureTargets?()
    }

    @objc private func screenVideoChanged() {
        guard !state.isRecording, !state.isPaused else { return }
        onIncludeScreenVideoChange?(screenSwitch.state == .on)
        update(state)
    }

    @objc private func transcriptionChanged() {
        guard !state.isRecording else { return }
        onTranscriptionChange?(transcriptionButton.state == .on)
        update(state)
    }

    @objc private func speakersChanged() {
        guard state.localTranscription, !state.isRecording else { return }
        onSpeakerIdentificationChange?(speakersButton.state == .on)
        update(state)
    }

    @objc private func notesSetupClicked() { onSetupNotes?() }
    @objc private func processingClicked() { onReviewProcessing?() }
    @objc private func driveSetupClicked() { onSetupConnection?(.googleDrive) }
    @objc private func notesChanged() {
        guard state.notesActive || state.canEnableNotes else { return }
        onNotesChange?(notesSwitch.state == .on)
        update(state)
    }
    @objc private func driveChanged() {
        guard state.driveActive || state.canEnableDrive else { return }
        onDriveChange?(driveSwitch.state == .on)
        update(state)
    }
    @objc private func loginChanged() {
        guard state.loginItem.canChange else { return }
        onLaunchAtLoginChange?(!state.loginItem.enabled && !state.loginItem.requiresApproval)
        update(state)
    }
    @objc private func checkInChanged() {
        guard !state.isRecording, !state.isPaused, let hours = checkInPopup.selectedItem?.tag, [1, 2, 4].contains(hours) else { return }
        onCheckInHoursChange?(hours)
        update(state)
    }
    @objc private func dockChanged() {
        onShowInDockChange?(dockSwitch.state == .on)
        update(state)
    }
    @objc private func loginSettingsClicked() { onOpenLoginSettings?() }
}

private final class SettingsDocumentView: NSView {
    override var isFlipped: Bool { true }
}
