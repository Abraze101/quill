import AppKit

/// A reminder never owns capture. Actions retain the displayed meeting ID;
/// AppController rechecks that identity and capture state before acting.
@MainActor
final class MeetingReminderController {
    var onStart: ((String) -> Void)?
    var onSkip: ((String) -> Void)?
    private let panel: NSPanel
    private let detail = NSTextField(wrappingLabelWithString: "")
    private let start = RecordingActionButton(title: "Start recording")
    private let skip = RecordingActionButton(title: "Skip this meeting")
    private let displayUpdates: Bool
    private struct Presentation: Equatable {
        let meetingID: String
        let canStart: Bool
        let canSkip: Bool
        let detail: String
    }
    private var presentation: Presentation?

    init(displayUpdates: Bool = true) {
        self.displayUpdates = displayUpdates
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 380, height: 154),
            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.title = "Quill meeting reminder"
        panel.setAccessibilityLabel(panel.title)
        panel.isReleasedWhenClosed = false
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.canHide = false
        panel.isMovableByWindowBackground = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .canJoinAllApplications, .stationary]
        panel.sharingType = .none
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.animationBehavior = .none
        let material = NSVisualEffectView()
        material.material = .hudWindow
        material.state = .active
        material.wantsLayer = true
        material.layer?.cornerRadius = 14
        material.layer?.masksToBounds = true
        panel.contentView = material
        let heading = NSTextField(labelWithString: "Meeting is not recording")
        heading.font = .systemFont(ofSize: 15, weight: .semibold)
        heading.textColor = .systemOrange
        detail.font = .systemFont(ofSize: 13)
        detail.preferredMaxLayoutWidth = 344
        detail.setContentCompressionResistancePriority(.required, for: .vertical)
        start.setAccessibilityIdentifier("quill-reminder-start")
        skip.setAccessibilityIdentifier("quill-reminder-skip")
        let actions = NSStackView(views: [start, skip])
        actions.orientation = .horizontal
        actions.spacing = 8
        let stack = NSStackView(views: [heading, detail, actions])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        material.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: material.leadingAnchor, constant: 18),
            stack.trailingAnchor.constraint(equalTo: material.trailingAnchor, constant: -18),
            stack.topAnchor.constraint(equalTo: material.topAnchor, constant: 16),
            stack.bottomAnchor.constraint(equalTo: material.bottomAnchor, constant: -16),
            detail.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
    }

    func update(meetingID: String?, canStart: Bool = false, canSkip: Bool = true, detail text: String = "") {
        guard let meetingID else {
            if presentation != nil {
                presentation = nil
                start.onClick = nil
                skip.onClick = nil
            }
            if panel.isVisible { panel.orderOut(nil) }
            return
        }
        let next = Presentation(meetingID: meetingID, canStart: canStart, canSkip: canSkip, detail: text)
        let firstPresentation = presentation?.meetingID != meetingID
        if presentation != next {
            presentation = next
            detail.stringValue = text
            start.title = canStart ? "Start recording" : "Check recording…"
            skip.isEnabled = canSkip
            start.onClick = { [weak self] in self?.onStart?(meetingID) }
            skip.onClick = { [weak self] in if canSkip { self?.onSkip?(meetingID) } }
            if let stack = panel.contentView?.subviews.first as? NSStackView {
                panel.contentView?.layoutSubtreeIfNeeded()
                let height = max(154, stack.fittingSize.height + 32)
                let frame = panel.frame
                if abs(frame.height - height) > 0.5 {
                    panel.setFrame(NSRect(x: frame.minX, y: frame.maxY - height,
                        width: frame.width, height: height), display: displayUpdates)
                }
            }
        }
        if firstPresentation, let screen = NSScreen.main ?? NSScreen.screens.first {
            let visible = screen.visibleFrame
            // Separate from the recording controls at the top-right edge.
            panel.setFrameOrigin(NSPoint(x: visible.maxX - panel.frame.width - 20,
                y: visible.minY + 30))
        }
        if displayUpdates && (!panel.isVisible || firstPresentation) { panel.orderFrontRegardless() }
    }
}
