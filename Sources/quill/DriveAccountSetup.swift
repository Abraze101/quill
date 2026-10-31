import Foundation
import Darwin

struct DriveAccountCatalog: Decodable {
    let accounts: [DriveAccountChoice]
    let revision: String
}

/// Only runs the account setup helper. Capture and processing have separate lifetimes.
@MainActor
final class DriveAccountSetup {
    private var process: Process?
    private var completion: CheckedContinuation<Data, Error>?
    private var deadline: Task<Void, Never>?
    private var outputURL: URL?
    private var timedOut = false

    func accounts() async throws -> DriveAccountCatalog {
        let data = try await run(["drive-accounts"])
        let catalog = try JSONDecoder().decode(DriveAccountCatalog.self, from: data)
        guard catalog.accounts.count <= 100, catalog.revision.count == 64,
              Set(catalog.accounts.map(\.linkID)).count == catalog.accounts.count,
              catalog.accounts.allSatisfy({ $0.linkID.hasPrefix("link_") && !$0.email.isEmpty
                  && $0.email.utf8.count <= 320 && !$0.email.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains) }) else {
            throw SetupError("The connected account list could not be verified. Try refreshing it.")
        }
        return catalog
    }

    func select(_ account: DriveAccountChoice, revision: String) async throws {
        _ = try await run(["drive-select", "--link-id", account.linkID, "--revision", revision,
                          "--profile-id", account.profileID, "--email", account.email])
    }

    private func run(_ arguments: [String]) async throws -> Data {
        guard process == nil, let command = Config.driveAccountCommand(arguments) else {
            throw SetupError("Configure the meeting workflow before choosing a Drive account. Open Configuration to review setup.")
        }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("quill-drive-setup-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                                               attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let output = directory.appendingPathComponent("result.json")
        FileManager.default.createFile(atPath: output.path, contents: nil, attributes: [.posixPermissions: 0o600])
        let handle = try FileHandle(forWritingTo: output)
        defer { try? handle.close() }
        timedOut = false
        outputURL = output
        return try await withCheckedThrowingContinuation { continuation in
            completion = continuation
            let child = Process()
            child.executableURL = URL(fileURLWithPath: command[0])
            child.arguments = Array(command.dropFirst())
            child.standardInput = FileHandle.nullDevice
            child.standardOutput = handle
            child.standardError = FileHandle.nullDevice
            child.terminationHandler = { [weak self] child in
                let status = child.terminationStatus
                Task { @MainActor in self?.finish(status: status) }
            }
            process = child
            do {
                try child.run()
                deadline = Task { [weak self] in
                    do { try await Task.sleep(for: .seconds(240)) } catch { return }
                    guard let self, let child = self.process, child.isRunning else { return }
                    self.timedOut = true
                    child.terminate()
                    do { try await Task.sleep(for: .seconds(5)) } catch { return }
                    if child.isRunning { kill(child.processIdentifier, SIGKILL) }
                }
            } catch {
                process = nil
                completion = nil
                continuation.resume(throwing: SetupError("The Google Drive setup helper could not start."))
            }
        }
    }

    private func finish(status: Int32) {
        guard let completion else { return }
        self.completion = nil
        process = nil
        deadline?.cancel()
        deadline = nil
        do {
            guard !timedOut else { throw SetupError("Checking Google Drive took too long. Check the account shown in Settings, then refresh before trying again.") }
            guard let outputURL,
                  let size = try outputURL.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= 100_000 else {
                throw SetupError("The Google Drive setup response could not be read.")
            }
            let data = try Data(contentsOf: outputURL)
            guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw SetupError("Google Drive did not return a valid setup response.")
            }
            if let error = value["error"] as? String {
                throw SetupError(String(error.prefix(1000)))
            }
            guard status == 0 else { throw SetupError("Google Drive setup did not finish. Refresh and try again.") }
            completion.resume(returning: data)
        } catch { completion.resume(throwing: error) }
    }

    struct SetupError: LocalizedError {
        let errorDescription: String?
        init(_ message: String) { errorDescription = message }
    }
}
