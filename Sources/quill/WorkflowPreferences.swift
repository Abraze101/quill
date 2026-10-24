import CoreFoundation
import Foundation

/// Only edits activation flags. Adapters, destination decisions, and queued jobs
/// remain owned by the completion workflow.
struct WorkflowPreferences {
    private var object: [String: Any]
    private static let stages = ["notes", "route", "archive", "publish"]

    init(url: URL) throws {
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
        guard values.isRegularFile == true, values.isSymbolicLink != true,
              let size = values.fileSize, size <= 100_000 else {
            throw PreferenceError("The completion settings must be a regular JSON file under 100 KB.")
        }
        guard let parsed = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any] else {
            throw PreferenceError("The completion settings are not a JSON object.")
        }
        for key in Self.stages where parsed[key] != nil {
            guard let stage = parsed[key] as? [String: Any],
                  stage["enabled"] == nil || Self.boolean(stage["enabled"]) != nil else {
                throw PreferenceError("The \(key) settings are invalid. The file was left unchanged.")
            }
        }
        object = parsed
    }

    var notesActive: Bool { enabled("notes") || enabled("route") }
    var notesEnabled: Bool { enabled("notes") && enabled("route") }
    var driveActive: Bool { enabled("archive") || enabled("publish") }
    var driveEnabled: Bool { enabled("archive") && enabled("publish") }

    var notesConfigured: Bool {
        Self.command(stage("notes")) && Self.command(stage("route")) && companiesValid
            && Self.boolean(stage("route")["review_required"]) == true
    }

    var driveConfigured: Bool {
        let archive = stage("archive"), publish = stage("publish")
        return Self.driveAdapter(archive) && Self.driveAdapter(publish)
            && (archive["meetings_root_id"] as? String) == (publish["meetings_root_id"] as? String)
            && companiesValid
    }

    var notesDescription: String {
        if !notesConfigured { return "Configure Call Summary, company folders, and folder review before enabling notes." }
        if notesEnabled { return "Call Summary and company suggestions are on for new completed meetings." }
        if notesActive { return "Notes and company suggestions are partly enabled. Switch off, then on to enable both." }
        return "Call Summary and company folders are configured. Automatic notes are off."
    }

    var driveDescription: String {
        if !driveConfigured { return "Configure both Drive adapters with the same meeting folder before enabling filing." }
        if driveEnabled && Self.boolean(stage("route")["auto_file_explicit_companies"]) == true {
            return "Meetings with an explicit company destination are filed automatically. Unclear destinations wait for your folder choice."
        }
        if driveEnabled { return "Recordings, transcripts, and notes will be filed after you approve their company folder." }
        if driveActive { return "Drive filing is partly enabled. Switch off, then on to enable recordings and notes." }
        return "Company folders are configured. Drive filing is off."
    }

    static func setNotes(_ enabled: Bool, at url: URL, transcriptionEnabled: Bool) throws {
        var preferences = try Self(url: url)
        if enabled {
            guard transcriptionEnabled else { throw PreferenceError("Turn on local transcription before enabling meeting notes.") }
            guard preferences.notesConfigured else { throw PreferenceError(preferences.notesDescription) }
        }
        preferences.set(enabled, stages: ["notes", "route"])
        // Drive filing depends on reviewed notes and routing. This dependency is
        // stated beside the switch; turning notes back on does not enable Drive.
        if !enabled { preferences.set(false, stages: ["archive", "publish"]) }
        try preferences.write(to: url)
    }

    static func setDrive(_ enabled: Bool, at url: URL, transcriptionEnabled: Bool) throws {
        var preferences = try Self(url: url)
        if enabled {
            guard transcriptionEnabled && preferences.notesConfigured && preferences.notesEnabled else {
                throw PreferenceError("Enable local transcription and meeting notes before turning on Drive filing.")
            }
            guard preferences.driveConfigured else { throw PreferenceError(preferences.driveDescription) }
        }
        preferences.set(enabled, stages: ["archive", "publish"])
        try preferences.write(to: url)
    }

    private func stage(_ key: String) -> [String: Any] { object[key] as? [String: Any] ?? [:] }
    private func enabled(_ key: String) -> Bool { Self.boolean(stage(key)["enabled"]) == true }

    private mutating func set(_ enabled: Bool, stages: [String]) {
        for key in stages {
            var settings = stage(key)
            settings["enabled"] = enabled
            object[key] = settings
        }
    }

    private func write(to url: URL) throws {
        let data = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    private static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }

    private static func matches(_ value: String, _ pattern: String) -> Bool {
        value.range(of: pattern, options: .regularExpression) != nil
    }

    private static func command(_ settings: [String: Any]) -> Bool {
        // The worker reserves codex_drive for archive/publish even if a
        // command is present. Other types use its ordinary command fallback.
        guard settings["type"] as? String != "codex_drive",
              let args = settings["command"] as? [String], let executable = args.first,
              executable.hasPrefix("/"), args.allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else { return false }
        return FileManager.default.isExecutableFile(atPath: executable)
    }

    private static func driveAdapter(_ settings: [String: Any]) -> Bool {
        guard settings["type"] as? String == "codex_drive",
              boolean(settings["requires_route"]) == true,
              let root = settings["meetings_root_id"] as? String,
              matches(root, "\\A[A-Za-z0-9_-]{10,200}\\z"),
              let codex = settings["codex"] as? String, codex.hasPrefix("/"),
              FileManager.default.isExecutableFile(atPath: codex) else { return false }
        if let model = settings["model"], !(model is NSNull) {
            guard let text = model as? String, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return false }
        }
        return true
    }

    private var companiesValid: Bool {
        guard let companies = stage("route")["companies"] as? [[String: Any]],
              (1...100).contains(companies.count) else { return false }
        var ids = Set<String>(), folders = Set<String>()
        for company in companies {
            guard Set(company.keys).isSubset(of: ["id", "name", "drive_folder_id", "description", "aliases"]),
                  let id = company["id"] as? String, Self.matches(id, "\\A[a-z][a-z0-9_-]{0,63}\\z"),
                  let name = company["name"] as? String, !name.isEmpty, name.count <= 200,
                  name == name.trimmingCharacters(in: .whitespacesAndNewlines),
                  let folder = company["drive_folder_id"] as? String, Self.matches(folder, "\\A[A-Za-z0-9_-]{10,200}\\z"),
                  ids.insert(id).inserted, folders.insert(folder).inserted else { return false }
            if let description = company["description"] {
                guard let text = description as? String, text.count <= 8000 else { return false }
            }
            if let value = company["aliases"] {
                guard let aliases = value as? [String], aliases.count <= 20,
                      aliases.allSatisfy({ alias in
                          !alias.isEmpty && alias.unicodeScalars.count <= 200
                              && alias.folding(options: .caseInsensitive, locale: Locale(identifier: "en_US_POSIX"))
                                  .unicodeScalars.count <= 200
                              && alias == alias.trimmingCharacters(in: .whitespacesAndNewlines)
                              && alias.unicodeScalars.allSatisfy { scalar in
                                  scalar.value >= 32 && !(127...159).contains(scalar.value)
                              }
                      }) else { return false }
            }
        }
        return true
    }

    struct PreferenceError: LocalizedError {
        let errorDescription: String?
        init(_ message: String) { errorDescription = message }
    }
}
