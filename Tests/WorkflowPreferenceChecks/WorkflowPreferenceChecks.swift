import Darwin
import Foundation

@main
enum WorkflowPreferenceChecks {
    static func main() throws {
        if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--toggle-documents" {
            try WorkflowPreferences.setDriveDocuments(true, at: URL(fileURLWithPath: CommandLine.arguments[2]),
                                                      transcriptionEnabled: true)
            return
        }
        let fm = FileManager.default
        let temporary = fm.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try fm.createDirectory(at: temporary, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: temporary) }
        let url = temporary.appendingPathComponent("workflow.json")
        let company: [String: Any] = ["id": "test-company", "name": "Test company", "drive_folder_id": "folder_1234567890"]
        let account: [String: Any] = ["link_id": "link_test_account", "email": "meetings@example.test",
                                     "profile_id": "profile_123456", "verified_at": 1_700_000_000.0,
                                     "meetings_root_id": "root_1234567890"]
        let adapter: [String: Any] = ["type": "codex_drive", "enabled": false, "requires_route": true,
                                    "meetings_root_id": "root_1234567890", "codex": "/usr/bin/true",
                                    "account_link_id": "link_test_account",
                                    "text_format": "combined_google_doc_v1"]
        let original: [String: Any] = [
            "recordings_root": "/not-opened",
            "custom": ["preserve": [1, 2, 3]],
            "drive_account": account,
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
        func setOutput(_ key: String, _ enabled: Bool, transcription: Bool = true) throws {
            if key == "publish" {
                try WorkflowPreferences.setDriveDocuments(enabled, at: url, transcriptionEnabled: transcription)
            } else {
                try WorkflowPreferences.setDriveRecordings(enabled, at: url, transcriptionEnabled: transcription)
            }
        }
        func configured(documents: Bool, recordings: Bool) -> [String: Any] {
            var value = original
            for key in ["notes", "route", "publish", "archive"] {
                var stage = value[key] as! [String: Any]
                stage["enabled"] = key == "publish" ? documents : key == "archive" ? recordings : true
                value[key] = stage
            }
            return value
        }

        try write(original)
        let connected = try WorkflowPreferences(url: url)
        precondition(connected.driveAccountEmail == "meetings@example.test")
        precondition(connected.driveAccountDescription == "Connected as meetings@example.test.")
        var missingAccount = configured(documents: true, recordings: true)
        missingAccount.removeValue(forKey: "drive_account")
        try write(missingAccount)
        let missingSelection = try WorkflowPreferences(url: url)
        precondition(missingSelection.driveAccountEmail == nil)
        precondition(missingSelection.driveAccountDescription.contains("Choose a Google Drive account"))
        precondition(!missingSelection.driveDocumentsConfigured && !missingSelection.driveRecordingsConfigured)
        try rejected { try setOutput("publish", true) }
        try rejected { try setOutput("archive", true) }
        try setOutput("publish", false, transcription: false)
        try setOutput("archive", false, transcription: false)

        var invalidAccounts: [[String: Any]] = []
        for key in account.keys {
            var incomplete = account
            incomplete.removeValue(forKey: key)
            invalidAccounts.append(incomplete)
        }
        let invalidAccountFields: [(String, [Any])] = [
            ("link_id", [NSNull(), "", "link_", "link_account\n", "link_a/b", String(repeating: "a", count: 206)]),
            ("email", [NSNull(), "", "not-an-email", " name@example.test", "name@example.test\n", "na\u{7f}me@example.test"]),
            ("profile_id", [NSNull(), "", "profile id", "profile/id", 123]),
            ("verified_at", [NSNull(), true, false, 0, -1, "1700000000"]),
            ("meetings_root_id", [NSNull(), "", "short", "root/path", "root_1234567890\n"])
        ]
        for (key, values) in invalidAccountFields {
            for value in values {
                var invalid = account
                invalid[key] = value
                invalidAccounts.append(invalid)
            }
        }
        for invalidAccount in invalidAccounts {
            var value = configured(documents: true, recordings: true)
            value["drive_account"] = invalidAccount
            try write(value)
            let selected = try WorkflowPreferences(url: url)
            precondition(selected.notesConfigured && selected.notesEnabled)
            precondition(!selected.driveDocumentsConfigured && !selected.driveRecordingsConfigured)
            precondition(selected.driveAccountEmail == nil && selected.driveAccountDescription.contains("Verify"))
            for key in ["publish", "archive"] {
                try rejected { try setOutput(key, true) }
                try setOutput(key, false, transcription: false)
                var stage = value[key] as! [String: Any]
                stage["enabled"] = false
                value[key] = stage
                let saved = try object()
                precondition(NSDictionary(dictionary: saved).isEqual(to: value),
                             "Disabling an invalid account must change only the selected flag")
            }
        }
        for key in ["archive", "publish"] {
            for field in ["account_link_id", "meetings_root_id"] {
                for replacement: Any in [NSNull(), "", field == "account_link_id" ? "link_other_account" : "other_root_123456"] {
                    var value = configured(documents: true, recordings: true)
                    var changed = value[key] as! [String: Any]
                    changed[field] = replacement
                    value[key] = changed
                    try write(value)
                    let selected = try WorkflowPreferences(url: url)
                    precondition(selected.driveAccountEmail == nil)
                    precondition(!selected.driveDocumentsConfigured && !selected.driveRecordingsConfigured)
                    try rejected { try setOutput("publish", true) }
                    try rejected { try setOutput("archive", true) }
                    try setOutput(key, false, transcription: false)
                }
            }
        }

        // A toggle waits for the account picker lock, then reads its completed
        // selection. Reading before the lock would overwrite the new account.
        try write(configured(documents: false, recordings: false))
        let lockDescriptor = open(url.path + ".lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        precondition(lockDescriptor >= 0)
        let lockHandle = FileHandle(fileDescriptor: lockDescriptor, closeOnDealloc: true)
        precondition(flock(lockDescriptor, LOCK_EX) == 0)
        let toggle = Process()
        toggle.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        toggle.arguments = ["--toggle-documents", url.path]
        toggle.standardOutput = FileHandle.nullDevice
        toggle.standardError = FileHandle.nullDevice
        try toggle.run()
        Thread.sleep(forTimeInterval: 0.15)
        precondition(toggle.isRunning, "The toggle must wait for the account-selection lock")
        var concurrentSelection = configured(documents: false, recordings: false)
        var newAccount = account
        newAccount["link_id"] = "link_reselected_account"
        newAccount["email"] = "selected@example.test"
        concurrentSelection["drive_account"] = newAccount
        concurrentSelection["selection_marker"] = "preserve the latest selection"
        for key in ["archive", "publish"] {
            var stage = concurrentSelection[key] as! [String: Any]
            stage["account_link_id"] = "link_reselected_account"
            concurrentSelection[key] = stage
        }
        try write(concurrentSelection)
        precondition(flock(lockDescriptor, LOCK_UN) == 0)
        try lockHandle.close()
        let deadline = Date().addingTimeInterval(5)
        while toggle.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.02) }
        if toggle.isRunning { toggle.terminate() }
        toggle.waitUntilExit()
        precondition(toggle.terminationStatus == 0)
        var enabledPublish = concurrentSelection["publish"] as! [String: Any]
        enabledPublish["enabled"] = true
        concurrentSelection["publish"] = enabledPublish
        let concurrentSaved = try object()
        precondition(NSDictionary(dictionary: concurrentSaved).isEqual(to: concurrentSelection),
                     "A toggle must preserve an account selection completed while it waited for the lock")

        // Loading all four combinations preserves their exact choice; each
        // control changes only its own activation flag in either direction.
        for documents in [false, true] {
            for recordings in [false, true] {
                let combination = configured(documents: documents, recordings: recordings)
                try write(combination)
                let beforeRead = try Data(contentsOf: url)
                let selected = try WorkflowPreferences(url: url)
                precondition(selected.driveDocumentsEnabled == documents)
                precondition(selected.driveRecordingsEnabled == recordings)
                precondition(selected.driveActive == (documents || recordings))
                precondition(!selected.driveDescription.contains("partly enabled"))
                let afterRead = try Data(contentsOf: url)
                precondition(afterRead == beforeRead, "Reading settings must never migrate flags")
                for key in ["publish", "archive"] {
                    for desired in [false, true] {
                        try write(combination)
                        try setOutput(key, desired)
                        var expected = combination
                        var changed = expected[key] as! [String: Any]
                        changed["enabled"] = desired
                        expected[key] = changed
                        let saved = try object()
                        precondition(NSDictionary(dictionary: saved).isEqual(to: expected),
                                     "A Drive control changed another choice or adapter field")
                    }
                }
            }
        }

        // Omitted text_format is the worker's legacy inventory, not the
        // combined document format. Mixed existing formats remain unchanged.
        let formats: [String?] = [nil, "legacy", "combined_google_doc_v1", "native_template_google_doc_v1"]
        for documentFormat in formats {
            for recordingFormat in formats {
                var combination = configured(documents: true, recordings: true)
                for (key, format) in [("publish", documentFormat), ("archive", recordingFormat)] {
                    var stage = combination[key] as! [String: Any]
                    stage["text_format"] = format
                    combination[key] = stage
                }
                try write(combination)
                let beforeRead = try Data(contentsOf: url)
                var selected = try WorkflowPreferences(url: url)
                let legacyDocuments = documentFormat == nil || documentFormat == "legacy"
                let legacyRecordings = recordingFormat == nil || recordingFormat == "legacy"
                precondition(selected.driveDocumentsConfigured && selected.driveRecordingsConfigured)
                precondition(selected.driveDocumentsTitle == (legacyDocuments
                    ? "Save notes files to Drive" : "Save notes and transcript to Drive"))
                precondition(selected.driveRecordingsTitle == (legacyRecordings
                    ? "Upload recordings and transcripts to Drive" : "Upload audio and video to Drive"))
                precondition(selected.driveDocumentsDescription.contains("Markdown and JSON") == legacyDocuments)
                precondition(selected.driveDocumentsDescription.contains("one Google Doc") == !legacyDocuments)
                precondition(selected.driveRecordingsDescription.contains("available transcript files") == legacyRecordings)
                precondition(selected.driveRecordingsDescription.contains("recording metadata") == legacyRecordings)
                let afterRead = try Data(contentsOf: url)
                precondition(afterRead == beforeRead, "Rendering legacy labels must not migrate formats or flags")
                try setOutput("publish", false)
                selected = try WorkflowPreferences(url: url)
                precondition(!selected.driveDocumentsEnabled && selected.driveRecordingsEnabled)
                if legacyRecordings {
                    precondition(selected.driveDescription.contains("Recording, transcript and metadata uploads are on."))
                    precondition(!selected.driveDescription.contains("Notes and transcript filing is off"))
                }
                try setOutput("publish", true)
                try setOutput("archive", false)
                try setOutput("archive", true)
                let saved = try object()
                precondition(NSDictionary(dictionary: saved).isEqual(to: combination),
                             "Toggling legacy connections must not add or change text_format")
            }
        }
        try write([String: Any]())
        let unconfigured = try WorkflowPreferences(url: url)
        precondition(unconfigured.driveDocumentsTitle == "Save notes and transcript to Drive")
        precondition(unconfigured.driveRecordingsTitle == "Upload audio and video to Drive")

        for key in ["publish", "archive"] {
            let peer = key == "publish" ? "archive" : "publish"
            for format: Any in [NSNull(), 1, true, "", "unknown", " legacy", ["legacy"], ["name": "legacy"]] {
                var invalidFormat = configured(documents: false, recordings: false)
                var stage = invalidFormat[key] as! [String: Any]
                stage["text_format"] = format
                invalidFormat[key] = stage
                try write(invalidFormat)
                let selected = try WorkflowPreferences(url: url)
                precondition(key == "publish" ? !selected.driveDocumentsConfigured : !selected.driveRecordingsConfigured)
                try rejected { try setOutput(key, true) }
                try setOutput(peer, true)
                var activeInvalid = try object()
                stage["enabled"] = true
                activeInvalid[key] = stage
                try write(activeInvalid)
                try setOutput(key, false, transcription: false)
                let disabled = try object()
                precondition((disabled[peer] as! [String: Any])["enabled"] as? Bool == true)
                precondition((disabled[key] as! [String: Any])["enabled"] as? Bool == false)
            }
        }

        for key in ["publish", "archive"] {
            let peer = key == "publish" ? "archive" : "publish"
            // Missing, unconfigured or differently rooted inactive peers do
            // not block a separately configured connection.
            let unusedPeers: [[String: Any]?] = [nil, [:], ["enabled": false, "type": "unconfigured"],
                                                adapter.merging(["meetings_root_id": "other_root_12345"]) { _, new in new }]
            for unused in unusedPeers {
                var value = configured(documents: false, recordings: false)
                value[peer] = unused
                try write(value)
                try setOutput(key, true)
                let saved = try object()
                precondition((saved[key] as! [String: Any])["enabled"] as? Bool == true)
                precondition(NSDictionary(dictionary: saved[peer] as? [String: Any] ?? [:])
                    .isEqual(to: unused ?? [:]))
                precondition((saved[peer] == nil) == (unused == nil), "Missing peer must stay missing")
                try setOutput(key, false)
            }

            // A currently enabled connection must remain compatible. An
            // invalid adapter can always be disabled without fixing it first.
            for other in [adapter.merging(["enabled": true, "meetings_root_id": "other_root_12345"]) { _, new in new },
                          ["enabled": true, "type": "unconfigured"]] {
                var value = configured(documents: false, recordings: false)
                value[peer] = other
                try write(value)
                try rejected { try setOutput(key, true) }
                try setOutput(peer, false, transcription: false)
                let disabled = try object()
                var expected = other
                expected["enabled"] = false
                precondition(NSDictionary(dictionary: disabled[peer] as! [String: Any]).isEqual(to: expected))
                try setOutput(key, true)
            }

            try write(configured(documents: false, recordings: false))
            try rejected { try setOutput(key, true, transcription: false) }
            var invalidTarget = configured(documents: true, recordings: true)
            invalidTarget[key] = ["enabled": true, "type": "unconfigured", "preserve": "yes"]
            try write(invalidTarget)
            try setOutput(key, false, transcription: false)
            let disabled = try object()
            precondition((disabled[peer] as! [String: Any])["enabled"] as? Bool == true)
            precondition((disabled[key] as! [String: Any])["preserve"] as? String == "yes")
            try rejected { try setOutput(key, true) }
        }

        try write(original)
        var preferences = try WorkflowPreferences(url: url)
        precondition(preferences.notesConfigured && preferences.driveDocumentsConfigured && preferences.driveRecordingsConfigured)
        try rejected { try WorkflowPreferences.setDriveDocuments(true, at: url, transcriptionEnabled: true) }
        try rejected { try WorkflowPreferences.setDriveRecordings(true, at: url, transcriptionEnabled: true) }
        try rejected { try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: false) }
        try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true)
        preferences = try WorkflowPreferences(url: url)
        precondition(preferences.notesEnabled && !preferences.driveActive)
        try WorkflowPreferences.setDriveDocuments(true, at: url, transcriptionEnabled: true)
        try WorkflowPreferences.setDriveRecordings(true, at: url, transcriptionEnabled: true)
        preferences = try WorkflowPreferences(url: url)
        precondition(preferences.driveDocumentsEnabled && preferences.driveRecordingsEnabled)
        let enabledObject = try object()
        for key in ["custom", "recordings_root", "drive_account"] {
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
        try WorkflowPreferences.setDriveDocuments(true, at: url, transcriptionEnabled: true)
        try WorkflowPreferences.setDriveRecordings(true, at: url, transcriptionEnabled: true)
        try WorkflowPreferences.setDriveDocuments(false, at: url, transcriptionEnabled: true)
        preferences = try WorkflowPreferences(url: url)
        precondition(preferences.notesEnabled, "Turning off Drive must keep notes enabled")
        precondition(!preferences.driveDocumentsEnabled && preferences.driveRecordingsEnabled,
                     "Turning off document filing must keep recording uploads enabled")
        try WorkflowPreferences.setDriveRecordings(false, at: url, transcriptionEnabled: false)
        preferences = try WorkflowPreferences(url: url)
        precondition(!preferences.driveActive && preferences.notesEnabled)

        // A numeric 1 is not a valid opt-in, even though Foundation bridges it to Bool.
        var invalid = original
        invalid["notes"] = ["enabled": 1, "command": ["/usr/bin/true"]]
        try write(invalid)
        try rejected { try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true) }
        try write(["notes": []])
        try rejected { try WorkflowPreferences.setNotes(false, at: url, transcriptionEnabled: true) }
        try write([1, 2])
        try rejected { try WorkflowPreferences.setDriveDocuments(false, at: url, transcriptionEnabled: true) }
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
            precondition(preferences.notesConfigured && preferences.driveDocumentsConfigured && preferences.driveRecordingsConfigured)
            try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true)
            try WorkflowPreferences.setDriveDocuments(true, at: url, transcriptionEnabled: true)
            try WorkflowPreferences.setDriveRecordings(true, at: url, transcriptionEnabled: true)
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
            precondition(!preferences.notesConfigured && !preferences.driveDocumentsConfigured && !preferences.driveRecordingsConfigured)
            try rejected { try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true) }
        }
        invalid = original
        var publish = adapter
        publish["meetings_root_id"] = "different_1234567890"
        invalid["publish"] = publish
        try write(invalid)
        try WorkflowPreferences.setNotes(true, at: url, transcriptionEnabled: true)
        try rejected { try WorkflowPreferences.setDriveDocuments(true, at: url, transcriptionEnabled: true) }
        try WorkflowPreferences.setDriveRecordings(true, at: url, transcriptionEnabled: true)
        try write(original)
        let link = temporary.appendingPathComponent("linked.json")
        try fm.createSymbolicLink(at: link, withDestinationURL: url)
        try rejected { try WorkflowPreferences.setNotes(true, at: link, transcriptionEnabled: true) }

        let statusURL = temporary.appendingPathComponent("status.json")
        try Data("{\"pending_reviews\":2}".utf8).write(to: statusURL)
        let status = CompletionUIStatus(settingsURL: url, statusURL: statusURL)
        precondition(status.pendingReviews == 2 && status.preferences?.notesConfigured == true)
        precondition(!status.notes.isEmpty && !status.drive.isEmpty)
        func processing(_ state: String, revision: String? = nil, pendingReviews: Int = 1) throws -> CompletionUIStatus {
            var value: [String: Any] = ["state": state, "title": "Meeting processing", "detail": "Synthetic status",
                                        "retryable_jobs": 1]
            if let revision { value["alert_revision"] = revision }
            try JSONSerialization.data(withJSONObject: ["processing": value, "pending_reviews": pendingReviews]).write(to: statusURL)
            return CompletionUIStatus(settingsURL: url, statusURL: statusURL)
        }
        for state in ["idle", "queued", "running", "retrying", "waiting_for_company"] {
            let current = try processing(state, revision: "must-not-notify")
            precondition(current.processingTitle == "Meeting processing")
            precondition(!current.needsAttention && current.alertRevision == nil,
                         "Healthy processing and company review must not produce failure alerts")
        }
        let upToDate = try processing("idle", pendingReviews: 0)
        precondition(upToDate.processingTitle == "Up to date" && upToDate.processingDetail.isEmpty)
        let failed = try processing("needs_attention", revision: "stable-failure-id")
        precondition(failed.needsAttention && failed.alertRevision == "stable-failure-id" && failed.retryableJobs == 1)
        for state in ["running", "idle"] {
            try JSONSerialization.data(withJSONObject: ["processing": [
                "state": state, "title": "Writing notes", "detail": "Current meeting",
                "attention_detail": "An older meeting needs review", "alert_revision": "older-failure"]]).write(to: statusURL)
            let activeWithFailure = CompletionUIStatus(settingsURL: url, statusURL: statusURL)
            precondition(activeWithFailure.processingTitle == "Writing notes" && activeWithFailure.needsAttention)
            precondition(activeWithFailure.processingDetail == "Current meeting")
            precondition(activeWithFailure.attentionDetail == "An older meeting needs review"
                         && activeWithFailure.alertRevision == "older-failure")
        }
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
