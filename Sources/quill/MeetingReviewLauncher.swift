import Darwin
import Foundation

/// A browser tab can close while its review server is still serving the same URL.
@MainActor
final class MeetingReviewLauncher {
    private struct Session {
        let command: [String]
        let child: Process
        let managed: Bool
        var url: URL?
    }

    private var session: Session?
    private let openURL: (URL) -> Bool
    private let reportError: (String) -> Void

    init(openURL: @escaping (URL) -> Bool, reportError: @escaping (String) -> Void) {
        self.openURL = openURL
        self.reportError = reportError
    }

    func open(_ command: [String]) {
        guard let executable = command.first else { return }
        if let active = session, active.child.isRunning, active.command == command {
            if let url = active.url {
                openPage(url)
            } else if !active.managed {
                reportError("Your configured meeting review command is still running.")
            }
            // A click during startup joins the pending open.
            return
        }
        close()

        let managed = Self.isWorkflowReview(command)
        let child = Process()
        child.executableURL = URL(fileURLWithPath: executable)
        var arguments = Array(command.dropFirst())
        if managed {
            for flag in ["--no-open", "--print-url"] where !arguments.contains(flag) {
                arguments.append(flag)
            }
        }
        child.arguments = arguments
        child.standardInput = FileHandle.nullDevice
        child.standardError = FileHandle.nullDevice
        let output = managed ? Pipe() : nil
        if let output { child.standardOutput = output } else { child.standardOutput = FileHandle.nullDevice }
        do {
            try child.run()
        } catch {
            reportError("The meeting review command could not start.")
            return
        }

        session = Session(command: command, child: child, managed: managed)
        if let output {
            let reader = output.fileHandleForReading
            Task.detached(priority: .userInitiated) { [weak self] in
                let address = Self.readAddress(reader)
                await self?.ready(address, from: child)
            }
        }
    }

    func close() {
        let previous = session
        session = nil
        if let previous, previous.managed, previous.child.isRunning {
            previous.child.terminate()
        }
    }

    private func ready(_ address: URL?, from child: Process) {
        guard session?.child === child else { return }
        guard let address, child.isRunning else {
            close()
            reportError("The meeting review page could not start. Try opening it again.")
            return
        }
        session?.url = address
        openPage(address)
    }

    private func openPage(_ url: URL) {
        if !openURL(url) { reportError("Your browser could not open the meeting review page. Try again.") }
    }

    private static func isWorkflowReview(_ command: [String]) -> Bool {
        // Only the shipped CLI has this handshake. Wrappers, alternate scripts
        // and other subcommands keep ownership of their arguments and browser.
        var arguments = command[...]
        guard let program = arguments.first else { return false }
        let executable = URL(fileURLWithPath: program).lastPathComponent
        if executable.wholeMatch(of: #/python(?:[0-9]+(?:\.[0-9]+)*)?/#) != nil {
            arguments = arguments.dropFirst()
        }
        guard let script = arguments.first, script.hasPrefix("/"),
              script.hasSuffix("/integrations/meeting-workflow/workflow.py") else { return false }
        arguments = arguments.dropFirst()
        while let next = arguments.first {
            if next == "--config" || next == "--state" {
                arguments = arguments.dropFirst(2)
            } else if next.hasPrefix("--config=") || next.hasPrefix("--state=") {
                arguments = arguments.dropFirst()
            } else {
                return next == "review"
            }
        }
        return false
    }

    private nonisolated static func readAddress(_ handle: FileHandle) -> URL? {
        defer { try? handle.close() }
        let deadline = ProcessInfo.processInfo.systemUptime + 15
        var input = pollfd(fd: handle.fileDescriptor, events: Int16(POLLIN), revents: 0)
        var bytes = [UInt8]()
        var buffer = [UInt8](repeating: 0, count: 128)
        while bytes.count < buffer.count {
            let remaining = Int32(max(0, (deadline - ProcessInfo.processInfo.systemUptime) * 1000))
            guard remaining > 0 else { return nil }
            let available = poll(&input, 1, remaining)
            if available < 0 && errno == EINTR { continue }
            guard available > 0 else { return nil }
            let count = read(input.fd, &buffer, buffer.count - bytes.count)
            if count < 0 && errno == EINTR { continue }
            guard count > 0 else { return nil }
            let chunk = buffer.prefix(count)
            if let end = chunk.firstIndex(of: 10) {
                bytes.append(contentsOf: chunk[..<end])
                // Exact output of create_server(): no user info, escaping, query, or fragment.
                guard let text = String(bytes: bytes, encoding: .ascii),
                      let match = text.wholeMatch(of: #/http://127\.0\.0\.1:([1-9][0-9]{0,4})/[A-Za-z0-9_-]{43}//#),
                      let port = UInt16(match.1), port > 0 else { return nil }
                return URL(string: text)
            }
            bytes.append(contentsOf: chunk)
        }
        return nil
    }
}
