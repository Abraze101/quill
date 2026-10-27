import AppKit

/// Reading warning history cannot start, stop, resume, or process a recording.
@MainActor
final class RecordingWarningHistoryWindowController: NSWindowController {
    private let textView = NSTextView()
    private let storageLabel = NSTextField(wrappingLabelWithString: "")
    private var displayedEntries: [RecordingWarningHistory.Entry] = []
    private var displayedIssue: String?
    private var hasRendered = false
    private let dateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateStyle = .short
        formatter.timeStyle = .medium
        return formatter
    }()

    init() {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 680, height: 520),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Recent warnings"
        window.setAccessibilityLabel("Quill recent warnings")
        window.minSize = NSSize(width: 460, height: 320)
        window.isReleasedWhenClosed = false
        super.init(window: window)
        buildContent()
        window.center()
    }

    required init?(coder: NSCoder) { nil }

    func show(entries: [RecordingWarningHistory.Entry], storageIssue: String?) {
        render(entries: entries, storageIssue: storageIssue)
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Background history changes never open a window or take keyboard focus.
    func update(entries: [RecordingWarningHistory.Entry], storageIssue: String?) {
        guard window?.isVisible == true else { return }
        render(entries: entries, storageIssue: storageIssue)
    }

    private func buildContent() {
        guard let content = window?.contentView else { return }
        let explanation = NSTextField(wrappingLabelWithString:
            "Warnings shown by Quill on this Mac, newest first. “No longer shown” means the warning left the interface; it does not confirm that an issue was fixed.")
        explanation.font = .systemFont(ofSize: 12)
        explanation.textColor = .secondaryLabelColor
        storageLabel.font = .systemFont(ofSize: 12)
        storageLabel.textColor = .systemOrange
        storageLabel.isHidden = true
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .bezelBorder
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = true
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.minSize = NSSize(width: 0, height: 0)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.textContainerInset = NSSize(width: 14, height: 14)
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.containerSize = NSSize(width: 620, height: CGFloat.greatestFiniteMagnitude)
        textView.setAccessibilityLabel("Warning history")
        textView.setAccessibilityIdentifier("quill-warning-history")
        scroll.documentView = textView
        let privacy = NSTextField(wrappingLabelWithString:
            "Saved locally. Browser-only reminders and warnings from earlier versions are not included.")
        privacy.font = .systemFont(ofSize: 11)
        privacy.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [explanation, storageLabel, scroll, privacy])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        for view in stack.arrangedSubviews {
            view.translatesAutoresizingMaskIntoConstraints = false
            view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 18),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -18),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 160)
        ])
        explanation.setContentHuggingPriority(.required, for: .vertical)
        storageLabel.setContentHuggingPriority(.required, for: .vertical)
        privacy.setContentHuggingPriority(.required, for: .vertical)
    }

    private func render(entries: [RecordingWarningHistory.Entry], storageIssue: String?) {
        guard !hasRendered || entries != displayedEntries || storageIssue != displayedIssue else { return }
        hasRendered = true
        displayedEntries = entries
        displayedIssue = storageIssue
        storageLabel.stringValue = storageIssue ?? ""
        storageLabel.isHidden = storageIssue == nil
        let text = NSMutableAttributedString(string: "")
        func append(_ value: String, font: NSFont, color: NSColor = .labelColor, spacing: CGFloat = 5) {
            let paragraph = NSMutableParagraphStyle()
            paragraph.paragraphSpacing = spacing
            text.append(NSAttributedString(string: value + "\n", attributes:
                [.font: font, .foregroundColor: color, .paragraphStyle: paragraph]))
        }
        if entries.isEmpty {
            append("No warnings recorded yet", font: .systemFont(ofSize: 16, weight: .semibold))
            append("Quill will save new warnings here. Earlier flashes cannot be reconstructed.",
                font: .systemFont(ofSize: 13), color: .secondaryLabelColor)
        } else {
            for entry in entries.reversed() {
                append("\(dateFormatter.string(from: entry.timestamp)) · \(entry.source.displayTitle) · \(entry.event.displayTitle)",
                    font: .systemFont(ofSize: 11, weight: .medium), color: .secondaryLabelColor)
                append(entry.title, font: .systemFont(ofSize: 14, weight: .semibold))
                if !entry.detail.isEmpty { append(entry.detail, font: .systemFont(ofSize: 13)) }
                append("", font: .systemFont(ofSize: 5), spacing: 8)
            }
        }
        let wasAtTop = (textView.enclosingScrollView?.contentView.bounds.minY ?? 0) <= 1
        textView.textStorage?.setAttributedString(text)
        if wasAtTop { textView.scrollRangeToVisible(NSRange(location: 0, length: 0)) }
    }
}
