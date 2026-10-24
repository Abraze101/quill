import Foundation

/// Optional user config at ~/.config/quill/config.json:
///
///     {
///       "recordings_dir": "~/Recordings",
///       "transcription": { "enabled": true, "engine": "parakeet" },
///       "mic_voice_processing": true,
///       "on_stop": "my-hook"
///     }
///
/// Resolution order for the recordings root: --out flag > config file >
/// ~/Recordings. `on_stop` is a shell command spawned with the session
/// directory as its argument — after the transcript is written, or right
/// after recording when transcription is disabled.
enum Config {
    static let path = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".config/quill/config.json")

    static let defaultRoot = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Recordings", isDirectory: true)

    /// The configured recordings root, or nil if no config file / no key.
    static func recordingsDir() -> URL? {
        guard let dir = load()?["recordings_dir"] as? String, !dir.isEmpty else { return nil }
        return URL(fileURLWithPath: (dir as NSString).expandingTildeInPath, isDirectory: true)
    }

    /// Shell command to spawn after each session's transcript is written (or
    /// after recording, if transcription is disabled), or nil.
    static func onStop() -> String? {
        guard let cmd = load()?["on_stop"] as? String, !cmd.isEmpty else { return nil }
        return cmd
    }

    /// Retrying an arbitrary shell hook is unsafe unless it is idempotent.
    /// Our durable queue hook explicitly opts into six attempts in its setup.
    static func onStopMaxAttempts() -> Int {
        min(6, max(1, load()?["on_stop_max_attempts"] as? Int ?? 1))
    }

    /// Optional command to resume already queued completion jobs on launch.
    /// Arguments are passed directly, without shell interpolation.
    static func completionWorkerCommand() -> [String]? {
        guard let command = load()?["completion_worker"] as? [String],
              let executable = command.first, executable.hasPrefix("/"),
              command.allSatisfy({ !$0.isEmpty }) else { return nil }
        return command
    }

    /// The optional review surface belongs to the completion worker.
    static func meetingReviewCommand() -> [String]? {
        guard let command = load()?["meeting_review"] as? [String],
              let executable = command.first, executable.hasPrefix("/"),
              command.allSatisfy({ !$0.isEmpty }) else { return nil }
        return command
    }

    static func completionStatusURL() -> URL {
        let path = load()?["completion_status_file"] as? String
            ?? "~/.local/state/quill/workflow/status.json"
        return URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
    }

    static func workflowSettingsURL() -> URL {
        let path = load()?["workflow_config_file"] as? String ?? "~/.config/quill/workflow.json"
        return URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
    }

    /// Transcription requires explicit opt-in in this workflow. Missing or
    /// malformed configuration must not start processing existing recordings.
    static func transcriptionEnabled() -> Bool {
        transcription()?["enabled"] as? Bool == true
    }

    /// Configured engine name. Only "parakeet" ships today; the coordinator
    /// warns and falls back for anything else.
    static func transcriptionEngine() -> String {
        transcription()?["engine"] as? String ?? "parakeet"
    }

    /// Optional on-device speaker separation. The value is frozen into each
    /// new recording; changing it never opts an older recording into analysis.
    static func speakerIdentificationEnabled() -> Bool {
        transcription()?["speaker_identification"] as? Bool == true
    }

    /// Automatic starts are opt-in. A missing detector or config never starts
    /// capture, and disabling this leaves ordinary manual recording available.
    static func meetingAutomationEnabled() -> Bool {
        guard let settings = load()?["automation"] as? [String: Any] else { return false }
        return settings["enabled"] as? Bool == true
    }

    static func screenVideoEnabled() -> Bool {
        (load()?["screen_recording"] as? [String: Any])?["enabled"] as? Bool == true
    }

    static func screenCaptureScope() -> String {
        (load()?["screen_recording"] as? [String: Any])?["scope"] as? String == "display" ? "display" : "window"
    }

    static func screenCaptureSelection() -> String {
        let settings = load()?["screen_recording"] as? [String: Any]
        return settings?[screenCaptureScope() == "display" ? "display" : "window"] as? String ?? "automatic"
    }

    static func setMeetingAutomationEnabled(_ enabled: Bool) throws {
        try update(section: "automation", values: ["enabled": enabled])
    }

    static func setScreenVideoEnabled(_ enabled: Bool) throws {
        try update(section: "screen_recording", values: ["enabled": enabled])
    }

    static func setScreenCaptureScope(_ scope: String) throws {
        try update(section: "screen_recording", values: ["scope": scope == "display" ? "display" : "window"])
    }

    static func setScreenCaptureSelection(_ selection: String) throws {
        try update(section: "screen_recording", values: [screenCaptureScope(): selection])
    }

    static func setTranscriptionEnabled(_ enabled: Bool) throws {
        try update(section: "transcription", values: ["enabled": enabled])
    }

    static func setSpeakerIdentificationEnabled(_ enabled: Bool) throws {
        try update(section: "transcription", values: ["speaker_identification": enabled])
    }

    /// Safeguards apply to new sessions. Invalid/missing values retain the
    /// approved two-hour default instead of silently disabling the limit.
    static func recordingCheckInHours() -> Int {
        let hours = (load()?["recording_safety"] as? [String: Any])?["check_in_hours"] as? Int ?? 2
        return [1, 2, 4].contains(hours) ? hours : 2
    }

    static func showInDock() -> Bool {
        (load()?["recording_safety"] as? [String: Any])?["show_in_dock"] as? Bool ?? true
    }

    static func setRecordingCheckInHours(_ hours: Int) throws {
        guard [1, 2, 4].contains(hours) else { throw CocoaError(.validationMissingMandatoryProperty) }
        try update(section: "recording_safety", values: ["check_in_hours": hours])
    }

    static func setShowInDock(_ enabled: Bool) throws {
        try update(section: "recording_safety", values: ["show_in_dock": enabled])
    }

    /// Preserve unrelated settings; never replace malformed configuration with
    /// fresh defaults. UI actions report failures and reload the actual state.
    private static func update(section: String, values: [String: Any]) throws {
        let fm = FileManager.default
        var json: [String: Any] = [:]
        if fm.fileExists(atPath: path.path) {
            let data = try Data(contentsOf: path)
            guard let parsed = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw CocoaError(.fileReadCorruptFile)
            }
            json = parsed
        }
        var settings = json[section] as? [String: Any] ?? [:]
        settings.merge(values) { _, new in new }
        json[section] = settings
        try fm.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try JSONSerialization.data(withJSONObject: json, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: path, options: .atomic)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
    }

    private static func transcription() -> [String: Any]? {
        load()?["transcription"] as? [String: Any]
    }

    /// Apple voice processing (acoustic echo cancellation) on the mic, so
    /// speaker playback doesn't bleed into the mic track and get transcribed
    /// as "me". Default off — the live voice unit ducks all other playback,
    /// and on headphones there's no echo to cancel anyway. Set true when
    /// recording meetings through the speakers.
    static func micVoiceProcessing() -> Bool {
        load()?["mic_voice_processing"] as? Bool ?? false
    }

    /// Parse the config file. A malformed config is reported on stderr rather
    /// than silently ignored — recordings landing in an unexpected place is
    /// worse than a warning.
    private static func load() -> [String: Any]? {
        guard FileManager.default.fileExists(atPath: path.path) else { return nil }
        guard
            let data = try? Data(contentsOf: path),
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            FileHandle.standardError.write(Data(
                "warning: \(path.path) is not valid JSON — ignoring config\n".utf8
            ))
            return nil
        }
        return json
    }

    /// Resolve the recordings root from an optional CLI override.
    static func resolveRoot(cliOverride: String?) -> URL {
        if let cliOverride {
            return URL(
                fileURLWithPath: (cliOverride as NSString).expandingTildeInPath,
                isDirectory: true
            )
        }
        return recordingsDir() ?? defaultRoot
    }
}
