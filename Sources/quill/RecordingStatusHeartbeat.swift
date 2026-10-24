import Foundation

/// Browser health is confirmation of current app state, never permission to
/// start capture. The native host rejects stale files after a crash or quit.
@MainActor
final class RecordingStatusHeartbeat {
    private struct Snapshot: Encodable {
        let version = 1
        let timestamp: TimeInterval
        let phase: String
        let deliberatelySkipped: [String]
    }
    private let path: URL
    private var lastWrite: TimeInterval = 0
    private var lastPhase = ""
    private var lastSkips: Set<String> = []

    init(path: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".local/state/quill/recording-status.json")) {
        self.path = path
    }

    func publish(phase: String, deliberatelySkipped: Set<String>, now: TimeInterval) {
        guard now.isFinite, ["idle", "starting", "recording", "paused", "busy"].contains(phase) else { return }
        guard now < lastWrite || now - lastWrite >= 1 || phase != lastPhase || deliberatelySkipped != lastSkips else { return }
        let directory = path.deletingLastPathComponent()
        let temporary = directory.appendingPathComponent(".recording-status-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: temporary) }
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700])
            let data = try JSONEncoder().encode(Snapshot(timestamp: now, phase: phase,
                deliberatelySkipped: Array(deliberatelySkipped.filter {
                    $0.range(of: "^[A-Za-z0-9-]{1,128}$", options: .regularExpression) != nil
                }.sorted().prefix(64))))
            guard FileManager.default.createFile(atPath: temporary.path, contents: data,
                attributes: [.posixPermissions: 0o600]),
                rename(temporary.path, path.path) == 0 else { return }
            lastWrite = now
            lastPhase = phase
            lastSkips = deliberatelySkipped
        } catch {
            // No old heartbeat is refreshed on failure. Browser confirmation
            // expires and its independent warning becomes visible.
        }
    }

    func invalidate() {
        try? FileManager.default.removeItem(at: path)
        lastWrite = 0
        lastPhase = ""
    }
}
