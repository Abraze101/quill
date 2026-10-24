import CoreFoundation
import Foundation

/// Recovery uses a decision saved with the completed recording. Merely turning
/// transcription on today must not opt older, unregistered recordings in.
public enum TranscriptionRecovery {
    public static let intentKey = "transcription_requested"

    /// Pure policy shared by startup recovery and the immediate completion path.
    /// Missing intent is accepted only for a previously registered transcript
    /// handoff; an explicit false or malformed intent always wins.
    public static func shouldResume(metadata: Data?, hasTranscript: Bool,
                                    registeredWaiting: Bool) -> Bool {
        guard !hasTranscript, let metadata, metadata.count <= 100_000,
              let object = try? JSONSerialization.jsonObject(with: metadata) as? [String: Any],
              let started = object["started"] as? String, !started.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let ended = object["ended"] as? String, !ended.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let files = object["files"] as? [String: String],
              files["mic"] == "mic.caf", files["system"] == "system.caf" else { return false }
        guard let value = object[intentKey] else { return registeredWaiting }
        guard let boolean = value as? NSNumber,
              CFGetTypeID(boolean) == CFBooleanGetTypeID() else { return false }
        return boolean.boolValue
    }

    /// Only inspect immediate, real session directories under the configured
    /// root. Never follow a session or metadata link into another recording tree.
    public static func isPending(session: URL, root: URL, registeredWaiting: Bool) -> Bool {
        let fm = FileManager.default
        guard !RecordingReviewGate.blocksProcessing(session: session), session.isFileURL, root.isFileURL,
              session.standardizedFileURL.deletingLastPathComponent() == root.standardizedFileURL,
              session.resolvingSymlinksInPath().deletingLastPathComponent()
                == root.resolvingSymlinksInPath(),
              let directory = try? session.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]),
              directory.isDirectory == true, directory.isSymbolicLink != true else { return false }
        let meta = session.appendingPathComponent("meta.json")
        guard let values = try? meta.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]),
              values.isRegularFile == true, values.isSymbolicLink != true,
              let size = values.fileSize, size <= 100_000 else { return false }
        let transcript = session.appendingPathComponent("transcript.json")
        // A broken transcript link is also an existing output, never permission
        // to replace it or follow its destination.
        let hasTranscript = fm.fileExists(atPath: transcript.path)
            || (try? fm.destinationOfSymbolicLink(atPath: transcript.path)) != nil
        return shouldResume(metadata: try? Data(contentsOf: meta), hasTranscript: hasTranscript,
                            registeredWaiting: registeredWaiting)
    }

    public static func pendingSessions(root: URL, waitingForTranscript: Set<URL>) -> [URL] {
        guard root.isFileURL,
              let entries = try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
        else { return [] }
        let registered = Set(waitingForTranscript.map(\.standardizedFileURL))
        return entries.filter {
            isPending(session: $0, root: root, registeredWaiting: registered.contains($0.standardizedFileURL))
        }.sorted { $0.lastPathComponent < $1.lastPathComponent }
    }
}
