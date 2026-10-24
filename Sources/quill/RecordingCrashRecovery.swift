import AVFoundation
import CoreFoundation
import Darwin
import Foundation
import MeetingWorkflow

/// A capture journal is evidence of an interrupted *new* recording, not an
/// instruction to process it. Recovery runs before workers and keeps the normal
/// review gate closed. Historical folders without this journal are untouched.
enum RecordingCrashRecovery {
    static let journalFilename = "recording-in-progress.json"
    static let captureLockFilename = ".quill-capture.lock"
    static let recoveryReason = "Recording recovered after an unexpected interruption. Review before processing."
    private static let screenRecoveryError = "Screen video was interrupted and could not be finalized. The original file is preserved locally."
    private static let maximumJSONSize = 100_000

    struct Summary: Sendable {
        var recovered = 0
        var skipped = 0
        var failed = 0
    }

    enum RecoveryError: Error { case unavailable, busy, invalidJournal, identityMismatch, missingMedia }

    /// Synchronous, single-owner state. Deinitialization releases the OS locks
    /// but intentionally leaves the journal for recovery after a failed start.
    final class CaptureLease {
        private let directory: Directory
        private let captureLock: FileLock
        private let filesLock: FileLock
        private let sessionID: UUID
        private var journal: Data?

        fileprivate init(directory: Directory, captureLock: FileLock, filesLock: FileLock,
                         sessionID: UUID, journal: Data) {
            self.directory = directory
            self.captureLock = captureLock
            self.filesLock = filesLock
            self.sessionID = sessionID
            self.journal = journal
        }

        func checkpoint(metadata: [String: Any]) throws {
            guard let previous = journal else { throw RecoveryError.unavailable }
            try directory.checkAvailable()
            try directory.requireUnchanged(journalFilename, expected: previous)
            let gate = try readGate(directory)
            guard gate.sessionID == sessionID, gate.phase == .active else { throw RecoveryError.identityMismatch }
            let data = try encodeJournal(sessionID: sessionID, metadata: metadata, gate: gate)
            try directory.publish(data, name: journalFilename, replacing: previous)
            journal = data
            withExtendedLifetime((captureLock, filesLock)) {}
        }

        /// Call only after clean metadata AND RecordingReviewGate.finish have
        /// succeeded. Pending and Keep Local are valid completed decisions.
        func complete() throws {
            guard let previous = journal else { return }
            try directory.checkAvailable()
            let gate = try readGate(directory)
            guard gate.sessionID == sessionID, gate.phase == .finished else { throw RecoveryError.identityMismatch }
            _ = try finalizedMetadata(directory, gate: gate)
            try directory.remove(journalFilename, expected: previous)
            journal = nil
            withExtendedLifetime((captureLock, filesLock)) {}
        }
    }

    static func begin(session: URL, sessionID: UUID, metadata: [String: Any]) throws -> CaptureLease {
        let directory = try Directory(session)
        let capture = try FileLock(directory, name: captureLockFilename, exclusive: true)
        // Capture has not been approved for processing. This raw shared lock
        // only protects its files against deletion through final gate cleanup.
        let files = try FileLock(directory, name: RecordingReviewGate.processingLockFilename, exclusive: false)
        try directory.checkAvailable()
        let gate = try readGate(directory)
        guard gate.sessionID == sessionID, gate.phase == .active else { throw RecoveryError.identityMismatch }
        guard !directory.exists("meta.json"), !directory.exists("transcript.json"),
              !directory.exists(journalFilename) else { throw RecoveryError.invalidJournal }
        let journal = try encodeJournal(sessionID: sessionID, metadata: metadata, gate: gate)
        try directory.publish(journal, name: journalFilename)
        return CaptureLease(directory: directory, captureLock: capture, filesLock: files,
                            sessionID: sessionID, journal: journal)
    }

