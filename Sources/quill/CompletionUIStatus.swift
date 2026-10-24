import Foundation

/// Display-only bridge: recording never waits for the completion worker.
struct CompletionUIStatus {
    var pendingReviews = 0
    var notes = "Not configured. Processing is off."
    var drive = "Not connected. Uploads are off."
    var preferences: WorkflowPreferences?
    var processingTitle: String?
    var processingDetail = ""
    var needsAttention = false
    var attentionDetail = ""
    var alertRevision: String?
    var retryableJobs = 0

    init(settingsURL: URL, statusURL: URL) {
        if let status = Self.readObject(statusURL) {
            pendingReviews = max(0, status["pending_reviews"] as? Int ?? 0)
            if let processing = status["processing"] as? [String: Any],
               let state = processing["state"] as? String,
               ["idle", "queued", "running", "retrying", "waiting_for_company", "needs_attention"].contains(state),
               let title = Self.text(processing["title"], limit: 200) {
                processingTitle = title
                processingDetail = Self.text(processing["detail"], limit: 1000) ?? ""
                attentionDetail = Self.text(processing["attention_detail"], limit: 1000) ?? ""
                retryableJobs = max(0, processing["retryable_jobs"] as? Int ?? 0)
                if state == "needs_attention" || !attentionDetail.isEmpty {
                    needsAttention = true
                    alertRevision = Self.text(processing["alert_revision"], limit: 200)
                }
                if needsAttention && attentionDetail.isEmpty { attentionDetail = processingDetail }
            }
        }
        preferences = try? WorkflowPreferences(url: settingsURL)
        guard let preferences else { return }
        notes = preferences.notesDescription
        drive = preferences.driveDescription
    }

    private static func text(_ value: Any?, limit: Int) -> String? {
        guard let text = value as? String, !text.isEmpty, text.utf8.count <= limit,
              !text.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) && $0 != "\n" }) else { return nil }
        return text
    }

    private static func readObject(_ url: URL) -> [String: Any]? {
        guard let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey]),
              values.isRegularFile == true, values.isSymbolicLink != true else { return nil }
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        guard let data = try? handle.read(upToCount: 65_537), data.count <= 65_536 else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }
}

/// A second, bounded guard for the supervisor itself. Worker retries and job
/// approvals remain in Python; a normal process exit never requests a restart.
struct CompletionWorkerRecovery {
    private(set) var failures = 0
    private(set) var retryAt: TimeInterval?
    private var startedAt: TimeInterval?
    var exhausted: Bool { failures > 3 }

    mutating func started(now: TimeInterval) {
        startedAt = now
        retryAt = nil
    }

    mutating func exited(status: Int32, now: TimeInterval) {
        startedAt = nil
        guard status != 0 else { retryAt = nil; return }
        failures += 1
        retryAt = failures <= 3 ? now + 30 * pow(2, Double(failures - 1)) : nil
    }

    mutating func healthy(now: TimeInterval) {
        if let startedAt, now - startedAt >= 300 { failures = 0 }
    }

    func shouldRestart(now: TimeInterval) -> Bool { retryAt.map { now >= $0 } ?? false }
}
