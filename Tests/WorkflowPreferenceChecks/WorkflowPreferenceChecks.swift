import Foundation

@main
enum WorkflowPreferenceChecks {
    static func main() throws {
        let fm = FileManager.default
        let temporary = fm.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try fm.createDirectory(at: temporary, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: temporary) }
        let url = temporary.appendingPathComponent("workflow.json")
        let company: [String: Any] = ["id": "test-company", "name": "Test company", "drive_folder_id": "folder_1234567890"]
        let adapter: [String: Any] = ["type": "codex_drive", "enabled": false, "requires_route": true,
                                    "meetings_root_id": "root_1234567890", "codex": "/usr/bin/true"]
        let original: [String: Any] = [
            "recordings_root": "/not-opened",
            "custom": ["preserve": [1, 2, 3]],
            "notes": ["enabled": false, "command": ["/usr/bin/true", "{input}"]],
            "route": ["enabled": false, "command": ["/usr/bin/true", "{companies}"],
                      "review_required": true, "companies": [company]],
            "archive": adapter, "publish": adapter
        ]
        func write(_ value: Any) throws {
            try JSONSerialization.data(withJSONObject: value, options: .sortedKeys).write(to: url)
        }
        func object() throws -> [String: Any] {
            try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        }
        func rejected(_ action: () throws -> Void) throws {
            let before = try Data(contentsOf: url)
            var didThrow = false
            do { try action() } catch { didThrow = true }
            precondition(didThrow, "Invalid activation must fail")
            let after = try Data(contentsOf: url)
            precondition(after == before, "Rejected changes must preserve the file")
        }
        try write(original)
        var preferences = try WorkflowPreferences(url: url)
        precondition(preferences.notesConfigured && preferences.driveConfigured)
        try rejected { try WorkflowPreferences.setDrive(true, at: url, transcriptionEnabled: true) }
        try rejected { try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: false) }
        try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true)
        preferences = try WorkflowPreferences(url: url)
        precondition(preferences.notesEnabled && !preferences.driveActive)
        try WorkflowPreferences.setDrive(true, at: url, transcriptionEnabled: true)
        preferences = try WorkflowPreferences(url: url)
        precondition(preferences.driveEnabled)
        let enabledObject = try object()
        for key in ["custom", "recordings_root"] {
            precondition(NSDictionary(dictionary: [key: enabledObject[key]!]).isEqual(to: [key: original[key]!]))
        }
        for key in ["notes", "route", "archive", "publish"] {
            var actual = enabledObject[key] as! [String: Any]
            var expected = original[key] as! [String: Any]
            actual.removeValue(forKey: "enabled")
            expected.removeValue(forKey: "enabled")
            precondition(NSDictionary(dictionary: actual).isEqual(to: expected), "Adapter fields changed")
        }
        let attributes = try fm.attributesOfItem(atPath: url.path)
        precondition((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)
        try WorkflowPreferences.setNotes(false, at: url, transcriptionEnabled: true)
        preferences = try WorkflowPreferences(url: url)
        precondition(!preferences.notesActive && !preferences.driveActive)
        try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true)
        preferences = try WorkflowPreferences(url: url)
        precondition(!preferences.driveActive, "Enabling notes must not enable uploads")
        try WorkflowPreferences.setDrive(true, at: url, transcriptionEnabled: true)
        try WorkflowPreferences.setDrive(false, at: url, transcriptionEnabled: true)
        preferences = try WorkflowPreferences(url: url)
        precondition(preferences.notesEnabled, "Turning off Drive must keep notes enabled")

        // A numeric 1 is not a valid opt-in, even though Foundation bridges it to Bool.
        var invalid = original
        invalid["notes"] = ["enabled": 1, "command": ["/usr/bin/true"]]
        try write(invalid)
        try rejected { try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true) }
        try write(["notes": []])
        try rejected { try WorkflowPreferences.setNotes(false, at: url, transcriptionEnabled: true) }
        try write([1, 2])
        try rejected { try WorkflowPreferences.setDrive(false, at: url, transcriptionEnabled: true) }
        try Data("broken JSON".utf8).write(to: url)
        try rejected { try WorkflowPreferences.setNotes(false, at: url, transcriptionEnabled: true) }

        for command in [[], [""], ["/missing/executable"], ["true"]] {
            invalid = original
            invalid["notes"] = ["enabled": false, "command": command]
            try write(invalid)
            try rejected { try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true) }
        }
        // A valid command cannot make the worker's Drive-only adapter a notes
        // or routing runner. rclone outside archive/publish uses command fallback.
        for key in ["notes", "route"] {
            invalid = original
            var stage = original[key] as! [String: Any]
            stage["type"] = "codex_drive"
            invalid[key] = stage
            try write(invalid)
            preferences = try WorkflowPreferences(url: url)
            precondition(!preferences.notesConfigured)
            try rejected { try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true) }
            stage["type"] = "rclone"
            invalid[key] = stage
            try write(invalid)
            try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true)
            preferences = try WorkflowPreferences(url: url)
            precondition(preferences.notesEnabled)
        }
        for folders in [[company, company], [["id": "invalid", "name": "Test", "drive_folder_id": "https://drive.google.com/folder"]]] {
            invalid = original
            var route = original["route"] as! [String: Any]
            route["companies"] = folders
            invalid["route"] = route
            try write(invalid)
            try rejected { try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true) }
        }
        // Optional aliases remain private configuration, preserved verbatim by
        // activation toggles. Readiness must accept the worker's valid shape.
        for aliases in [[], ["Ackme", "Acme Studio"], [String(repeating: "ß", count: 100)],
                        Array(repeating: "Ackme", count: 20)] {
            var configured = original
            var route = original["route"] as! [String: Any]
            var aliasedCompany = company
            aliasedCompany["aliases"] = aliases
            route["companies"] = [aliasedCompany]
            configured["route"] = route
            try write(configured)
            preferences = try WorkflowPreferences(url: url)
            precondition(preferences.notesConfigured && preferences.driveConfigured)
            try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true)
            try WorkflowPreferences.setDrive(true, at: url, transcriptionEnabled: true)
            let savedRoute = try object()["route"] as! [String: Any]
            let savedCompanies = savedRoute["companies"] as! [[String: Any]]
            precondition(NSDictionary(dictionary: savedCompanies[0]).isEqual(to: aliasedCompany))
        }
        let invalidAliases: [Any] = [NSNull(), "Ackme", ["field": "Ackme"], [NSNull()], [1], [true],
                                    [""], [" "], [" Ackme"], ["Ackme "], ["Ack\nme"],
                                    ["Ack\0me"], ["Ack\u{7f}me"], [String(repeating: "x", count: 201)],
                                    [String(repeating: "ß", count: 101)], Array(repeating: "Ackme", count: 21)]
        for aliases in invalidAliases {
            invalid = original
            var route = original["route"] as! [String: Any]
            var aliasedCompany = company
            aliasedCompany["aliases"] = aliases
            route["companies"] = [aliasedCompany]
            invalid["route"] = route
            try write(invalid)
            preferences = try WorkflowPreferences(url: url)
            precondition(!preferences.notesConfigured && !preferences.driveConfigured)
            try rejected { try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true) }
        }
        invalid = original
        var publish = adapter
        publish["meetings_root_id"] = "different_1234567890"
        invalid["publish"] = publish
        try write(invalid)
        try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true)
        try rejected { try WorkflowPreferences.setDrive(true, at: url, transcriptionEnabled: true) }
        try write(original)
        let link = temporary.appendingPathComponent("linked.json")
        try fm.createSymbolicLink(at: link, withDestinationURL: url)
        try rejected { try WorkflowPreferences.setNotes(true, at: link, transcriptionEnabled: true) }

        let statusURL = temporary.appendingPathComponent("status.json")
        try Data("{\"pending_reviews\":2}".utf8).write(to: statusURL)
        let status = CompletionUIStatus(settingsURL: url, statusURL: statusURL)
        precondition(status.pendingReviews == 2 && status.preferences?.notesConfigured == true)
        precondition(!status.notes.isEmpty && !status.drive.isEmpty)
        func processing(_ state: String, revision: String? = nil) throws -> CompletionUIStatus {
            var value: [String: Any] = ["state": state, "title": "Meeting processing", "detail": "Synthetic status",
                                        "retryable_jobs": 1]
            if let revision { value["alert_revision"] = revision }
            try JSONSerialization.data(withJSONObject: ["processing": value, "pending_reviews": 1]).write(to: statusURL)
            return CompletionUIStatus(settingsURL: url, statusURL: statusURL)
        }
        for state in ["idle", "queued", "running", "retrying", "waiting_for_company"] {
            let current = try processing(state, revision: "must-not-notify")
            precondition(current.processingTitle == "Meeting processing")
            precondition(!current.needsAttention && current.alertRevision == nil,
                         "Healthy processing and company review must not produce failure alerts")
        }
        let failed = try processing("needs_attention", revision: "stable-failure-id")
        precondition(failed.needsAttention && failed.alertRevision == "stable-failure-id" && failed.retryableJobs == 1)
        try JSONSerialization.data(withJSONObject: ["processing": [
            "state": "running", "title": "Writing notes", "detail": "Current meeting",
            "attention_detail": "An older meeting needs review", "alert_revision": "older-failure"]]).write(to: statusURL)
        let activeWithFailure = CompletionUIStatus(settingsURL: url, statusURL: statusURL)
        precondition(activeWithFailure.processingTitle == "Writing notes" && activeWithFailure.needsAttention)
        precondition(activeWithFailure.attentionDetail == "An older meeting needs review"
                     && activeWithFailure.alertRevision == "older-failure")
        let unknown = try processing("unrecognized")
        precondition(unknown.processingTitle == nil)
        let statusLink = temporary.appendingPathComponent("status-link.json")
        try fm.createSymbolicLink(at: statusLink, withDestinationURL: statusURL)
        precondition(CompletionUIStatus(settingsURL: url, statusURL: statusLink).processingTitle == nil)
        try Data(repeating: 32, count: 65_537).write(to: statusURL)
        precondition(CompletionUIStatus(settingsURL: url, statusURL: statusURL).processingTitle == nil)

        var recovery = CompletionWorkerRecovery()
        recovery.started(now: 0)
        recovery.exited(status: 0, now: 1)
        precondition(!recovery.shouldRestart(now: 999), "A normal exit must not restart")
        recovery.exited(status: 1, now: 10)
        precondition(!recovery.shouldRestart(now: 39) && recovery.shouldRestart(now: 40))
        recovery.started(now: 40)
        precondition(!recovery.shouldRestart(now: 1000), "A running supervisor must not relaunch")
        recovery.exited(status: 1, now: 41)
        precondition(recovery.retryAt == 101)
        recovery.started(now: 101)
        recovery.exited(status: 1, now: 102)
        precondition(recovery.retryAt == 222)
        recovery.started(now: 222)
        recovery.exited(status: 1, now: 223)
        precondition(recovery.exhausted && !recovery.shouldRestart(now: 9999), "Recovery must be bounded")
        recovery = CompletionWorkerRecovery()
        recovery.exited(status: 1, now: 0)
        recovery.started(now: 30)
        recovery.healthy(now: 329)
        precondition(recovery.failures == 1)
        recovery.healthy(now: 330)
        precondition(recovery.failures == 0, "Only sustained healthy supervision resets crash history")
        print("Workflow preferences: activation dependencies, preserved settings, malformed input, and status checks passed")
    }
}