    static func recoverPending(root: URL) -> Summary {
        var summary = Summary()
        guard let rootDirectory = try? Directory(root),
              let sessions = try? FileManager.default.contentsOfDirectory(at: root,
                includingPropertiesForKeys: [.isDirectoryKey, .isSymbolicLinkKey]) else { return summary }
        for session in sessions.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            guard let values = try? session.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]),
                  values.isDirectory == true, values.isSymbolicLink != true else { continue }
            do {
                try rootDirectory.checkAvailable()
                let directory = try Directory(session)
                guard directory.exists(journalFilename) else { continue }
                // Reject malformed nodes before making any new files.
                _ = try directory.read(journalFilename)
                let capture = try FileLock(directory, name: captureLockFilename, exclusive: true)
                let files = try FileLock(directory, name: RecordingReviewGate.processingLockFilename, exclusive: true)
                let outcome = try recover(directory)
                withExtendedLifetime((capture, files)) {}
                switch outcome {
                case .recovered: summary.recovered += 1
                case .cleaned: summary.skipped += 1
                case .needsReview: summary.failed += 1
                }
                // finish() attempts this while we hold the exclusive file
                // lock; publish the capability once recovery releases it.
            } catch RecoveryError.busy {
                summary.skipped += 1
            } catch {
                // No raw device errors or meeting details enter startup logs.
                summary.failed += 1
            }
            try? RecordingReviewGate.registerCompletedSessionLease(session: session)
        }
        return summary
    }

    private enum Outcome { case recovered, cleaned, needsReview }

    private static func recover(_ directory: Directory) throws -> Outcome {
        try directory.checkAvailable()
        let data = try directory.read(journalFilename)
        let gate = try readGate(directory)
        let snapshot = try decodeJournal(data, gate: gate)

        if directory.exists("meta.json") {
            _ = try finalizedMetadata(directory, gate: gate)
            if gate.phase == .finished {
                // Clean stop may have finished just before a crash. Never
                // replace a completed decision, metadata, or transcript.
                try directory.remove(journalFilename, expected: data)
                return .cleaned
            }
            try directory.checkAvailable()
            try RecordingReviewGate.finish(session: directory.url, sessionID: gate.sessionID,
                                           reasons: [recoveryReason])
            try directory.remove(journalFilename, expected: data)
            return .recovered
        }
        guard !directory.exists("transcript.json"), gate.reviewStatus != .approved else {
            throw RecoveryError.invalidJournal
        }
        // Hold before publishing metadata: a crash at any following point is
        // still unapproved, even when the next startup sees finalized media.
        try RecordingReviewGate.hold(session: directory.url, sessionID: gate.sessionID, reason: recoveryReason)
        let mic: PCMTrack
        let system: PCMTrack
        do {
            mic = try PCMTrack(directory: directory, name: "mic.caf")
            system = try PCMTrack(directory: directory, name: "system.caf")
        } catch {
            // The downstream schema requires both tracks. Do not invent a
            // silent replacement or release a one-track recording to workers.
            return .needsReview
        }
        var metadata = snapshot
        let offsets = snapshot["start_offset_ms"] as! [String: Any]
        let duration = max(mic.duration + number(offsets["mic"])! / 1_000,
                           system.duration + number(offsets["system"])! / 1_000)
        let paused = number(snapshot["paused_duration_seconds"])!
        let captureStart = number(snapshot["capture_start_offset_seconds"]) ?? 0
        let wallDuration = captureStart + duration + paused
        let started = date(snapshot["started"])!
        metadata["ended"] = ISO8601DateFormatter().string(from: started.addingTimeInterval(wallDuration))
        metadata["duration_seconds"] = duration
        metadata["wall_duration_seconds"] = wallDuration
        metadata["files"] = ["mic": "mic.caf", "system": "system.caf"]
        metadata["start_offset_ms"] = ["mic": offsets["mic"]!, "system": offsets["system"]!]
        metadata["mic_capture"] = mic.report(snapshot["mic_capture"] as! [String: Any])
        metadata["system_capture"] = system.report(snapshot["system_capture"] as! [String: Any])
        metadata["capture_recovery"] = ["status": "interrupted", "session_id": gate.sessionID.uuidString,
                                         "snapshot_ended": snapshot["ended"] as! String]
        if snapshot["screen_recording_requested"] as? Bool == true {
            metadata["screen_error"] = screenRecoveryError
        }
        // The source video is deliberately left on disk. It cannot be treated
        // as a finalized recording after a process crash.
        try directory.checkAvailable()
        try directory.requireUnchanged(journalFilename, expected: data)
        try directory.publish(try encoded(metadata), name: "meta.json")
        try RecordingReviewGate.finish(session: directory.url, sessionID: gate.sessionID,
                                       reasons: [recoveryReason])
        try directory.remove(journalFilename, expected: data)
        return .recovered
    }

    private static func encodeJournal(sessionID: UUID, metadata: [String: Any],
                                      gate: RecordingReviewGate.Record) throws -> Data {
        try validateSnapshot(metadata, gate: gate)
        return try encoded(["schema_version": 1, "session_id": sessionID.uuidString, "metadata": metadata])
    }

    private static func decodeJournal(_ data: Data, gate: RecordingReviewGate.Record) throws -> [String: Any] {
        guard let journal = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(journal.keys) == Set(["schema_version", "session_id", "metadata"]),
              let version = journal["schema_version"] as? NSNumber,
              CFGetTypeID(version) != CFBooleanGetTypeID(),
              !["f", "d"].contains(String(cString: version.objCType)), version.intValue == 1,
              let rawID = journal["session_id"] as? String, UUID(uuidString: rawID) == gate.sessionID,
              let metadata = journal["metadata"] as? [String: Any] else { throw RecoveryError.invalidJournal }
        try validateSnapshot(metadata, gate: gate)
        return metadata
    }

    private static func validateSnapshot(_ metadata: [String: Any], gate: RecordingReviewGate.Record,
                                         finalized: Bool = false) throws {
        guard let started = date(metadata["started"]), started == gate.startedAt,
              date(metadata["ended"]) != nil,
              metadata["capture_storage_format"] as? String == "pcm_s16",
              let files = metadata["files"] as? [String: String],
              files["mic"] == "mic.caf", files["system"] == "system.caf",
              let offsets = metadata["start_offset_ms"] as? [String: Any],
              metadata["mic_capture"] is [String: Any], metadata["system_capture"] is [String: Any],
              let timezone = metadata["recording_timezone"] as? String,
              TimeZone(identifier: timezone) != nil,
              let utcOffset = number(metadata["recording_utc_offset_seconds"]), abs(utcOffset) <= 86_400 else {
            throw RecoveryError.invalidJournal
        }
        for key in ["duration_seconds", "wall_duration_seconds", "paused_duration_seconds"] {
            // Clean legacy duration fields use wall time and can be negative
            // after a clock correction. Checkpoints use monotonic durations.
            let minimum = finalized && key != "paused_duration_seconds" ? -31_536_000.0 : 0
            guard let value = number(metadata[key]), value >= minimum, value <= 31_536_000 else { throw RecoveryError.invalidJournal }
        }
        if let rawOffset = metadata["capture_start_offset_seconds"] {
            guard let offset = number(rawOffset), offset >= 0, offset <= 31_536_000 else {
                throw RecoveryError.invalidJournal
            }
        }
        for key in ["mic", "system"] {
            guard let value = number(offsets[key]), value >= 0, value <= 31_536_000_000,
                  value.rounded(.towardZero) == value else { throw RecoveryError.invalidJournal }
        }
        for key in ["transcription_requested", "speaker_identification_requested", "screen_recording_requested", "screen_recording_started"] {
            guard let value = metadata[key] as? NSNumber, CFGetTypeID(value) == CFBooleanGetTypeID() else {
                throw RecoveryError.invalidJournal
            }
        }
    }

    private static func readGate(_ directory: Directory) throws -> RecordingReviewGate.Record {
        // Bounded no-follow read first; the existing gate owns schema validation.
        _ = try directory.read(RecordingReviewGate.filename)
        guard let gate = try RecordingReviewGate.read(session: directory.url) else { throw RecoveryError.invalidJournal }
        return gate
    }

    private static func finalizedMetadata(_ directory: Directory, gate: RecordingReviewGate.Record) throws -> [String: Any] {
        let bytes = try directory.read("meta.json")
        guard let metadata = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else {
            throw RecoveryError.invalidJournal
        }
        try validateSnapshot(metadata, gate: gate, finalized: true)
        if let value = metadata["capture_recovery"] {
            guard let recovery = value as? [String: Any], recovery["status"] as? String == "interrupted",
                  date(recovery["snapshot_ended"]) != nil,
                  let rawID = recovery["session_id"] as? String, UUID(uuidString: rawID) == gate.sessionID else {
                throw RecoveryError.identityMismatch
            }
        }
        return metadata
    }

    private static func number(_ value: Any?) -> Double? {
        guard let value = value as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
              value.doubleValue.isFinite else { return nil }
        return value.doubleValue
    }

    private static func date(_ value: Any?) -> Date? {
        guard let value = value as? String, value.utf8.count <= 40 else { return nil }
        return ISO8601DateFormatter().date(from: value)
    }

    private static func encoded(_ value: [String: Any]) throws -> Data {
        guard JSONSerialization.isValidJSONObject(value) else { throw RecoveryError.invalidJournal }
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        guard !data.isEmpty, data.count <= maximumJSONSize else { throw RecoveryError.invalidJournal }
        return data
    }

    private struct PCMTrack {
        let frames: AVAudioFramePosition
        let sampleRate: Double
        let channels: AVAudioChannelCount
        var duration: Double { Double(frames) / sampleRate }

        init(directory: Directory, name: String) throws {
            let descriptor = try directory.openRegular(name)
            defer { close(descriptor) }
            var magic = [UInt8](repeating: 0, count: 4)
            guard pread(descriptor, &magic, magic.count, 0) == magic.count,
                  magic == Array("caff".utf8) else { throw RecoveryError.missingMedia }
            // /dev/fd pins the same regular inode validated above; a replaced
            // path cannot redirect AVFoundation into another file.
            let file = try AVAudioFile(forReading: URL(fileURLWithPath: "/dev/fd/\(descriptor)"))
            let format = file.fileFormat.streamDescription.pointee
            guard format.mFormatID == kAudioFormatLinearPCM, format.mBitsPerChannel == 16,
                  format.mFormatFlags & kAudioFormatFlagIsFloat == 0,
                  format.mSampleRate.isFinite, format.mSampleRate > 0, format.mSampleRate <= 384_000,
                  format.mChannelsPerFrame > 0, format.mChannelsPerFrame <= 8,
                  file.length > 0, Double(file.length) / format.mSampleRate <= 31_536_000 else {
                throw RecoveryError.missingMedia
            }
            // Probe the final frame as well as the reported length so a
            // truncated/malformed header is not accepted as usable audio.
            file.framePosition = file.length - 1
            guard let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 1) else {
                throw RecoveryError.missingMedia
            }
            try file.read(into: buffer, frameCount: 1)
            guard buffer.frameLength == 1 else { throw RecoveryError.missingMedia }
            frames = file.length
            sampleRate = format.mSampleRate
            channels = format.mChannelsPerFrame
            try directory.requireSameFile(name, descriptor: descriptor)
        }

        func report(_ original: [String: Any]) -> [String: Any] {
            var report = original
            // Existing reports distinguish captured samples from inserted
            // silence. The checkpoint's known silence can be stale, so bound
            // it by the file that survived and retain incomplete coverage.
            let silence = min(max(0, number(report["silence_frames"]) ?? 0), Double(frames)).rounded(.down)
            report["captured_frames"] = frames - Int64(silence)
            report["decoded_frames"] = frames
            report["sample_rate"] = sampleRate
            report["channels"] = channels
            report["duration_seconds"] = duration
            report["silence_frames"] = Int64(silence)
            report["coverage_complete"] = false
            report["status"] = "incomplete"
            return report
        }
    }

    fileprivate final class Directory {
        let url: URL
        let descriptor: Int32
        private let identity: stat

        init(_ url: URL) throws {
            guard url.isFileURL else { throw RecoveryError.unavailable }
            let descriptor = open(url.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard descriptor >= 0 else { throw RecoveryError.unavailable }
            var info = stat()
            guard fstat(descriptor, &info) == 0, info.st_mode & S_IFMT == S_IFDIR else {
                close(descriptor)
                throw RecoveryError.unavailable
            }
            self.url = url
            self.descriptor = descriptor
            identity = info
            try checkAvailable()
        }

        deinit { close(descriptor) }

        func checkAvailable() throws {
            var current = stat()
            guard lstat(url.path, &current) == 0, current.st_mode & S_IFMT == S_IFDIR,
                  current.st_dev == identity.st_dev, current.st_ino == identity.st_ino,
                  !exists(RecordingReviewGate.deletionFilename) else { throw RecoveryError.unavailable }
        }

        func exists(_ name: String) -> Bool {
            var info = stat()
            if fstatat(descriptor, name, &info, AT_SYMLINK_NOFOLLOW) == 0 { return true }
            return errno != ENOENT
        }

        func openRegular(_ name: String) throws -> Int32 {
            let fd = openat(descriptor, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
            guard fd >= 0 else { throw RecoveryError.unavailable }
            var info = stat()
            guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
                  info.st_nlink == 1, info.st_uid == getuid() else {
                close(fd)
                throw RecoveryError.unavailable
            }
            return fd
        }

        func requireSameFile(_ name: String, descriptor fd: Int32) throws {
            var actual = stat()
            var current = stat()
            guard fstat(fd, &actual) == 0,
                  fstatat(descriptor, name, &current, AT_SYMLINK_NOFOLLOW) == 0,
                  current.st_mode & S_IFMT == S_IFREG, current.st_nlink == 1,
                  current.st_dev == actual.st_dev, current.st_ino == actual.st_ino else {
                throw RecoveryError.unavailable
            }
        }

        func read(_ name: String) throws -> Data {
            let fd = try openRegular(name)
            defer { close(fd) }
            var info = stat()
            guard fstat(fd, &info) == 0, info.st_size > 0, info.st_size <= maximumJSONSize else {
                throw RecoveryError.invalidJournal
            }
            let data = try FileHandle(fileDescriptor: fd, closeOnDealloc: false).read(upToCount: maximumJSONSize + 1) ?? Data()
            guard !data.isEmpty, data.count == info.st_size else { throw RecoveryError.invalidJournal }
            try requireSameFile(name, descriptor: fd)
            return data
        }

        func requireUnchanged(_ name: String, expected: Data) throws {
            guard try read(name) == expected else { throw RecoveryError.identityMismatch }
        }

        func publish(_ data: Data, name: String, replacing previous: Data? = nil) throws {
            try checkAvailable()
            let temporary = ".quill-recovery-\(UUID().uuidString).tmp"
            let fd = openat(descriptor, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
            guard fd >= 0 else { throw RecoveryError.unavailable }
            defer { close(fd); unlinkat(descriptor, temporary, 0) }
            try data.withUnsafeBytes { bytes in
                var offset = 0
                while offset < bytes.count {
                    let count = Darwin.write(fd, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                    if count < 0, errno == EINTR { continue }
                    guard count > 0 else { throw RecoveryError.unavailable }
                    offset += count
                }
            }
            guard fsync(fd) == 0 else { throw RecoveryError.unavailable }
            try checkAvailable()
            if let previous {
                try requireUnchanged(name, expected: previous)
                guard renameat(descriptor, temporary, descriptor, name) == 0 else { throw RecoveryError.unavailable }
            } else {
                guard linkat(descriptor, temporary, descriptor, name, 0) == 0 else { throw RecoveryError.unavailable }
                unlinkat(descriptor, temporary, 0)
            }
            guard fsync(descriptor) == 0 else { throw RecoveryError.unavailable }
        }

        func remove(_ name: String, expected: Data) throws {
            try checkAvailable()
            try requireUnchanged(name, expected: expected)
            guard unlinkat(descriptor, name, 0) == 0, fsync(descriptor) == 0 else { throw RecoveryError.unavailable }
        }
    }

    fileprivate final class FileLock {
        private let descriptor: Int32

        init(_ directory: Directory, name: String, exclusive: Bool) throws {
            try directory.checkAvailable()
            let fd = openat(directory.descriptor, name, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, 0o600)
            guard fd >= 0 else { throw RecoveryError.unavailable }
            var transferred = false
            defer { if !transferred { close(fd) } }
            var info = stat()
            guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
                  info.st_nlink == 1, info.st_uid == getuid(), info.st_mode & 0o077 == 0 else {
                throw RecoveryError.unavailable
            }
            guard flock(fd, (exclusive ? LOCK_EX : LOCK_SH) | LOCK_NB) == 0 else {
                if errno == EWOULDBLOCK || errno == EAGAIN { throw RecoveryError.busy }
                throw RecoveryError.unavailable
            }
            try directory.requireSameFile(name, descriptor: fd)
            try directory.checkAvailable()
            descriptor = fd
            transferred = true
        }

        deinit { flock(descriptor, LOCK_UN); close(descriptor) }
    }
}
