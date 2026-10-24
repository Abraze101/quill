import Darwin
import Foundation
import CoreFoundation

/// Persistent, session-local authorization for all post-recording work. Old
/// recordings without this journal retain their existing processing policy.
/// Invalid journals and unfinished new recordings always fail closed.
public enum RecordingReviewGate {
    public static let filename = "recording-safety.json"
    public static let deletionFilename = ".quill-deleted.json"
    public static let processingLockFilename = ".meeting-files.lock"
    public static let processingLeaseMarkerFilename = ".meeting-files-v1.json"
    public enum Owner: String, Codable, Sendable { case manual, automatic }
    public enum Phase: String, Codable, Sendable { case active, finished }
    public enum ReviewStatus: String, Codable, Sendable {
        case pending, approved
        case keptLocal = "kept_local"
    }
    public enum GateError: Error { case invalidRecord, identityMismatch, unfinishedMedia, processingHeld }
    public enum LeaseError: Error { case busy, unavailable, deleted }

    /// A shared filesystem lease prevents deletion until this owner finishes
    /// all session reads/writes. It is acquired without waiting on a UI action.
    public final class ProcessingLease {
        private let descriptor: Int32
        fileprivate init(descriptor: Int32) { self.descriptor = descriptor }
        deinit { flock(descriptor, LOCK_UN); close(descriptor) }
    }

    public static func acquireProcessingLease(session: URL) throws -> ProcessingLease {
        try acquireProcessingLease(session: session, requiresApproval: true)
    }

    /// Advertise the current app's lease support for a completed managed
    /// session, including one kept local or held for review. This does not
    /// approve transcription, enqueue a hook, or change the safety decision.
    public static func registerCompletedSessionLease(session: URL) throws {
        let lease = try acquireProcessingLease(session: session, requiresApproval: false)
        withExtendedLifetime(lease) {}
    }

