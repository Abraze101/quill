import Foundation

@main
struct TranscriptionRecoveryChecks {
    static func main() throws {
        let fm = FileManager.default
        let temporary = fm.temporaryDirectory.appendingPathComponent("quill-recovery-checks-\(UUID().uuidString)", isDirectory: true)
        try fm.createDirectory(at: temporary, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: temporary) }
        let root = temporary.appendingPathComponent("recordings", isDirectory: true)
        try fm.createDirectory(at: root, withIntermediateDirectories: true)

        func metadata(_ intent: Any? = nil, files: [String: String] = ["mic": "mic.caf", "system": "system.caf"]) throws -> Data {
            var object: [String: Any] = ["files": files, "started": "2026-01-01T10:00:00Z", "ended": "2026-01-01T10:01:00Z"]
            if let intent { object[TranscriptionRecovery.intentKey] = intent }
            return try JSONSerialization.data(withJSONObject: object)
        }
        func eligible(_ data: Data?, transcript: Bool = false, waiting: Bool = false) -> Bool {
            TranscriptionRecovery.shouldResume(metadata: data, hasTranscript: transcript, registeredWaiting: waiting)
        }
        let optedIn = try metadata(true), optedOut = try metadata(false), legacy = try metadata()
        precondition(eligible(optedIn)) // No completion hook is required for normal Quill recovery.
        precondition(eligible(legacy, waiting: true))
        precondition(!eligible(legacy))
        precondition(!eligible(optedOut))
        precondition(!eligible(optedOut, waiting: true)) // Explicit newer intent wins over a stale handoff.
        precondition(!eligible(optedIn, transcript: true, waiting: true))
        for value: Any in [1, 0, "true", NSNull(), [true]] {
            let data = try metadata(value)
            precondition(!eligible(data, waiting: true))
        }
        for data in [nil, Data("bad".utf8), Data("[]".utf8), Data("{}".utf8), Data(repeating: 32, count: 100_001)] {
            precondition(!eligible(data, waiting: true))
        }
        let unsafeTrack = try metadata(true, files: ["mic": "../other/mic.caf"])
        precondition(!eligible(unsafeTrack, waiting: true))
        let emptyFiles = try metadata(true, files: [:])
        precondition(!eligible(emptyFiles, waiting: true))
        let singleTrack = try metadata(true, files: ["mic": "mic.caf"])
        precondition(!eligible(singleTrack, waiting: true))
        for field in ["started", "ended"] {
            for value: Any in ["", "   ", 1, NSNull()] {
                var incomplete = try JSONSerialization.jsonObject(with: optedIn) as! [String: Any]
                incomplete[field] = value
                let data = try JSONSerialization.data(withJSONObject: incomplete)
                precondition(!eligible(data, waiting: true))
            }
            var missing = try JSONSerialization.jsonObject(with: optedIn) as! [String: Any]
            missing.removeValue(forKey: field)
            let data = try JSONSerialization.data(withJSONObject: missing)
            precondition(!eligible(data, waiting: true))
        }

        func session(_ name: String, under parent: URL? = nil, data: Data?) throws -> URL {
            let directory = (parent ?? root).appendingPathComponent(name, isDirectory: true)
            try fm.createDirectory(at: directory, withIntermediateDirectories: true)
            if let data { try data.write(to: directory.appendingPathComponent("meta.json")) }
            return directory
        }
        let first = try session("2026.01.01-1000", data: optedIn)
        let second = try session("2026.01.02-1000", data: legacy)
        _ = try session("2026.01.03-1000", data: legacy)
        let off = try session("2026.01.04-1000", data: optedOut)
        let complete = try session("2026.01.05-1000", data: optedIn)
        try Data("preserved transcript".utf8).write(to: complete.appendingPathComponent("transcript.json"))
        _ = try session("2026.01.06-1000", data: Data("malformed".utf8))
        _ = try session("2026.01.07-1000", data: nil)
        let outside = try session("outside", under: temporary, data: optedIn)
        let nested = try session("nested", under: first, data: optedIn)
        precondition(!TranscriptionRecovery.isPending(session: outside, root: root, registeredWaiting: true))
        precondition(!TranscriptionRecovery.isPending(session: nested, root: root, registeredWaiting: true))
        precondition(!TranscriptionRecovery.isPending(session: root, root: root, registeredWaiting: true))
        precondition(!TranscriptionRecovery.isPending(session: URL(string: "https://example.com/meeting")!, root: root, registeredWaiting: true))

        let sessionLink = root.appendingPathComponent("linked-session", isDirectory: true)
        try fm.createSymbolicLink(at: sessionLink, withDestinationURL: outside)
        let metaLink = try session("linked-meta", data: nil)
        try fm.createSymbolicLink(at: metaLink.appendingPathComponent("meta.json"),
                                 withDestinationURL: outside.appendingPathComponent("meta.json"))
        let transcriptLink = try session("linked-transcript", data: optedIn)
        try fm.createSymbolicLink(at: transcriptLink.appendingPathComponent("transcript.json"),
                                 withDestinationURL: temporary.appendingPathComponent("missing-transcript.json"))
        let waiting: Set<URL> = [second, off, outside, nested, sessionLink, metaLink, transcriptLink]
        let pending = TranscriptionRecovery.pendingSessions(root: root, waitingForTranscript: waiting)
        precondition(pending.map(\.standardizedFileURL) == [first, second].map(\.standardizedFileURL),
                     "Only opted-in and registered legacy sessions may resume: \(pending)")
        let oldMetadata = try Data(contentsOf: off.appendingPathComponent("meta.json"))
        precondition(oldMetadata == optedOut)
        let oldTranscript = try String(contentsOf: complete.appendingPathComponent("transcript.json"), encoding: .utf8)
        precondition(oldTranscript == "preserved transcript")
        print("Transcription recovery: saved intent, legacy registration, malformed metadata, existing outputs, and root/link boundaries passed")
    }
}
