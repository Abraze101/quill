import AppKit

/// The capture controller is the only source of truth. Text describes actual
/// tracks rather than the recording preferences selected for the next meeting.
struct RecordingStatusSnapshot: Equatable {
    enum Phase: Equatable { case idle, starting, recording, pausing, paused, resuming, stopping, attention, review }
    let phase: Phase
    let sessionID: String?
    private(set) var elapsed: String
    let microphoneStatus: String
    let systemAudioStatus: String
    let videoStatus: String
    let detail: String?
    let warningRevision: Int?
    let actionRevision: Int?
    let countdownSeconds: Int?
    let pendingReviews: Int
    let stopEnabled: Bool
    let processEnabled: Bool
    let captureNeedsAttention: Bool
    let keepLocalEnabled: Bool
    let pauseEnabled: Bool

    init(phase: Phase = .idle, sessionID: String? = nil, elapsed: String = "00:00:00",
         microphoneStatus: String = "Off", systemAudioStatus: String = "Off",
         videoStatus: String = "Off", detail: String? = nil,
         warningRevision: Int? = nil, actionRevision: Int? = nil, countdownSeconds: Int? = nil,
         pendingReviews: Int = 0, stopEnabled: Bool = false, processEnabled: Bool = true,
         captureNeedsAttention: Bool = false, keepLocalEnabled: Bool = true, pauseEnabled: Bool = false) {
        self.phase = phase
        self.sessionID = sessionID
        self.elapsed = elapsed
        self.microphoneStatus = microphoneStatus
        self.systemAudioStatus = systemAudioStatus
        self.videoStatus = videoStatus
        self.detail = detail
        self.warningRevision = warningRevision
        self.actionRevision = actionRevision
        self.countdownSeconds = countdownSeconds
        self.pendingReviews = pendingReviews
        self.stopEnabled = stopEnabled
        self.processEnabled = processEnabled
        self.captureNeedsAttention = captureNeedsAttention
        self.keepLocalEnabled = keepLocalEnabled
        self.pauseEnabled = pauseEnabled
    }

    var needsPersistentControl: Bool {
        switch phase {
        case .starting, .recording, .pausing, .paused, .resuming, .stopping, .attention, .review: true
        case .idle: false
        }
    }

    var needsAttention: Bool { phase == .attention || countdownSeconds != nil || captureNeedsAttention }

    var canChangePause: Bool {
        pauseEnabled && (phase == .recording || phase == .paused)
            && sessionID != nil && actionRevision != nil
    }

    /// The clock changes each second; its text does not change the controls,
    /// warning presentation, or the session identity bound to an action.
    func hasSameControls(as other: Self) -> Bool {
        var comparison = self
        comparison.elapsed = other.elapsed
        return comparison == other
    }

    var heading: String {
        switch phase {
        case .idle: "Ready to record"
        case .starting: "Starting recording…"
        case .recording: captureNeedsAttention ? "Recording · Needs attention" : "Recording"
        case .pausing: "Pausing recording…"
        case .paused: captureNeedsAttention ? "Paused · Needs attention" : "Paused"
        case .resuming: "Resuming recording…"
        case .stopping: "Saving recording…"
        case .attention: stopEnabled ? "Recording · Needs attention" : "Needs attention"
        case .review: "Saved locally for review"
        }
    }
}

/// A control surface, not a second recording owner. Its actions always carry
/// the identity rendered on the button so an old interaction cannot stop or
/// release a later recording.
@MainActor
final class RecordingStatusController: NSObject, NSWindowDelegate {
    var onStop: ((String, Int?) -> Void)?
    /// The final argument is the displayed pause state, never a blind toggle.
    var onPause: ((String, Int, Bool) -> Void)?
    var onContinue: ((String, Int) -> Void)?
    var onKeepLocal: ((String) -> Void)?
    var onProcess: ((String) -> Void)?
    var onOpenFolder: ((String) -> Void)?
    var onSettings: (() -> Void)?
    var onWarningHistory: (() -> Void)?

