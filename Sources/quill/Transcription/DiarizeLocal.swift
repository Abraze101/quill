import ArgumentParser
import Darwin
import FluidAudio
import Foundation
import MeetingWorkflow

/// Internal subprocess only: no AppKit initialization, recorder, microphone,
/// browser, or workflow hook. Models and voice embeddings die with this process.
struct DiarizeLocal: ParsableCommand {
    static let configuration = CommandConfiguration(commandName: "diarize-local",
        abstract: "Separate speakers in one completed local audio track.", shouldDisplay: false)
    @Option(name: .long) var input: String
    @Option(name: .long) var output: String
    @Option(name: .long) var parentPid: Int32

    func run() throws {
        let audio = URL(fileURLWithPath: input)
        let destination = URL(fileURLWithPath: output)
        let session = audio.deletingLastPathComponent()
        let expectedParent = parentPid
        guard expectedParent > 1, getppid() == expectedParent,
              ["mic.caf", "system.caf"].contains(audio.lastPathComponent),
              !FileManager.default.fileExists(atPath: destination.path),
              let values = try? audio.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey]),
              values.isRegularFile == true, values.isSymbolicLink != true else {
            throw ValidationError("A completed recording track and private output are required")
        }
        var directoryInfo = stat()
        guard lstat(destination.deletingLastPathComponent().path, &directoryInfo) == 0,
              directoryInfo.st_mode & S_IFMT == S_IFDIR, directoryInfo.st_uid == getuid(),
              directoryInfo.st_mode & 0o077 == 0 else {
            throw ValidationError("Speaker output needs a private local directory")
        }
        // The parent runner creates a new process group; retain that boundary
        // for diagnostic callers using another launch mechanism.
        if getpgrp() != getpid(), setpgid(0, 0) != 0 { throw ExitCode(1) }
        Task.detached {
            while true {
                if getppid() != expectedParent || RecordingReviewGate.blocksProcessing(session: session) {
                    Darwin._exit(75)
                }
                try? await Task.sleep(for: .milliseconds(250))
            }
        }
        Task.detached {
            do {
                let lease = try RecordingReviewGate.acquireProcessingLease(session: session)
                defer { withExtendedLifetime(lease) {} }
                let manager = OfflineDiarizerManager()
                try await manager.prepareModels()
                try RecordingReviewGate.requireProcessingAllowed(session: session)
                let result = try await manager.process(audio)
                try RecordingReviewGate.requireProcessingAllowed(session: session)
                let payload = LocalDiarizationResult(segments: result.segments.map {
                    SpeakerInterval(speaker: $0.speakerId, start: Double($0.startTimeSeconds),
                                    end: Double($0.endTimeSeconds), quality: Double($0.qualityScore))
                })
                let data = try JSONEncoder().encode(payload)
                guard data.count <= LocalDiarizationProcess.maximumOutputBytes else { throw ExitCode(1) }
                // Explicit POSIX flags avoid Foundation writing-option ABI
                // differences between this Mac's Swift tools and runtime.
                let descriptor = open(destination.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
                guard descriptor >= 0 else { throw ExitCode(1) }
                defer { close(descriptor) }
                try data.withUnsafeBytes { bytes in
                    var offset = 0
                    while offset < bytes.count {
                        let count = Darwin.write(descriptor, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                        if count < 0, errno == EINTR { continue }
                        guard count > 0 else { throw ExitCode(1) }
                        offset += count
                    }
                }
                guard fsync(descriptor) == 0 else { throw ExitCode(1) }
                Darwin.exit(0)
            } catch {
                FileHandle.standardError.write(Data("Local speaker detection was unavailable.\n".utf8))
                Darwin.exit(1)
            }
        }
        dispatchMain()
    }
}
