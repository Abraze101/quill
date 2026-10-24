import ArgumentParser
import Foundation

/// Preserve the legacy LaunchAgent command for standalone binary installs.
/// App bundles manage launch at login in Settings through SMAppService instead.
struct Install: ParsableCommand {
    static let configuration = CommandConfiguration(
        abstract: "Install or remove the legacy launch-at-login LaunchAgent."
    )

    @Flag(name: .long, help: "Register a standalone quill binary to start at login.")
    var launchAtLogin: Bool = false

    @Flag(name: .long, help: "Remove only the legacy LaunchAgent; app login settings are unchanged.")
    var uninstall: Bool = false

    func run() throws {
        if launchAtLogin == uninstall {
            FileHandle.standardError.write(Data(
                "specify exactly one of --launch-at-login or --uninstall\n".utf8
            ))
            throw ExitCode(64)
        }

        if uninstall {
            try removeAgent()
        } else {
            try writeAgent()
        }
    }

    // MARK: -

    private static let label = "com.digimata.quill"

    private var plistURL: URL {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home
            .appendingPathComponent("Library/LaunchAgents", isDirectory: true)
            .appendingPathComponent("\(Self.label).plist")
    }

    private func writeAgent() throws {
        try ensureLegacyInstallAllowed()
        let binary = try resolveBinaryPath()

        let plist: [String: Any] = [
            "Label": Self.label,
            "ProgramArguments": [binary, "run"],
            "RunAtLoad": true,
            "KeepAlive": ["SuccessfulExit": false] as [String: Any],
            "ProcessType": "Interactive",
            "StandardOutPath": "/tmp/quill.out.log",
            "StandardErrorPath": "/tmp/quill.err.log",
        ]

        let url = plistURL
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let data = try PropertyListSerialization.data(
            fromPropertyList: plist,
            format: .xml,
            options: 0
        )
        try data.write(to: url, options: .atomic)

        // Best-effort bootstrap; ignore failure if already loaded.
        _ = runLaunchctl(["bootout", "gui/\(uid())", url.path])
        let result = runLaunchctl(["bootstrap", "gui/\(uid())", url.path])
        if result.status != 0 {
            FileHandle.standardError.write(Data(
                "warning: launchctl bootstrap exited \(result.status):\n\(result.stderr)\n".utf8
            ))
        }

        print("✓ legacy launch-at-login installed")
        print("  plist:  \(url.path)")
        print("  binary: \(binary)")
        print("  logs:   /tmp/quill.out.log, /tmp/quill.err.log")
    }

    private func removeAgent() throws {
        let url = plistURL
        if FileManager.default.fileExists(atPath: url.path) {
            _ = runLaunchctl(["bootout", "gui/\(uid())", url.path])
            try FileManager.default.removeItem(at: url)
            print("✓ legacy LaunchAgent plist removed")
        } else {
            print("nothing to remove (no legacy agent at \(url.path))")
        }
        print("Quill.app's Launch at login setting is unchanged; manage it in Quill Settings.")
    }

    private func ensureLegacyInstallAllowed() throws {
        let installedLocations = [
            URL(fileURLWithPath: "/Applications/Quill.app", isDirectory: true),
            FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Applications/Quill.app", isDirectory: true),
        ]
        let installedApp = installedLocations.first { url in
            Bundle(url: url)?.bundleIdentifier == Self.label
        }
        guard Bundle.main.bundleURL.pathExtension.lowercased() == "app" || installedApp != nil else {
            return
        }

        let instruction: String
        if let installedApp {
            instruction = "Open \(installedApp.path) and use Launch at login in Quill Settings."
        } else {
            instruction = "Install Quill.app in Applications, then use Launch at login in Quill Settings."
        }
        FileHandle.standardError.write(Data(
            "Legacy LaunchAgent registration is unavailable for app installs. \(instruction)\n".utf8
        ))
        throw ExitCode(1)
    }

    private func resolveBinaryPath() throws -> String {
        // /usr/local/bin/quill is the canonical install path. Honor a real
        // location if running from elsewhere (e.g. dev).
        let candidate = "/usr/local/bin/quill"
        if FileManager.default.isExecutableFile(atPath: candidate) {
            return candidate
        }
        // Fall back to the running executable's resolved path.
        let argv0 = CommandLine.arguments.first ?? "quill"
        if argv0.hasPrefix("/"), FileManager.default.isExecutableFile(atPath: argv0) {
            FileHandle.standardError.write(Data(
                "note: /usr/local/bin/quill not found; using \(argv0)\n".utf8
            ))
            return argv0
        }
        FileHandle.standardError.write(Data(
            "couldn't locate the quill binary. install it to /usr/local/bin/quill first.\n".utf8
        ))
        throw ExitCode(1)
    }

    private func uid() -> uid_t { getuid() }

    private func runLaunchctl(_ args: [String]) -> (status: Int32, stderr: String) {
        let task = Process()
        task.launchPath = "/bin/launchctl"
        task.arguments = args
        let errPipe = Pipe()
        task.standardError = errPipe
        task.standardOutput = Pipe()
        do {
            try task.run()
        } catch {
            return (-1, "\(error)")
        }
        task.waitUntilExit()
        let err = String(
            data: errPipe.fileHandleForReading.readDataToEndOfFile(),
            encoding: .utf8
        ) ?? ""
        return (task.terminationStatus, err)
    }
}
