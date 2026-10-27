import AppKit
import Darwin
import Foundation

@main
enum WarningHistoryChecks {
    @MainActor
    static func main() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("quill-warning-history-\(UUID())")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try transitionsAndIdleTicks(root.appendingPathComponent("transitions/history.json"))
        try separateSourcesAndRestart(root.appendingPathComponent("restart/history.json"))
        try boundedHistoryAndPermissions(root.appendingPathComponent("bounded/history.json"))
        try invalidAndUnavailableStores(root.appendingPathComponent("invalid"))
        try boundedInputs(root.appendingPathComponent("inputs/history.json"))
        _ = NSApplication.shared
        let controller = RecordingWarningHistoryWindowController()
        precondition(controller.window?.isVisible == false, "History must not open at construction")
        controller.update(entries: [], storageIssue: nil)
        precondition(controller.window?.isVisible == false, "Background updates must not open history")
        precondition(controller.window?.styleMask.contains(.resizable) == true)
        controller.window?.close()
        print("Warning history: bounded private persistence, deduplicated transitions, honest restart/context handling, and read-only window passed.")
    }

    @MainActor
    private static func transitionsAndIdleTicks(_ path: URL) throws {
        let history = RecordingWarningHistory(path: path)
        let warning = RecordingWarningHistory.Warning(id: "meeting-state", title: "Meeting detection unavailable",
            detail: "Recording continues. Use Stop & save when the call ends.")
        let start = Date(timeIntervalSince1970: 1_700_000_000)
        precondition(history.observe(source: .recording, contextID: "session-one", warnings: [warning], at: start))
        precondition(history.entries.count == 1 && history.entries[0].event == .appeared)
        let bytes = try Data(contentsOf: path)
        let before = try FileManager.default.attributesOfItem(atPath: path.path)
        for tick in 1...150 {
            precondition(!history.observe(source: .recording, contextID: "session-one", warnings: [warning],
                at: start.addingTimeInterval(Double(tick))), "Identical timer observations must be ignored")
        }
        let after = try FileManager.default.attributesOfItem(atPath: path.path)
        let unchanged = try Data(contentsOf: path)
        precondition(history.entries.count == 1 && unchanged == bytes)
        precondition((before[.systemFileNumber] as? NSNumber) == (after[.systemFileNumber] as? NSNumber),
            "Timer-only updates replaced the history file")
        precondition((before[.modificationDate] as? Date) == (after[.modificationDate] as? Date))
        let changed = RecordingWarningHistory.Warning(id: warning.id, title: warning.title, detail: "Check meeting detection.")
        precondition(history.observe(source: .recording, contextID: "session-one", warnings: [changed], at: start.addingTimeInterval(151)))
        precondition(history.entries.last?.event == .changed && history.entries.last?.detail == changed.detail)
        precondition(history.observe(source: .recording, contextID: "session-one", warnings: [], at: start.addingTimeInterval(152)))
        precondition(history.entries.last?.event == .noLongerShown)
        precondition(!history.observe(source: .recording, contextID: nil, warnings: [], at: start.addingTimeInterval(153)))
        precondition(history.entries.count == 3, "An already absent warning cannot end twice")
        history.observe(source: .recording, contextID: "session-two", warnings: [warning], at: start.addingTimeInterval(154))
        history.observe(source: .recording, contextID: "session-three", warnings: [warning], at: start.addingTimeInterval(155))
        precondition(history.entries.suffix(2).map(\.event) == [.observationEnded, .appeared])
        precondition(history.entries.suffix(2).map(\.contextID) == ["session-two", "session-three"])
        history.observe(source: .recording, contextID: nil, warnings: [], at: start.addingTimeInterval(156))
        precondition(history.entries.last?.event == .observationEnded,
            "Closing a recording must not claim capture recovered")
        precondition(history.entries.allSatisfy { !$0.event.displayTitle.lowercased().contains("recover") })
    }

    @MainActor
    private static func separateSourcesAndRestart(_ path: URL) throws {
        let first = RecordingWarningHistory(path: path)
        let warning = RecordingWarningHistory.Warning(id: "same-id", title: "Warning", detail: "Synthetic test detail")
        first.observe(source: .recording, contextID: "recording", warnings: [warning])
        first.observe(source: .meetingReminder, contextID: "call", warnings: [warning])
        first.observe(source: .processing, contextID: "worker", warnings: [warning])
        first.observe(source: .recording, contextID: "recording", warnings: [])
        precondition(first.entries.count == 4)
        precondition(first.entries.last?.source == .recording && first.entries.last?.event == .noLongerShown)
        let saved = try Data(contentsOf: path)
        let restored = RecordingWarningHistory(path: path)
        precondition(restored.entries == first.entries && restored.storageIssue == nil)
        for source in RecordingWarningHistory.Source.allCases {
            precondition(!restored.observe(source: source, contextID: nil, warnings: []),
                "Loading old history must not synthesize a cleared or recovered event")
        }
        let afterReload = try Data(contentsOf: path)
        precondition(afterReload == saved)
        restored.observe(source: .meetingReminder, contextID: "call", warnings: [warning])
        precondition(restored.entries.last?.event == .appeared,
            "Only a new observation can establish a warning after relaunch")
    }

    @MainActor
    private static func boundedHistoryAndPermissions(_ path: URL) throws {
        let history = RecordingWarningHistory(path: path, limit: 7)
        for step in 0..<20 {
            history.observe(source: .processing, contextID: "queue", warnings: [
                .init(id: "worker", title: "Processing needs attention", detail: "Synthetic change \(step)")
            ])
        }
        precondition(history.entries.count == 7 && history.entries.first?.detail == "Synthetic change 13")
        let restored = RecordingWarningHistory(path: path, limit: 3)
        precondition(restored.entries.count == 3 && restored.entries.first?.detail == "Synthetic change 17")
        let file = try FileManager.default.attributesOfItem(atPath: path.path)
        let directory = try FileManager.default.attributesOfItem(atPath: path.deletingLastPathComponent().path)
        precondition((file[.posixPermissions] as? NSNumber)?.intValue == 0o600)
        precondition((directory[.posixPermissions] as? NSNumber)?.intValue == 0o700)
        let siblings = try FileManager.default.contentsOfDirectory(atPath: path.deletingLastPathComponent().path)
        precondition(siblings == ["history.json"], "Atomic persistence left temporary files")
        let maximum = RecordingWarningHistory(path: path.deletingLastPathComponent().appendingPathComponent("max.json"), limit: 10_000)
        for step in 0..<210 {
            maximum.observe(source: .recording, contextID: "session", warnings: [
                .init(id: "warning", title: "Synthetic warning", detail: String(step))
            ])
        }
        precondition(maximum.entries.count == 200)
    }

    @MainActor
    private static func invalidAndUnavailableStores(_ root: URL) throws {
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let warning = RecordingWarningHistory.Warning(id: "test", title: "Test warning", detail: "Local-only synthetic data")
        let malformed = root.appendingPathComponent("malformed.json")
        let original = Data("{invalid".utf8)
        try original.write(to: malformed)
        let corrupt = RecordingWarningHistory(path: malformed)
        precondition(corrupt.entries.isEmpty && corrupt.storageIssue != nil)
        corrupt.observe(source: .recording, contextID: "call", warnings: [warning])
        precondition(corrupt.entries.count == 1 && corrupt.storageIssue != nil)
        let afterCorruptWrite = try Data(contentsOf: malformed)
        precondition(afterCorruptWrite == original, "Unreadable history must be preserved")

        let blockedDirectory = root.appendingPathComponent("blocked")
        let unavailable = RecordingWarningHistory(path: blockedDirectory.appendingPathComponent("history.json"))
        try Data("Not a directory".utf8).write(to: blockedDirectory)
        precondition(unavailable.observe(source: .meetingReminder, contextID: "call", warnings: [warning]))
        precondition(unavailable.entries.count == 1 && unavailable.storageIssue != nil,
            "Storage failure must retain the warning in memory")
        precondition(!unavailable.observe(source: .meetingReminder, contextID: "call", warnings: [warning]),
            "An unwritable history must not retry disk on every timer tick")

        let oversized = root.appendingPathComponent("large.json")
        precondition(FileManager.default.createFile(atPath: oversized.path, contents: nil))
        let large = try FileHandle(forWritingTo: oversized)
        try large.truncate(atOffset: 2_097_153)
        try large.close()
        let bounded = RecordingWarningHistory(path: oversized)
        precondition(bounded.entries.isEmpty && bounded.storageIssue != nil)

        let symlink = root.appendingPathComponent("linked.json")
        try FileManager.default.createSymbolicLink(at: symlink, withDestinationURL: malformed)
        let linked = RecordingWarningHistory(path: symlink)
        linked.observe(source: .processing, contextID: "worker", warnings: [warning])
        let afterSymlinkWrite = try Data(contentsOf: malformed)
        precondition(linked.storageIssue != nil && afterSymlinkWrite == original)

        let fifo = root.appendingPathComponent("pipe")
        precondition(mkfifo(fifo.path, 0o600) == 0)
        let pipe = RecordingWarningHistory(path: fifo)
        precondition(pipe.storageIssue != nil && pipe.entries.isEmpty, "Non-regular input must be rejected without blocking")
    }

    @MainActor
    private static func boundedInputs(_ path: URL) throws {
        let history = RecordingWarningHistory(path: path)
        let warning = RecordingWarningHistory.Warning(id: "bounded", title: String(repeating: "A", count: 500),
            detail: String(repeating: "🥯", count: 1_000) + "\u{0}")
        history.observe(source: .recording, contextID: String(repeating: "x", count: 200), warnings: [warning, warning])
        precondition(history.entries.count == 1)
        let entry = history.entries[0]
        precondition(entry.title.utf8.count <= 200 && entry.detail.utf8.count <= 2_048 && entry.contextID.utf8.count <= 160)
        precondition(!entry.detail.contains("\u{0}") && !entry.detail.contains("�"))
        precondition(!history.observe(source: .recording, contextID: "call", warnings: [], at: Date(timeIntervalSince1970: .nan)))
        precondition(history.entries.count == 1)
        precondition(RecordingWarningHistory(path: path).entries == history.entries)
    }
}
