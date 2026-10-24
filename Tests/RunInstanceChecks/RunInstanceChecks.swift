import Darwin
import Foundation

@main
enum RunInstanceChecks {
    static func main() throws {
        if CommandLine.arguments.count > 2 {
            try child(mode: CommandLine.arguments[1], url: URL(fileURLWithPath: CommandLine.arguments[2]))
            return
        }

        let fm = FileManager.default
        let root = fm.temporaryDirectory.appendingPathComponent("quill-instance-\(UUID().uuidString)")
        try fm.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        defer { try? fm.removeItem(at: root) }
        let lockURL = root.appendingPathComponent("state/run.lock")
        let owner = try RunInstanceLock(url: lockURL)
        var info = stat()
        precondition(lstat(lockURL.path, &info) == 0 && info.st_mode & 0o777 == 0o600)
        let originalInode = info.st_ino
        try withExtendedLifetime(owner) {
            try expectAlreadyRunning(lockURL)
            let duplicateStatus = try runChild("try", lockURL)
            precondition(duplicateStatus == 23, "A second process must be rejected")
        }
        // Test scope below, rather than an explicit unlock API, exercises lifetime release.
        try testRelease(root.appendingPathComponent("release/run.lock"))

        let processURL = root.appendingPathComponent("process/run.lock")
        let readiness = Pipe()
        let input = Pipe()
        let process = childProcess("hold", processURL)
        process.standardOutput = readiness
        process.standardInput = input
        try process.run()
        defer { if process.isRunning { process.terminate(); process.waitUntilExit() } }
        precondition(readiness.fileHandleForReading.readData(ofLength: 1) == Data([1]), "Child must acquire lock")
        try expectAlreadyRunning(processURL)
        process.terminate()
        process.waitUntilExit()
        let afterExit = try RunInstanceLock(url: processURL)
        withExtendedLifetime(afterExit) {}

        let target = root.appendingPathComponent("untouched.txt")
        let original = Data("unchanged".utf8)
        try original.write(to: target)
        let symlink = root.appendingPathComponent("linked.lock")
        try fm.createSymbolicLink(at: symlink, withDestinationURL: target)
        try expectRejected(symlink)
        let targetContents = try Data(contentsOf: target)
        precondition(targetContents == original, "Symlink target must remain untouched")

        let fifo = root.appendingPathComponent("fifo.lock")
        precondition(mkfifo(fifo.path, 0o600) == 0)
        try expectRejected(fifo)
        let directory = root.appendingPathComponent("directory.lock")
        try fm.createDirectory(at: directory, withIntermediateDirectories: false)
        try expectRejected(directory)
        let hardlink = root.appendingPathComponent("hardlinked.lock")
        precondition(link(target.path, hardlink.path) == 0)
        try expectRejected(hardlink)

        precondition(lstat(lockURL.path, &info) == 0 && info.st_ino == originalInode,
                     "A rejected acquisition must preserve the lock inode")
        withExtendedLifetime(owner) {}
        print("Run instance: 8 checks passed (private file, same-process exclusion, cross-process exclusion, release, process exit, symlink, nonregular files, hardlink)")
    }

    private static func expectAlreadyRunning(_ url: URL) throws {
        do {
            let lock = try RunInstanceLock(url: url)
            withExtendedLifetime(lock) { preconditionFailure("Duplicate acquisition succeeded") }
        } catch RunInstanceLock.Failure.alreadyRunning { }
    }

    private static func expectRejected(_ url: URL) throws {
        var rejected = false
        do {
            let lock = try RunInstanceLock(url: url)
            withExtendedLifetime(lock) {}
        } catch { rejected = true }
        precondition(rejected, "Unsafe lock path was accepted")
    }

    private static func testRelease(_ url: URL) throws {
        func acquireAndRelease() throws -> ino_t {
            let lock = try RunInstanceLock(url: url)
            var info = stat()
            precondition(lstat(url.path, &info) == 0)
            withExtendedLifetime(lock) {}
            return info.st_ino
        }
        let inode = try acquireAndRelease()
        let reacquired = try RunInstanceLock(url: url)
        var info = stat()
        precondition(lstat(url.path, &info) == 0 && info.st_ino == inode,
                     "Releasing must not unlink the stable lock file")
        withExtendedLifetime(reacquired) {}
    }

    private static func childProcess(_ mode: String, _ url: URL) -> Process {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        process.arguments = [mode, url.path]
        return process
    }

    private static func runChild(_ mode: String, _ url: URL) throws -> Int32 {
        let process = childProcess(mode, url)
        try process.run()
        process.waitUntilExit()
        return process.terminationStatus
    }

    private static func child(mode: String, url: URL) throws {
        do {
            let lock = try RunInstanceLock(url: url)
            if mode == "hold" {
                FileHandle.standardOutput.write(Data([1]))
                _ = FileHandle.standardInput.readData(ofLength: 1)
            }
            withExtendedLifetime(lock) {}
        } catch RunInstanceLock.Failure.alreadyRunning {
            exit(23)
        }
    }
}
