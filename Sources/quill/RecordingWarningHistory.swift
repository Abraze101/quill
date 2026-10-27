import Darwin
import Foundation

/// A local record of warnings the interface displayed. It has no capture,
/// processing, notification, or recovery actions.
@MainActor
final class RecordingWarningHistory {
    enum Source: String, Codable, CaseIterable {
        case recording, meetingReminder, processing

        var displayTitle: String {
            switch self {
            case .recording: "Recording"
            case .meetingReminder: "Meeting reminder"
            case .processing: "Processing"
            }
        }
    }

    struct Warning: Equatable {
        let id: String
        let title: String
        let detail: String
    }

    enum Event: String, Codable {
        case appeared, changed, noLongerShown, observationEnded

        var displayTitle: String {
            switch self {
            case .appeared: "Appeared"
            case .changed: "Updated"
            case .noLongerShown: "No longer shown"
            case .observationEnded: "Observation ended"
            }
        }
    }

    struct Entry: Codable, Equatable, Identifiable {
        let id: UUID
        let timestamp: Date
        let source: Source
        let contextID: String
        let warningID: String
        let title: String
        let detail: String
        let event: Event
    }

    private struct Saved: Codable {
        let version: Int
        let entries: [Entry]
    }

    private struct Observation {
        let contextID: String
        let warnings: [String: Warning]
    }

