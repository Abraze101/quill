import Darwin
import Foundation

/// An isolated child lets Delete terminate model work, including detached
/// tasks inside FluidAudio. The caller's processing lease outlives child exit.
enum LocalDiarizationProcess {
    enum Failure: Error { case unavailable, invalidOutput, timedOut }
    static let maximumOutputBytes = 20_000_000

    static func run(audio: URL, executable: URL? = nil, timeout: TimeInterval = 1_800) async throws -> [SpeakerInterval] {
        try Task.checkCancellation()
        guard timeout.isFinite, timeout > 0 else { throw Failure.unavailable }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("quill-speakers-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                                                attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let output = directory.appendingPathComponent("speakers.json")
        guard let executable = executable ?? Bundle.main.executableURL else { throw Failure.unavailable }
        let pid = try spawn(executable: executable, arguments: ["diarize-local", "--input", audio.path,
            "--output", output.path, "--parent-pid", String(getpid())])
        var reaped = false
        defer { if !reaped { stop(pid) } }
        let deadline = Date().addingTimeInterval(timeout)
        var status: Int32 = 0
        while true {
            try Task.checkCancellation()
            let exited = waitpid(pid, &status, WNOHANG)
            if exited == pid { reaped = true; break }
            if exited < 0, errno != EINTR { throw Failure.unavailable }
            if Date() >= deadline { throw Failure.timedOut }
            try await Task.sleep(for: .milliseconds(50))
        }
        try Task.checkCancellation()
        guard status & 0x7f == 0, (status >> 8) & 0xff == 0 else { throw Failure.unavailable }
        let fd = open(output.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
        guard fd >= 0 else { throw Failure.invalidOutput }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == getuid(), info.st_nlink == 1,
              info.st_size > 0, info.st_size <= maximumOutputBytes else { throw Failure.invalidOutput }
        let data = try FileHandle(fileDescriptor: fd, closeOnDealloc: false).read(upToCount: maximumOutputBytes + 1) ?? Data()
        guard data.count <= maximumOutputBytes else { throw Failure.invalidOutput }
        let result = try JSONDecoder().decode(LocalDiarizationResult.self, from: data)
        guard result.schema_version == 1, result.segments.count <= 200_000,
              result.segments.allSatisfy({
                !$0.speaker.isEmpty && $0.speaker.utf8.count <= 80
                    && $0.start.isFinite && $0.end.isFinite && $0.quality.isFinite
                    && $0.start >= 0 && $0.end > $0.start && $0.quality >= 0 && $0.quality <= 1
              }) else { throw Failure.invalidOutput }
        return result.segments
    }

    private static func spawn(executable: URL, arguments: [String]) throws -> pid_t {
        var actions: posix_spawn_file_actions_t?
        var attributes: posix_spawnattr_t?
        guard posix_spawn_file_actions_init(&actions) == 0 else { throw Failure.unavailable }
        defer { posix_spawn_file_actions_destroy(&actions) }
        guard posix_spawnattr_init(&attributes) == 0 else { throw Failure.unavailable }
        defer { posix_spawnattr_destroy(&attributes) }
        guard posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP)) == 0,
              posix_spawnattr_setpgroup(&attributes, 0) == 0 else { throw Failure.unavailable }
        for descriptor in [STDIN_FILENO, STDOUT_FILENO, STDERR_FILENO] {
            guard posix_spawn_file_actions_addopen(&actions, descriptor, "/dev/null", O_RDWR, 0) == 0 else {
                throw Failure.unavailable
            }
        }
        let strings = ([executable.path] + arguments).map { strdup($0) }
        defer { for string in strings { free(string) } }
        guard strings.allSatisfy({ $0 != nil }) else { throw Failure.unavailable }
        var argv = strings + [nil]
        let allowedEnvironment = Set(["HOME", "USER", "LOGNAME", "TMPDIR", "PATH", "LANG", "LC_ALL",
            "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy",
            "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"])
        let environment = ProcessInfo.processInfo.environment.filter { allowedEnvironment.contains($0.key) }
            .map { strdup("\($0.key)=\($0.value)") }
        defer { for value in environment { free(value) } }
        guard environment.allSatisfy({ $0 != nil }) else { throw Failure.unavailable }
        var envp = environment + [nil]
        var pid: pid_t = 0
        let code = executable.path.withCString { path in
            argv.withUnsafeMutableBufferPointer { buffer in
                envp.withUnsafeMutableBufferPointer { env in
                    posix_spawn(&pid, path, &actions, &attributes, buffer.baseAddress!, env.baseAddress!)
                }
            }
        }
        guard code == 0, pid > 0 else { throw Failure.unavailable }
        return pid
    }

    private static func stop(_ pid: pid_t) {
        // posix_spawn establishes this group before the child can run. Reap
        // directly; Foundation's waitUntilExit can hang in a Swift async task.
        kill(-pid, SIGTERM)
        let deadline = Date().addingTimeInterval(0.5)
        var status: Int32 = 0
        while Date() < deadline {
            let result = waitpid(pid, &status, WNOHANG)
            if result == pid || result < 0 && errno == ECHILD { return }
            Thread.sleep(forTimeInterval: 0.01)
        }
        kill(-pid, SIGKILL)
        while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
    }
}