    public static func registerCompletedSessionLeases(root: URL) {
        guard root.isFileURL,
              let sessions = try? FileManager.default.contentsOfDirectory(at: root,
                includingPropertiesForKeys: [.isDirectoryKey, .isSymbolicLinkKey]) else { return }
        for session in sessions {
            guard let values = try? session.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]),
                  values.isDirectory == true, values.isSymbolicLink != true else { continue }
            try? registerCompletedSessionLease(session: session)
        }
    }

    private static func acquireProcessingLease(session: URL, requiresApproval: Bool) throws -> ProcessingLease {
        guard session.isFileURL else { throw LeaseError.unavailable }
        if !requiresApproval { try requireCompletedManagedSession(session) }
        let directory = open(session.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard directory >= 0 else { throw LeaseError.unavailable }
        defer { close(directory) }
        var directoryInfo = stat()
        guard fstat(directory, &directoryInfo) == 0,
              directoryInfo.st_mode & S_IFMT == S_IFDIR else { throw LeaseError.unavailable }
        guard !hasDeletionMarker(directory: directory) else { throw LeaseError.deleted }
        let descriptor = openat(directory, processingLockFilename, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, 0o600)
        guard descriptor >= 0 else { throw LeaseError.unavailable }
        var transferred = false
        defer { if !transferred { close(descriptor) } }
        var fileInfo = stat()
        guard fstat(descriptor, &fileInfo) == 0, fileInfo.st_mode & S_IFMT == S_IFREG,
              fileInfo.st_nlink == 1, fileInfo.st_uid == getuid(), fileInfo.st_mode & 0o077 == 0 else {
            throw LeaseError.unavailable
        }
        guard flock(descriptor, LOCK_SH | LOCK_NB) == 0 else {
            if errno == EWOULDBLOCK || errno == EAGAIN { throw LeaseError.busy }
            throw LeaseError.unavailable
        }
        var currentDirectory = stat()
        var currentFile = stat()
        guard lstat(session.path, &currentDirectory) == 0,
              currentDirectory.st_mode & S_IFMT == S_IFDIR,
              currentDirectory.st_dev == directoryInfo.st_dev, currentDirectory.st_ino == directoryInfo.st_ino,
              fstatat(directory, processingLockFilename, &currentFile, AT_SYMLINK_NOFOLLOW) == 0,
              currentFile.st_dev == fileInfo.st_dev, currentFile.st_ino == fileInfo.st_ino else {
            throw LeaseError.unavailable
        }
        guard !hasDeletionMarker(directory: directory) else { throw LeaseError.deleted }
        if requiresApproval { try requireProcessingAllowed(session: session) }
        else { try requireCompletedManagedSession(session) }
        try ensureProcessingLeaseMarker(directory: directory)
        transferred = true
        return ProcessingLease(descriptor: descriptor)
    }

    private static func requireCompletedManagedSession(_ session: URL) throws {
        guard let record = try read(session: session), record.phase == .finished,
              hasFinalizedMetadata(session: session) else { throw GateError.unfinishedMedia }
    }

    /// A capability marker is published only by a caller already protected by
    /// the shared lease. linkat publishes atomically without replacing a node
    /// created by another lease holder (or following an existing symlink).
    private static func ensureProcessingLeaseMarker(directory: Int32) throws {
        if try hasValidProcessingLeaseMarker(directory: directory) { return }
        let temporary = ".meeting-files-v1-\(UUID().uuidString).tmp"
        let fd = openat(directory, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw LeaseError.unavailable }
        defer { close(fd); unlinkat(directory, temporary, 0) }
        let payload = Data("{\"schema_version\":1,\"shared_processing_lease\":true}\n".utf8)
        try payload.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                let count = Darwin.write(fd, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                if count < 0, errno == EINTR { continue }
                guard count > 0 else { throw LeaseError.unavailable }
                offset += count
            }
        }
        guard fsync(fd) == 0 else { throw LeaseError.unavailable }
        if linkat(directory, temporary, directory, processingLeaseMarkerFilename, 0) != 0,
           errno != EEXIST { throw LeaseError.unavailable }
        // Remove our second name before validating the final published node.
        unlinkat(directory, temporary, 0)
        guard try hasValidProcessingLeaseMarker(directory: directory) else { throw LeaseError.unavailable }
    }

    /// Missing markers are legacy sessions; every other malformed or unsafe
    /// node blocks processing rather than claiming shared-lease protection.
    private static func hasValidProcessingLeaseMarker(directory: Int32) throws -> Bool {
        let fd = openat(directory, processingLeaseMarkerFilename, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
        guard fd >= 0 else {
            if errno == ENOENT { return false }
            throw LeaseError.unavailable
        }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_size > 0, info.st_size <= 1_000 else { throw LeaseError.unavailable }
        let data = try FileHandle(fileDescriptor: fd, closeOnDealloc: false).read(upToCount: 1_001) ?? Data()
        var current = stat()
        guard data.count <= 1_000,
              fstatat(directory, processingLeaseMarkerFilename, &current, AT_SYMLINK_NOFOLLOW) == 0,
              current.st_mode & S_IFMT == S_IFREG, current.st_dev == info.st_dev, current.st_ino == info.st_ino,
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == Set(["schema_version", "shared_processing_lease"]),
              let version = object["schema_version"] as? NSNumber,
              CFGetTypeID(version) != CFBooleanGetTypeID(),
              !["f", "d"].contains(String(cString: version.objCType)), version.intValue == 1,
              let shared = object["shared_processing_lease"] as? NSNumber,
              CFGetTypeID(shared) == CFBooleanGetTypeID(), shared.boolValue else { throw LeaseError.unavailable }
        return true
    }

    private static func hasDeletionMarker(directory: Int32) -> Bool {
        var info = stat()
        if fstatat(directory, deletionFilename, &info, AT_SYMLINK_NOFOLLOW) == 0 { return true }
        return errno != ENOENT
    }

    private static func sessionIsAvailable(_ session: URL) -> Bool {
        guard session.isFileURL else { return false }
        let directory = open(session.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard directory >= 0 else { return false }
        defer { close(directory) }
        return !hasDeletionMarker(directory: directory)
            && (try? hasValidProcessingLeaseMarker(directory: directory)) != nil
    }

    public struct Record: Codable, Equatable, Sendable {
        public var schemaVersion: Int = 1
        public let sessionID: UUID
        public let owner: Owner
        public var phase: Phase
        public let startedAt: Date
        public var checkInAt: Date
        public var stopAt: Date
        public var warningRevision: Int
        public var reviewStatus: ReviewStatus
        public var reasons: [String]

        enum CodingKeys: String, CodingKey {
            case schemaVersion = "schema_version", sessionID = "session_id", owner, phase
            case startedAt = "started_at", checkInAt = "check_in_at", stopAt = "stop_at"
            case warningRevision = "warning_revision", reviewStatus = "review_status", reasons
        }

        public init(sessionID: UUID, owner: Owner, phase: Phase = .active,
                    startedAt: Date, checkInAt: Date, stopAt: Date, warningRevision: Int = 1,
                    reviewStatus: ReviewStatus = .pending, reasons: [String] = []) {
            self.sessionID = sessionID
            self.owner = owner
            self.phase = phase
            self.startedAt = Date(timeIntervalSince1970: floor(startedAt.timeIntervalSince1970))
            self.checkInAt = Date(timeIntervalSince1970: floor(checkInAt.timeIntervalSince1970))
            self.stopAt = Date(timeIntervalSince1970: floor(stopAt.timeIntervalSince1970))
            self.warningRevision = warningRevision
            self.reviewStatus = reviewStatus
            self.reasons = reasons
        }
    }

    public static func blocksProcessing(session: URL) -> Bool {
        guard sessionIsAvailable(session) else { return true }
        do {
            guard let record = try read(session: session) else { return false }
            return record.phase != .finished || record.reviewStatus != .approved
                || !hasFinalizedMetadata(session: session)
        } catch { return true }
    }

    public static func requireProcessingAllowed(session: URL) throws {
        guard !blocksProcessing(session: session) else { throw GateError.processingHeld }
    }

    public static func read(session: URL) throws -> Record? {
        guard session.isFileURL else { throw GateError.invalidRecord }
        let file = session.appendingPathComponent(filename)
        let attributes: [FileAttributeKey: Any]
        do { attributes = try FileManager.default.attributesOfItem(atPath: file.path) }
        catch let error as NSError where error.domain == NSCocoaErrorDomain && error.code == NSFileReadNoSuchFileError {
            return nil
        }
        guard attributes[.type] as? FileAttributeType == .typeRegular,
              let size = attributes[.size] as? NSNumber, size.intValue <= 100_000 else {
            throw GateError.invalidRecord
        }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let record = try decoder.decode(Record.self, from: Data(contentsOf: file))
        try validate(record)
        return record
    }

    /// Initial persistence and deadline updates preserve accumulated failure
    /// reasons and review decisions. An active update can never reopen a finished
    /// journal, replace its identity, or silently release a hold.
    public static func write(_ record: Record, session: URL) throws {
        try locked(session: session) {
            var updated = record
            if let previous = try read(session: session) {
                guard previous.sessionID == record.sessionID, previous.owner == record.owner,
                      previous.startedAt == record.startedAt else { throw GateError.identityMismatch }
                guard previous.phase == .active, record.phase == .active,
                      record.warningRevision >= previous.warningRevision else { throw GateError.invalidRecord }
                updated.reasons = Array(Set(previous.reasons + record.reasons)).sorted()
                updated.reviewStatus = previous.reviewStatus
            }
            try save(updated, session: session)
        }
    }

    public static func hold(session: URL, sessionID: UUID, reason: String) throws {
        try locked(session: session) {
            guard var record = try read(session: session), record.sessionID == sessionID else {
                throw GateError.identityMismatch
            }
            record.reasons = Array(Set(record.reasons + [reason])).sorted()
            if record.reviewStatus != .keptLocal { record.reviewStatus = .pending }
            try save(record, session: session)
        }
    }

    /// Call only after capture has stopped. Existing reasons are never erased;
    /// retries of finish cannot turn a held or reviewed session into approval.
    public static func finish(session: URL, sessionID: UUID, reasons: [String] = []) throws {
        try locked(session: session) {
            guard var record = try read(session: session), record.sessionID == sessionID else {
                throw GateError.identityMismatch
            }
            let wasActive = record.phase == .active
            record.phase = .finished
            record.reasons = Array(Set(record.reasons + reasons)).sorted()
            if record.reviewStatus != .keptLocal {
                if !reasons.isEmpty || (wasActive && !record.reasons.isEmpty) { record.reviewStatus = .pending }
                else if wasActive { record.reviewStatus = .approved }
            }
            try save(record, session: session)
        }
        try? registerCompletedSessionLease(session: session)
    }

    /// Approval requires finalized metadata. Interrupted capture with no meta
    /// cannot enter transcription; Keep local is always available to dismiss it.
    public static func decide(session: URL, sessionID: UUID, status: ReviewStatus) throws {
        guard status != .pending else { throw GateError.invalidRecord }
        try locked(session: session) {
            guard var record = try read(session: session), record.sessionID == sessionID else {
                throw GateError.identityMismatch
            }
            if status == .approved {
                guard hasFinalizedMetadata(session: session) else { throw GateError.unfinishedMedia }
                record.phase = .finished
            }
            record.reviewStatus = status
            try save(record, session: session)
        }
    }

    public static func hasFinalizedMetadata(session: URL) -> Bool {
        let file = session.appendingPathComponent("meta.json")
        guard let values = try? file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]),
              values.isRegularFile == true, values.isSymbolicLink != true,
              let size = values.fileSize, size <= 100_000,
              let data = try? Data(contentsOf: file),
              let metadata = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let started = metadata["started"] as? String,
              let ended = metadata["ended"] as? String,
              ISO8601DateFormatter().date(from: started) != nil,
              ISO8601DateFormatter().date(from: ended) != nil,
              let files = metadata["files"] as? [String: String],
              files["mic"] == "mic.caf", files["system"] == "system.caf" else { return false }
        return true
    }

    /// New session journals only: no opt-in discovery of legacy recordings.
    /// Malformed journals remain visible for local recovery, never processing.
    public static func pendingSessions(root: URL) -> [URL] {
        guard root.isFileURL,
              let entries = try? FileManager.default.contentsOfDirectory(at: root,
                  includingPropertiesForKeys: [.isDirectoryKey, .isSymbolicLinkKey]) else { return [] }
        return entries.filter { session in
            guard sessionIsAvailable(session) else { return false }
            guard let values = try? session.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]),
                  values.isDirectory == true, values.isSymbolicLink != true else { return false }
            do {
                guard let record = try read(session: session) else { return false }
                return record.reviewStatus == .pending
            } catch { return true }
        }.sorted { $0.lastPathComponent > $1.lastPathComponent }
    }

    private static func validate(_ record: Record) throws {
        guard record.schemaVersion == 1, record.warningRevision >= 1,
              record.startedAt.timeIntervalSince1970.isFinite,
              record.checkInAt >= record.startedAt, record.stopAt > record.checkInAt,
              record.reasons.count <= 100,
              record.reasons.allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && $0.utf8.count <= 2_000 }),
              record.phase != .active || record.reviewStatus != .approved else { throw GateError.invalidRecord }
    }

    private static func save(_ record: Record, session: URL) throws {
        try validate(record)
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let file = session.appendingPathComponent(filename)
        try encoder.encode(record).write(to: file, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
    }

    private static func locked<T>(session: URL, _ body: () throws -> T) throws -> T {
        guard session.isFileURL,
              let values = try? session.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]),
              values.isDirectory == true, values.isSymbolicLink != true else { throw GateError.invalidRecord }
        let lock = session.appendingPathComponent(".recording-safety.lock")
        let fd = open(lock.path, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw GateError.invalidRecord }
        defer { close(fd) }
        guard flock(fd, LOCK_EX) == 0 else { throw GateError.invalidRecord }
        defer { flock(fd, LOCK_UN) }
        return try body()
    }
}