    private let panel: RecordingControlPanel
    private let root = NSStackView()
    private let expanded = NSStackView()
    private let heading = NSTextField(labelWithString: "Ready to record")
    private let marker = NSImageView()
    private let elapsed = NSTextField(labelWithString: "00:00:00")
    private let compactElapsed = NSTextField(labelWithString: "00:00:00")
    private let microphone = NSTextField(wrappingLabelWithString: "Off")
    private let systemAudio = NSTextField(wrappingLabelWithString: "Off")
    private let video = NSTextField(wrappingLabelWithString: "Off")
    private let tracks = NSStackView()
    private let detail = NSTextField(wrappingLabelWithString: "")
    private let warning = NSTextField(wrappingLabelWithString: "")
    private let stopButton = RecordingActionButton(title: "Stop & save")
    private let compactStopButton = RecordingActionButton(title: "Stop")
    private let pauseButton = RecordingActionButton(title: "Pause")
    private let compactPauseButton = RecordingActionButton(title: "Pause")
    private let captureActions = NSStackView()
    private let continueButton = RecordingActionButton(title: "Continue for 1 hour")
    private let keepLocalButton = RecordingActionButton(title: "Keep local")
    private let processButton = RecordingActionButton(title: "Process entire recording")
    private let openFolderButton = RecordingActionButton(title: "Show recording in Finder")
    private let collapseButton = RecordingActionButton(title: "")
    private let dismissButton = RecordingActionButton(title: "Close")
    private let reviewActions = NSStackView()
    private var snapshot = RecordingStatusSnapshot()
    private var compact: Bool
    private var manuallyShown = false
    private var adjustingFrame = false
    private let defaults: UserDefaults
    private let displayUpdates: Bool
    private let positionKey = "QuillRecordingControlPosition"
    private let compactKey = "QuillRecordingControlCompact"
    private var contentWidth: NSLayoutConstraint?

    var windowNumber: Int { panel.windowNumber }

