import Darwin
import Foundation

/// Held by `Run` for its entire lifetime, across installed and preview copies.
final class RunInstanceLock {
    enum Failure: LocalizedError {
        case alreadyRunning
        case unsafeLock
        case system(operation: String, code: Int32)

        var errorDescription: String? {
            switch self {
            case .alreadyRunning:
                return "Quill is already running. Use its menu-bar controls."
            case .unsafeLock:
                return "Quill's recording lock is not a private file owned by this user."
            case let .system(operation, code):
                return "Quill could not \(operation): \(String(cString: strerror(code)))."
            }
        }
    }

    static var defaultURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/state/quill/run.lock")
    }

    private let descriptor: Int32

    init(url: URL = RunInstanceLock.defaultURL) throws {
        let parent = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(
            at: parent, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let directory = open(parent.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard directory >= 0 else { throw Failure.system(operation: "open its recording lock directory", code: errno) }
        defer { close(directory) }
        var directoryInfo = stat()
        guard fstat(directory, &directoryInfo) == 0 else {
            throw Failure.system(operation: "inspect its recording lock directory", code: errno)
        }
        guard directoryInfo.st_uid == getuid(), directoryInfo.st_mode & S_IFMT == S_IFDIR else {
            throw Failure.unsafeLock
        }
        guard fchmod(directory, 0o700) == 0 else {
            throw Failure.system(operation: "protect its recording lock directory", code: errno)
        }

        // NONBLOCK also makes a substituted FIFO safe to inspect and reject.
        let file = openat(directory, url.lastPathComponent,
                          O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, 0o600)
        guard file >= 0 else { throw Failure.system(operation: "open its recording lock", code: errno) }
        var acquired = false
        defer { if !acquired { close(file) } }
        var info = stat()
        guard fstat(file, &info) == 0 else {
            throw Failure.system(operation: "inspect its recording lock", code: errno)
        }
        guard info.st_uid == getuid(), info.st_mode & S_IFMT == S_IFREG, info.st_nlink == 1 else {
            throw Failure.unsafeLock
        }
        guard fchmod(file, 0o600) == 0 else {
            throw Failure.system(operation: "protect its recording lock", code: errno)
        }
        var result: Int32
        repeat { result = flock(file, LOCK_EX | LOCK_NB) } while result != 0 && errno == EINTR
        guard result == 0 else {
            let code = errno
            if code == EWOULDBLOCK || code == EAGAIN { throw Failure.alreadyRunning }
            throw Failure.system(operation: "acquire its recording lock", code: code)
        }
        descriptor = file
        acquired = true
    }

    deinit {
        // Keep the inode in place; unlinking could let another process lock a new file.
        close(descriptor)
    }
}
