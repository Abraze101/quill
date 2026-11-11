import Foundation

/// Launches the updater sealed inside the current app. Preparing never requests
/// a quit. Only the app controller may hand off an idle session for replacement.
enum QuillAppUpdater {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    struct InstallResult: Decodable {
        let token: UUID
        let success: Bool
        let message: String
    }

    static func prepare(_ candidate: QuillUpdateChecker.Candidate, token: UUID) async throws {
        let arguments = ["prepare", "--commit", candidate.commit, "--version", candidate.version,
                         "--build", candidate.build, "--token", token.uuidString.lowercased()]
        try await Task.detached { try run(arguments, wait: true) }.value
    }

    static func discard(_ token: UUID) async {
        await Task.detached { try? run(["discard", "--token", token.uuidString.lowercased()], wait: true) }.value
    }

    static func applyAfterQuit(_ token: UUID) throws {
        try run(["apply", "--token", token.uuidString.lowercased(), "--pid", String(ProcessInfo.processInfo.processIdentifier)], wait: false)
    }

    static func handOffInterruptedUpdate(confirmRepair: (String) -> Bool) throws -> Bool {
        guard Bundle.main.bundleURL.standardizedFileURL.path == "/Applications/Quill.app" else { return false }
        let directory = FileManager.default.homeDirectoryForCurrentUser.appending(path: ".local/state/quill/app-updates")
        guard FileManager.default.fileExists(atPath: directory.appending(path: "pending.json").path) else { return false }
        let manual = directory.appending(path: "manual-repair.json")
        if FileManager.default.fileExists(atPath: manual.path) {
            struct Repair: Decodable { let message: String }
            var message = "The app and its workflow files could not be verified together."
            if let info = try? manual.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]),
               info.isRegularFile == true, info.isSymbolicLink == false, (info.fileSize ?? Int.max) <= 8192,
               let data = try? Data(contentsOf: manual), let repair = try? JSONDecoder().decode(Repair.self, from: data) {
                message = repair.message
            }
            guard confirmRepair(message) else { return true }
        }
        try run(["recover", "--pid", String(ProcessInfo.processInfo.processIdentifier)], wait: false)
        return true
    }

    private static func run(_ arguments: [String], wait: Bool) throws {
        guard let helper = Bundle.main.url(forResource: "app-update", withExtension: "py"),
              Bundle.main.bundleURL.standardizedFileURL.path == "/Applications/Quill.app" else {
            throw Failure(message: "Open Quill from Applications to install this update.")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-I", "-B", helper.path] + arguments
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        let errors = wait ? Pipe() : nil
        process.standardError = errors ?? FileHandle.nullDevice
        try process.run()
        if let errors {
            var bytes = Data()
            while let chunk = try errors.fileHandleForReading.read(upToCount: 4096), !chunk.isEmpty {
                bytes.append(chunk)
                bytes = bytes.suffix(1600)
            }
            process.waitUntilExit()
            guard process.terminationStatus == 0 else {
                let detail = String(decoding: bytes, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
                throw Failure(message: detail.isEmpty ? "Quill couldn’t prepare the update. Try again." : detail)
            }
        }
    }

    static func takeResult() -> InstallResult? {
        guard Bundle.main.bundleURL.standardizedFileURL.path == "/Applications/Quill.app" else { return nil }
        let path = FileManager.default.homeDirectoryForCurrentUser.appending(path: ".local/state/quill/app-updates/result.json")
        guard let values = try? path.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]),
              values.isRegularFile == true, values.isSymbolicLink == false, (values.fileSize ?? Int.max) <= 8192,
              let data = try? Data(contentsOf: path),
              let result = try? JSONDecoder().decode(InstallResult.self, from: data) else { return nil }
        try? FileManager.default.removeItem(at: path)
        return result
    }
}