    init(defaults: UserDefaults = .standard, displayUpdates: Bool = true) {
        self.defaults = defaults
        self.displayUpdates = displayUpdates
        compact = defaults.bool(forKey: compactKey)
        panel = RecordingControlPanel(
            contentRect: NSRect(x: 0, y: 0, width: 366, height: 320),
            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false
        )
        super.init()
        panel.delegate = self
        panel.title = "Quill recording controls"
        panel.setAccessibilityLabel("Quill recording controls")
        panel.isReleasedWhenClosed = false
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.canHide = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.isMovableByWindowBackground = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .canJoinAllApplications, .stationary]
        panel.animationBehavior = .none
        // Defense in depth only. ScreenCaptureKit exclusion is owned by the
        // capture filter; this flag alone is not a recording exclusion.
        panel.sharingType = .none
        buildContent()
        restorePosition()
        NotificationCenter.default.addObserver(self, selector: #selector(screenLayoutChanged),
            name: NSApplication.didChangeScreenParametersNotification, object: nil)
        render()
    }

    func update(_ next: RecordingStatusSnapshot) {
        let old = snapshot
        snapshot = next
        if (next.warningRevision != old.warningRevision && next.countdownSeconds != nil)
            || (next.phase == .attention && (old.phase != .attention || next.detail != old.detail))
            || (next.captureNeedsAttention && (!old.captureNeedsAttention || next.detail != old.detail))
            || next.phase == .review {
            compact = false
        }
        if next.phase == .idle && old.phase != .idle { manuallyShown = false }
        if next != old {
            if next.hasSameControls(as: old) { renderElapsed() }
            else { render() }
        }
        if next.needsPersistentControl || manuallyShown {
            // Order without activation: another app keeps its keyboard focus.
            if displayUpdates && (!panel.isVisible || !old.needsPersistentControl) { panel.orderFrontRegardless() }
        } else if panel.isVisible {
            panel.orderOut(nil)
        }
        if displayUpdates && (next.phase != old.phase || next.needsAttention != old.needsAttention
            || next.pendingReviews != old.pendingReviews) { updateDockBadge() }
    }

    func show() {
        manuallyShown = true
        compact = false
        render()
        constrainToVisibleScreen()
        if displayUpdates { panel.orderFrontRegardless() }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard !snapshot.needsPersistentControl else { return false }
        manuallyShown = false
        return true
    }

    func windowDidMove(_ notification: Notification) {
        guard !adjustingFrame else { return }
        constrainToVisibleScreen()
        savePosition()
    }

    @objc private func screenLayoutChanged() {
        constrainToVisibleScreen()
        savePosition()
    }

    private func buildContent() {
        let material = NSVisualEffectView()
        material.material = .hudWindow
        material.blendingMode = .behindWindow
        material.state = .active
        material.wantsLayer = true
        material.layer?.cornerRadius = 14
        material.layer?.masksToBounds = true
        panel.contentView = material
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 14
        root.translatesAutoresizingMaskIntoConstraints = false
        material.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: material.leadingAnchor, constant: 16),
            root.trailingAnchor.constraint(equalTo: material.trailingAnchor, constant: -16),
            root.topAnchor.constraint(equalTo: material.topAnchor, constant: 14),
            root.bottomAnchor.constraint(equalTo: material.bottomAnchor, constant: -14)
        ])
        contentWidth = root.widthAnchor.constraint(equalToConstant: 334)
        contentWidth?.isActive = true
        heading.font = .systemFont(ofSize: 13, weight: .semibold)
        heading.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        marker.imageScaling = .scaleProportionallyDown
        marker.setAccessibilityElement(false)
        marker.widthAnchor.constraint(equalToConstant: 14).isActive = true
        marker.heightAnchor.constraint(equalToConstant: 14).isActive = true
        compactElapsed.font = .monospacedDigitSystemFont(ofSize: 13, weight: .medium)
        compactElapsed.setAccessibilityLabel("Recording duration")
        compactStopButton.controlSize = .small
        compactPauseButton.controlSize = .small
        collapseButton.controlSize = .small
        collapseButton.isBordered = false
        collapseButton.onClick = { [weak self] in
            guard let self else { return }
            self.compact.toggle()
            self.defaults.set(self.compact, forKey: self.compactKey)
            self.render()
        }
        let header = NSStackView(views: [marker, heading, spacer(), compactElapsed, compactPauseButton, compactStopButton, collapseButton])
        header.orientation = .horizontal
        header.spacing = 8
        header.alignment = .centerY
        add(header, to: root)

        expanded.orientation = .vertical
        expanded.alignment = .leading
        expanded.spacing = 12
        add(expanded, to: root)
        elapsed.font = .monospacedDigitSystemFont(ofSize: 29, weight: .medium)
        elapsed.setAccessibilityLabel("Recording duration")
        add(elapsed, to: expanded)
        tracks.orientation = .vertical
        tracks.alignment = .leading
        tracks.spacing = 9
        add(trackRow("Microphone", value: microphone, symbol: "mic"), to: tracks)
        add(trackRow("System audio", value: systemAudio, symbol: "speaker.wave.2"), to: tracks)
        add(trackRow("Screen video", value: video, symbol: "rectangle.on.rectangle"), to: tracks)
        add(tracks, to: expanded)
        detail.font = .systemFont(ofSize: 12)
        detail.textColor = .labelColor
        detail.setContentCompressionResistancePriority(.required, for: .vertical)
        add(detail, to: expanded)
        warning.font = .systemFont(ofSize: 13, weight: .medium)
        warning.textColor = .systemOrange
        warning.setAccessibilityLabel("Recording check-in")
        warning.setContentCompressionResistancePriority(.required, for: .vertical)
        add(warning, to: expanded)
        add(continueButton, to: expanded)
        stopButton.bezelColor = .systemRed
        stopButton.contentTintColor = .white
        compactStopButton.bezelColor = .systemRed
        compactStopButton.contentTintColor = .white
        stopButton.setAccessibilityIdentifier("quill-status-stop")
        compactStopButton.setAccessibilityIdentifier("quill-status-compact-stop")
        pauseButton.setAccessibilityIdentifier("quill-status-pause")
        compactPauseButton.setAccessibilityIdentifier("quill-status-compact-pause")
        continueButton.setAccessibilityIdentifier("quill-status-continue")
        captureActions.orientation = .horizontal
        captureActions.alignment = .centerY
        captureActions.spacing = 8
        captureActions.distribution = .fillEqually
        captureActions.addArrangedSubview(pauseButton)
        captureActions.addArrangedSubview(stopButton)
        add(captureActions, to: expanded)
        reviewActions.orientation = .vertical
        reviewActions.alignment = .leading
        reviewActions.spacing = 8
        add(keepLocalButton, to: reviewActions)
        add(processButton, to: reviewActions)
        add(openFolderButton, to: reviewActions)
        add(reviewActions, to: expanded)
        let settingsButton = RecordingActionButton(title: "Recording safeguards…")
        settingsButton.controlSize = .small
        settingsButton.onClick = { [weak self] in self?.onSettings?() }
        let historyButton = RecordingActionButton(title: "Recent warnings…")
        historyButton.controlSize = .small
        historyButton.setAccessibilityIdentifier("quill-status-warning-history")
        historyButton.onClick = { [weak self] in self?.onWarningHistory?() }
        let history = NSStackView(views: [historyButton, spacer()])
        history.orientation = .horizontal
        history.alignment = .centerY
        add(history, to: expanded)
        dismissButton.controlSize = .small
        dismissButton.onClick = { [weak self] in
            guard let self, !self.snapshot.needsPersistentControl else { return }
            self.manuallyShown = false
            if self.panel.isVisible { self.panel.orderOut(nil) }
        }
        let footer = NSStackView(views: [settingsButton, spacer(), dismissButton])
        footer.orientation = .horizontal
        footer.alignment = .centerY
        add(footer, to: expanded)
    }

    private func render() {
        let paused = snapshot.phase == .paused
        let changingPause = snapshot.phase == .pausing || snapshot.phase == .resuming
        let active = snapshot.phase == .recording || (snapshot.phase == .attention && snapshot.stopEnabled)
        let canCollapse = snapshot.phase != .idle && snapshot.phase != .review
        let isCompact = compact && canCollapse
        heading.stringValue = isCompact
            ? snapshot.phase == .stopping ? "Saving" : paused ? "Paused" : active ? (snapshot.needsAttention ? "REC !" : "REC") : snapshot.heading
            : snapshot.heading
        heading.toolTip = snapshot.heading
        marker.image = NSImage(systemSymbolName: snapshot.needsAttention ? "exclamationmark.triangle.fill"
            : paused ? "pause.circle.fill" : snapshot.phase == .review ? "checkmark.circle.fill" : "record.circle.fill", accessibilityDescription: nil)
        marker.contentTintColor = snapshot.needsAttention || paused || changingPause ? .systemOrange : active ? .systemRed : .secondaryLabelColor
        elapsed.isHidden = snapshot.phase == .idle || snapshot.phase == .review
        renderElapsed()
        expanded.isHidden = isCompact
        compactElapsed.isHidden = !isCompact
        compactStopButton.isHidden = !isCompact
        compactStopButton.title = snapshot.phase == .stopping ? "Saving…" : "Stop"
        let showsPause = snapshot.phase == .recording || paused || changingPause
        pauseButton.isHidden = !showsPause
        compactPauseButton.isHidden = !isCompact || !showsPause
        pauseButton.title = snapshot.phase == .pausing ? "Pausing…" : snapshot.phase == .resuming ? "Resuming…" : paused ? "Resume" : "Pause"
        compactPauseButton.title = pauseButton.title
        pauseButton.isEnabled = snapshot.canChangePause
        compactPauseButton.isEnabled = pauseButton.isEnabled
        for button in [pauseButton, compactPauseButton] {
            button.setAccessibilityLabel(paused ? "Resume recording" : "Pause recording")
            button.toolTip = paused ? "Resume microphone, system audio, and screen recording."
                : "Pause all recording tracks. Resume continues this recording."
            button.image = NSImage(systemSymbolName: paused ? "play.fill" : "pause.fill", accessibilityDescription: nil)
            button.imagePosition = .imageLeading
        }
        collapseButton.isHidden = !canCollapse
        collapseButton.image = NSImage(systemSymbolName: isCompact ? "arrow.up.left.and.arrow.down.right" : "minus",
            accessibilityDescription: isCompact ? "Expand recording controls" : "Collapse recording controls")
        collapseButton.setAccessibilityLabel(isCompact ? "Expand recording controls" : "Collapse recording controls")
        collapseButton.toolTip = isCompact ? "Expand recording controls" : "Collapse to recording pill"
        tracks.isHidden = snapshot.phase == .idle || snapshot.phase == .review
        microphone.stringValue = snapshot.microphoneStatus
        systemAudio.stringValue = snapshot.systemAudioStatus
        video.stringValue = snapshot.videoStatus
        detail.stringValue = snapshot.detail ?? (snapshot.phase == .review
            ? "Review this recording before transcription, notes, or uploading." : "")
        detail.isHidden = detail.stringValue.isEmpty
        if let seconds = snapshot.countdownSeconds {
            let remaining = max(0, seconds)
            let introduction = paused ? "Recording is paused." : "Still recording?"
            warning.stringValue = "\(introduction) Quill will stop and save in \(remaining / 60):\(String(format: "%02d", remaining % 60)) unless you continue."
            if isCompact { heading.stringValue = "STOP IN \(remaining / 60):\(String(format: "%02d", remaining % 60))" }
        } else { warning.stringValue = "" }
        warning.isHidden = snapshot.countdownSeconds == nil
        stopButton.title = snapshot.phase == .stopping ? "Saving recording…" : "Stop & save"
        stopButton.isHidden = snapshot.phase == .idle || snapshot.phase == .review
        captureActions.isHidden = stopButton.isHidden
        stopButton.isEnabled = snapshot.stopEnabled && snapshot.sessionID != nil
        compactStopButton.isEnabled = stopButton.isEnabled
        continueButton.isHidden = snapshot.countdownSeconds == nil || snapshot.warningRevision == nil
        continueButton.isEnabled = snapshot.stopEnabled && snapshot.sessionID != nil
        reviewActions.isHidden = snapshot.phase != .review
        keepLocalButton.isEnabled = snapshot.sessionID != nil && snapshot.keepLocalEnabled
        processButton.isEnabled = snapshot.sessionID != nil && snapshot.processEnabled
        openFolderButton.isEnabled = snapshot.sessionID != nil && onOpenFolder != nil
        dismissButton.isHidden = snapshot.needsPersistentControl
        let sessionID = snapshot.sessionID
        let revision = snapshot.warningRevision
        let actionRevision = snapshot.actionRevision
        stopButton.onClick = { [weak self] in if let sessionID { self?.onStop?(sessionID, actionRevision) } }
        compactStopButton.onClick = stopButton.onClick
        let pauseEnabled = snapshot.canChangePause
        pauseButton.onClick = { [weak self] in
            if pauseEnabled, let sessionID, let actionRevision {
                self?.onPause?(sessionID, actionRevision, paused)
            }
        }
        compactPauseButton.onClick = pauseButton.onClick
        continueButton.onClick = { [weak self] in if let sessionID, let revision { self?.onContinue?(sessionID, revision) } }
        keepLocalButton.onClick = { [weak self] in if let sessionID { self?.onKeepLocal?(sessionID) } }
        processButton.onClick = { [weak self] in if let sessionID { self?.onProcess?(sessionID) } }
        openFolderButton.onClick = { [weak self] in if let sessionID { self?.onOpenFolder?(sessionID) } }
        resizeToContent(compact: isCompact)
    }

    private func renderElapsed() {
        // Do not reset tooltips, symbols, button actions, or the panel frame
        // while the user is reading or interacting with an unchanged control.
        if elapsed.stringValue != snapshot.elapsed { elapsed.stringValue = snapshot.elapsed }
        if compactElapsed.stringValue != snapshot.elapsed { compactElapsed.stringValue = snapshot.elapsed }
    }

    private func updateDockBadge() {
        switch snapshot.phase {
        case .recording: NSApp.dockTile.badgeLabel = snapshot.needsAttention ? "REC !" : "REC"
        case .paused: NSApp.dockTile.badgeLabel = snapshot.needsAttention ? "Paused !" : "Paused"
        case .pausing: NSApp.dockTile.badgeLabel = "Pausing"
        case .resuming: NSApp.dockTile.badgeLabel = "Resuming"
        case .attention: NSApp.dockTile.badgeLabel = snapshot.stopEnabled ? "REC !" : "!"
        case .starting: NSApp.dockTile.badgeLabel = "…"
        case .stopping: NSApp.dockTile.badgeLabel = "Saving"
        case .idle, .review:
            NSApp.dockTile.badgeLabel = snapshot.pendingReviews > 0 ? String(snapshot.pendingReviews) : nil
        }
    }

    private func resizeToContent(compact: Bool) {
        let changingPause = snapshot.phase == .pausing || snapshot.phase == .resuming
        let width: CGFloat = compact ? (snapshot.countdownSeconds == nil && !changingPause ? 360 : 420) : 366
        contentWidth?.constant = width - 32
        detail.preferredMaxLayoutWidth = width - 32
        warning.preferredMaxLayoutWidth = width - 32
        panel.contentView?.layoutSubtreeIfNeeded()
        let height = max(compact ? 52 : 120, root.fittingSize.height + 28)
        guard abs(panel.frame.width - width) > 0.5 || abs(panel.frame.height - height) > 0.5 else { return }
        adjustingFrame = true
        let old = panel.frame
        panel.setFrame(NSRect(x: old.minX, y: old.maxY - height, width: width, height: height), display: true)
        adjustingFrame = false
        constrainToVisibleScreen()
    }

    private func restorePosition() {
        if let saved = defaults.dictionary(forKey: positionKey),
           let x = saved["x"] as? Double, let top = saved["top"] as? Double,
           x.isFinite, top.isFinite {
            panel.setFrameOrigin(NSPoint(x: x, y: top - panel.frame.height))
        } else if let visible = NSScreen.main?.visibleFrame ?? NSScreen.screens.first?.visibleFrame {
            panel.setFrameOrigin(NSPoint(x: visible.maxX - panel.frame.width - 20,
                y: visible.maxY - panel.frame.height - 20))
        }
        constrainToVisibleScreen()
    }

    private func savePosition() {
        defaults.set(["x": panel.frame.minX, "top": panel.frame.maxY], forKey: positionKey)
    }

    private func constrainToVisibleScreen() {
        guard !adjustingFrame, !NSScreen.screens.isEmpty else { return }
        let frame = panel.frame
        let screen = NSScreen.screens.max { left, right in
            let a = left.visibleFrame.intersection(frame)
            let b = right.visibleFrame.intersection(frame)
            return (a.isNull ? 0 : a.width * a.height) < (b.isNull ? 0 : b.width * b.height)
        } ?? NSScreen.screens[0]
        let bounds = screen.visibleFrame.insetBy(dx: 8, dy: 8)
        let origin = NSPoint(x: max(bounds.minX, min(frame.minX, bounds.maxX - frame.width)),
            y: max(bounds.minY, min(frame.minY, bounds.maxY - frame.height)))
        guard origin != frame.origin else { return }
        adjustingFrame = true
        panel.setFrameOrigin(origin)
        adjustingFrame = false
    }

    private func add(_ view: NSView, to stack: NSStackView) {
        stack.addArrangedSubview(view)
        view.translatesAutoresizingMaskIntoConstraints = false
        view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }

    private func spacer() -> NSView {
        let view = NSView()
        view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return view
    }

    private func trackRow(_ title: String, value: NSTextField, symbol: String) -> NSStackView {
        let icon = NSImageView(image: NSImage(systemSymbolName: symbol, accessibilityDescription: nil) ?? NSImage())
        icon.contentTintColor = .secondaryLabelColor
        icon.widthAnchor.constraint(equalToConstant: 16).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 16).isActive = true
        icon.setAccessibilityElement(false)
        let label = NSTextField(labelWithString: title)
        label.font = .systemFont(ofSize: 12)
        value.font = .systemFont(ofSize: 12, weight: .medium)
        value.alignment = .right
        value.setAccessibilityLabel(title)
        value.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        value.setContentCompressionResistancePriority(.required, for: .vertical)
        value.widthAnchor.constraint(lessThanOrEqualToConstant: 185).isActive = true
        let row = NSStackView(views: [icon, label, spacer(), value])
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = 7
        return row
    }
}

private final class RecordingControlPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

/// AppKit may run a nested event loop while a mouse button is held. Freeze the
/// action from mouse-down through dispatch, even if a newer snapshot arrives.
final class RecordingActionButton: NSButton {
    var onClick: (() -> Void)?
    private var trackingAction: (() -> Void)?

    init(title: String) {
        super.init(frame: .zero)
        self.title = title
        bezelStyle = .rounded
        target = self
        action = #selector(invoke)
    }
    required init?(coder: NSCoder) { nil }
    override var needsPanelToBecomeKey: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) {
        trackingAction = onClick
        defer { trackingAction = nil }
        super.mouseDown(with: event)
    }
    @objc private func invoke() { (trackingAction ?? onClick)?() }
}