    static var defaultPath: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/state/quill/warning-history/history.json")
    }

    /// Entries remain in observation order, oldest first. They are history,
    /// including after relaunch, and must never be treated as live warnings.
    private(set) var entries: [Entry] = []
    private(set) var storageIssue: String?
    private let path: URL
    private let limit: Int
    private var observations: [Source: Observation] = [:]
    private var canPersist = true
    private static let maximumBytes = 2_097_152
    private static let maximumEntries = 200

    init(path: URL = RecordingWarningHistory.defaultPath, limit: Int = 200) {
        self.path = path
        self.limit = min(Self.maximumEntries, max(1, limit))
        load()
    }

    /// A nil or different context ends observation; it never establishes that
    /// capture recovered. Timer-only updates do not add entries or touch disk.
    @discardableResult
    func observe(source: Source, contextID: String?, warnings: [Warning], at: Date = Date()) -> Bool {
        guard Self.validDate(at) else { return false }
        let context = contextID.map { Self.bounded($0, bytes: 160) }.flatMap { $0.isEmpty ? nil : $0 }
        let previous = observations[source]
        var added: [Entry] = []
        func append(_ warning: Warning, context: String, event: Event) {
            added.append(Entry(id: UUID(), timestamp: at, source: source, contextID: context,
                warningID: warning.id, title: warning.title, detail: warning.detail, event: event))
        }

        if let previous, previous.contextID != context {
            for id in previous.warnings.keys.sorted() {
                if let warning = previous.warnings[id] {
                    append(warning, context: previous.contextID, event: .observationEnded)
                }
            }
        }
        if let context {
            var current: [String: Warning] = [:]
            for warning in warnings.prefix(20) {
                let id = Self.bounded(warning.id, bytes: 120)
                let title = Self.bounded(warning.title, bytes: 200)
                guard !id.isEmpty, !title.isEmpty, current[id] == nil else { continue }
                current[id] = Warning(id: id, title: title,
                    detail: Self.bounded(warning.detail, bytes: 2_048, multiline: true))
            }
            let prior = previous?.contextID == context ? previous?.warnings ?? [:] : [:]
            for id in prior.keys.sorted() where current[id] == nil {
                if let warning = prior[id] { append(warning, context: context, event: .noLongerShown) }
            }
            for id in current.keys.sorted() {
                guard let warning = current[id], prior[id] != warning else { continue }
                append(warning, context: context, event: prior[id] == nil ? .appeared : .changed)
            }
            observations[source] = Observation(contextID: context, warnings: current)
        } else {
            observations[source] = nil
        }
        guard !added.isEmpty else { return false }
        entries.append(contentsOf: added)
        if entries.count > limit { entries.removeFirst(entries.count - limit) }
        save()
        return true
    }

    private static func validDate(_ date: Date) -> Bool {
        let seconds = date.timeIntervalSince1970
        return seconds.isFinite && (-62_135_596_800...253_402_300_799).contains(seconds)
    }

    private static func bounded(_ text: String, bytes: Int, multiline: Bool = false) -> String {
        var result = ""
        var count = 0
        for scalar in text.unicodeScalars {
            if CharacterSet.controlCharacters.contains(scalar), !(multiline && scalar == "\n") { continue }
            let size = scalar.utf8.count
            guard count + size <= bytes else { break }
            result.unicodeScalars.append(scalar)
            count += size
        }
        return result.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func valid(_ entry: Entry) -> Bool {
        validDate(entry.timestamp) && !entry.contextID.isEmpty && !entry.warningID.isEmpty && !entry.title.isEmpty
            && entry.contextID == bounded(entry.contextID, bytes: 160)
            && entry.warningID == bounded(entry.warningID, bytes: 120)
            && entry.title == bounded(entry.title, bytes: 200)
            && entry.detail == bounded(entry.detail, bytes: 2_048, multiline: true)
    }

    private func load() {
        let descriptor = open(path.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
        guard descriptor >= 0 else {
            if errno != ENOENT { cannotReadHistory() }
            return
        }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        defer { try? handle.close() }
        var information = stat()
        guard fstat(descriptor, &information) == 0,
              information.st_mode & S_IFMT == S_IFREG, information.st_uid == getuid(),
              information.st_size >= 0, information.st_size <= Self.maximumBytes else {
            cannotReadHistory()
            return
        }
        do {
            guard let data = try handle.read(upToCount: Self.maximumBytes + 1), data.count <= Self.maximumBytes else {
                cannotReadHistory()
                return
            }
            let decoder = JSONDecoder()
            let saved = try decoder.decode(Saved.self, from: data)
            guard saved.version == 1, saved.entries.count <= Self.maximumEntries,
                  saved.entries.allSatisfy(Self.valid), Set(saved.entries.map(\.id)).count == saved.entries.count else {
                cannotReadHistory()
                return
            }
            entries = Array(saved.entries.suffix(limit))
            // Do not rebuild observations from prior-run entries. The app did
            // not observe what happened after its previous run ended.
        } catch {
            cannotReadHistory()
        }
    }

    private func cannotReadHistory() {
        canPersist = false
        storageIssue = "Saved warning history could not be read. New entries will be kept here until Quill closes."
    }

    private func save() {
        guard canPersist else { return }
        do {
            let directory = path.deletingLastPathComponent()
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700])
            var directoryInfo = stat()
            guard lstat(directory.path, &directoryInfo) == 0,
                  directoryInfo.st_mode & S_IFMT == S_IFDIR, directoryInfo.st_uid == getuid(),
                  chmod(directory.path, 0o700) == 0 else { throw StorageError.unavailable }
            var destinationInfo = stat()
            if lstat(path.path, &destinationInfo) == 0 {
                guard destinationInfo.st_mode & S_IFMT == S_IFREG, destinationInfo.st_uid == getuid() else {
                    throw StorageError.unavailable
                }
            } else if errno != ENOENT { throw StorageError.unavailable }

            let encoder = JSONEncoder()
            let data = try encoder.encode(Saved(version: 1, entries: entries))
            guard data.count <= Self.maximumBytes else { throw StorageError.unavailable }
            let temporary = directory.appendingPathComponent(".history-\(UUID().uuidString).tmp")
            let descriptor = open(temporary.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
            guard descriptor >= 0 else { throw StorageError.unavailable }
            let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
            defer {
                try? handle.close()
                _ = unlink(temporary.path)
            }
            guard fchmod(descriptor, 0o600) == 0 else { throw StorageError.unavailable }
            try handle.write(contentsOf: data)
            try handle.synchronize()
            guard rename(temporary.path, path.path) == 0 else { throw StorageError.unavailable }
            storageIssue = nil
        } catch {
            storageIssue = "Warning history could not be saved. New entries are available here until Quill closes."
        }
    }

    private enum StorageError: Error { case unavailable }
}
