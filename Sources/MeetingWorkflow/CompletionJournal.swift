import CryptoKit
import Darwin
import Foundation

/// Durable handoff records for sessions explicitly registered on Stop. This
/// journal never discovers recordings by scanning a recordings directory.
public struct CompletionJournal: Sendable {
    public struct Job: Codable, Sendable {
        public enum State: String, Codable, Sendable {
            case waitingForTranscript, ready, running, retry, delivered, failed
        }
        public let id: String
        public let session: URL
        public let command: String
        public let maxAttempts: Int
        public let created: TimeInterval
        public var state: State
        public var attempts: Int
        public var nextAttempt: TimeInterval
        public var attemptedAt: TimeInterval?
        public var processID: Int32?
        public var error: String?
    }

    public let directory: URL

    public init(directory: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".local/state/quill/completion-hooks")) {
        self.directory = directory
    }

    public func register(session: URL, command: String, waitingForTranscript: Bool,
                         maxAttempts: Int = 1, now: TimeInterval = Date().timeIntervalSince1970) throws -> String {
        try RecordingReviewGate.requireProcessingAllowed(session: session)
        let identity = session.standardizedFileURL.path + "\0" + command
        let id = SHA256.hash(data: Data(identity.utf8)).map { String(format: "%02x", $0) }.joined()
        return try locked {
            if FileManager.default.fileExists(atPath: path(id).path) {
                let previous = try read(id)
                guard previous.session == session.standardizedFileURL, previous.command == command else {
                    throw JournalError.invalidRecord
                }
                return id
            }
            let job = Job(id: id, session: session.standardizedFileURL, command: command,
                          maxAttempts: min(6, max(1, maxAttempts)), created: now,
                          state: waitingForTranscript ? .waitingForTranscript : .ready,
                          attempts: 0, nextAttempt: now)
            try write(job)
            return id
        }
    }

    public func jobs() throws -> [Job] {
        try locked { try allJobs() }
    }

    public func ready(_ id: String, now: TimeInterval = Date().timeIntervalSince1970) throws {
        try locked {
            var job = try read(id)
            guard job.state == .waitingForTranscript,
                  !RecordingReviewGate.blocksProcessing(session: job.session) else { return }
            job.state = .ready
            job.nextAttempt = now
            try write(job)
        }
    }

    /// Claim and persist before launching a child. Multiple Quill instances
    /// cannot claim the same job concurrently.
    public func claim(now: TimeInterval = Date().timeIntervalSince1970) throws -> Job? {
        try locked {
            let jobs = try allJobs().sorted { $0.created < $1.created }
            for var job in jobs where [.ready, .retry].contains(job.state) && job.nextAttempt <= now {
                guard !RecordingReviewGate.blocksProcessing(session: job.session) else { continue }
                if job.attempts >= job.maxAttempts {
                    job.state = .failed
                    job.error = "Completion hook exhausted its attempts"
                    try write(job)
                    continue
                }
                job.state = .running
                job.attempts += 1
                job.attemptedAt = now
                job.processID = nil
                try write(job)
                return job
            }
            return nil
        }
    }

    /// Recheck immediately before launching a claimed hook. A hold added after
    /// claim returns the job to the queue without spending a retry attempt.
    public func deferIfHeld(_ id: String) throws -> Bool {
        try locked {
            var job = try read(id)
            guard job.state == .running, job.processID == nil,
                  RecordingReviewGate.blocksProcessing(session: job.session) else { return false }
            job.state = .ready
            job.attempts = max(0, job.attempts - 1)
            job.attemptedAt = nil
            try write(job)
            return true
        }
    }

    /// A delete lease may win after claim but before launch. Put that unstarted
    /// job back without consuming an attempt; backoff prevents a busy spin.
    public func deferUnlaunched(_ id: String, now: TimeInterval = Date().timeIntervalSince1970,
                                delay: TimeInterval = 2) throws {
        try locked {
            var job = try read(id)
            guard job.state == .running, job.processID == nil else { return }
            job.state = .ready
            job.attempts = max(0, job.attempts - 1)
            job.attemptedAt = nil
            job.nextAttempt = now + max(0.25, delay)
            try write(job)
        }
    }

    public func launched(_ id: String, processID: Int32) throws {
        try locked {
            var job = try read(id)
            guard job.state == .running else { return }
            job.processID = processID
            try write(job)
        }
    }

    public func finish(_ id: String, exitCode: Int32, now: TimeInterval = Date().timeIntervalSince1970,
                       retryDelay: TimeInterval? = nil) throws {
        try locked {
            var job = try read(id)
            guard job.state == .running else { return }
            job.processID = nil
            if exitCode == 0 {
                job.state = .delivered
                job.error = nil
            } else {
                job.state = job.attempts >= job.maxAttempts ? .failed : .retry
                job.error = "Completion hook exited with status \(exitCode)"
                job.nextAttempt = now + (retryDelay ?? min(900, 10 * pow(2, Double(job.attempts - 1))))
            }
            try write(job)
        }
    }

    /// A live child is a real wait, not permission to launch a duplicate. Never
    /// signal a recovered PID: PID reuse must not affect unrelated processes.
    public func recover(now: TimeInterval = Date().timeIntervalSince1970,
                        isAlive: (Int32) -> Bool = { kill($0, 0) == 0 || errno == EPERM }) throws {
        try locked {
            for var job in try allJobs() where job.state == .running {
                if let pid = job.processID, isAlive(pid) { continue }
                // Allow a concurrently launching process time to persist PID.
                if job.processID == nil, now - (job.attemptedAt ?? now) < 5 { continue }
                job.processID = nil
                job.state = job.attempts >= job.maxAttempts ? .failed : .retry
                job.nextAttempt = now
                job.error = "Quill was interrupted before the hook result was saved"
                try write(job)
            }
        }
    }

    public enum JournalError: Error { case lockUnavailable, invalidRecord }

    private func path(_ id: String) -> URL { directory.appendingPathComponent(id + ".json") }

    private func read(_ id: String) throws -> Job {
        guard id.count == 64, id.allSatisfy({ $0.isHexDigit }) else { throw JournalError.invalidRecord }
        let bytes = try Data(contentsOf: path(id))
        guard bytes.count < 100_000 else { throw JournalError.invalidRecord }
        let job = try JSONDecoder().decode(Job.self, from: bytes)
        guard job.id == id, job.session.isFileURL, !job.command.isEmpty else { throw JournalError.invalidRecord }
        return job
    }

    private func allJobs() throws -> [Job] {
        let files = try FileManager.default.contentsOfDirectory(at: directory,
            includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey])
        return try files.filter { $0.pathExtension == "json" }.map { file in
            let values = try file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
            guard values.isRegularFile == true, values.isSymbolicLink != true else { throw JournalError.invalidRecord }
            return try read(file.deletingPathExtension().lastPathComponent)
        }
    }

    private func write(_ job: Job) throws {
        let bytes = try JSONEncoder().encode(job)
        try bytes.write(to: path(job.id), options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path(job.id).path)
    }

    private func locked<T>(_ body: () throws -> T) throws -> T {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                               attributes: [.posixPermissions: 0o700])
        let fd = open(directory.appendingPathComponent("journal.lock").path, O_RDWR | O_CREAT | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw JournalError.lockUnavailable }
        defer { close(fd) }
        guard flock(fd, LOCK_EX) == 0 else { throw JournalError.lockUnavailable }
        defer { flock(fd, LOCK_UN) }
        return try body()
    }
}
