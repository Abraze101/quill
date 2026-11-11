import CoreFoundation
import Foundation
import MeetingWorkflow

/// Post-recording pipeline: a serial queue of session folders to transcribe.
/// mic.caf → "me", system.caf → "them"; each track's segments are shifted by
/// its start offset, merged by timestamp, and written as transcript.json
/// (canonical) plus transcript.md (readable). The filesystem is the queue —
/// `resumePending()` rescans opted-in sessions at launch, so a crash or quit
/// mid-transcription retries on next run without opting older raw recordings in.
/// Failures append to the session's transcribe.log
/// and never block later jobs.
actor TranscriptionCoordinator {
    enum Status: Sendable {
        case idle
        case preparing(session: String, reservation: UUID)
        case transcribing(session: String, queued: Int, reservation: UUID)
        case cancelling(session: String, queued: Int, reservation: UUID)
        case failed(session: String)
    }

    enum RetryResult: Sendable, Equatable {
        case disabled
        case queued(Int)
    }

    private struct Work: Sendable { let dir: URL; let completion: String? }
    private var queue: [Work] = []
    private let journal: CompletionJournal
    private let delivery: CompletionDelivery
    private var draining = false
    private var activeSession: URL?
    private var activeCompletion: String?
    private struct Reservation {
        let id = UUID()
        let session: URL
        let sessionID: UUID?
        let lease: RecordingReviewGate.ProcessingLease
    }
    private var reservation: Reservation?
    private var engine: TranscriptionEngine?
    private var lastFailure: URL?
    private var status: Status = .idle
    private var statusHandler: (@Sendable (Status) -> Void)?

    init(journal: CompletionJournal = CompletionJournal()) {
        self.journal = journal
        delivery = CompletionDelivery(journal: journal)
    }

    var hasWorkForUpdate: Bool { draining || activeSession != nil || !queue.isEmpty }

    func setStatusHandler(_ handler: @escaping @Sendable (Status) -> Void) {
        statusHandler = handler
    }

    /// A menu action carries the reservation that produced its label. Never
    /// substitute the next session, even when it reuses the same folder name.
    func cancelActiveTranscription(expectedSession: String, expectedReservation: UUID) throws -> Bool {
        guard let reservation, reservation.id == expectedReservation,
              reservation.session.lastPathComponent == expectedSession,
              try RecordingReviewGate.cancelLeasedTranscription(session: reservation.session,
                sessionID: reservation.sessionID, lease: reservation.lease) else { return false }
        refreshProcessingCancellations()
        return true
    }

    func refreshProcessingCancellations() {
        let previousCount = queue.count
        queue.removeAll { RecordingReviewGate.processingCancellationRequested(session: $0.dir) }
        var reconcileJournal = previousCount != queue.count
        defer { if reconcileJournal { try? journal.reconcileCancellations() } }
        if let lastFailure, RecordingReviewGate.processingCancellationRequested(session: lastFailure) {
            self.lastFailure = nil
            reconcileJournal = true
            if case .failed = status { publish(.idle) }
        }
        if let reservation, RecordingReviewGate.processingCancellationRequested(session: reservation.session) {
            if case .cancelling(_, let queued, let id) = status,
               queued == queue.count, id == reservation.id { return }
            reconcileJournal = true
            publish(.cancelling(session: reservation.session.lastPathComponent, queued: queue.count, reservation: reservation.id))
        } else if previousCount != queue.count { refreshQueueStatus() }
    }

    /// Queue a finished session. With transcription disabled in config, the
    /// on_stop hook still fires — it just gets an untranscribed folder.
    func enqueue(_ sessionDir: URL, completion: String? = nil) {
        let sessionDir = sessionDir.standardizedFileURL
        guard !RecordingReviewGate.blocksProcessing(session: sessionDir) else { return }
        if contains(sessionDir) {
            // A manual scan can see finalized metadata just before Stop
            // registers its handoff. Preserve that later registration while
            // deduplicating the transcription, including a lease-busy item
            // that is both reserved and back in the queue.
            if let completion {
                if activeSession == sessionDir { activeCompletion = completion }
                if let index = queue.firstIndex(where: { $0.dir == sessionDir }) {
                    queue[index] = Work(dir: sessionDir, completion: completion)
                }
            }
            return
        }
        guard Config.transcriptionEnabled(),
              TranscriptionRecovery.isPending(session: sessionDir, root: sessionDir.deletingLastPathComponent(),
                                              registeredWaiting: completion != nil) else {
            runHook(completion: completion, for: sessionDir)
            return
        }
        queue.append(Work(dir: sessionDir, completion: completion))
        refreshQueueStatus()
        drainIfIdle()
    }

    /// Retry only sessions already opted into transcription. This neither
    /// registers new hooks nor retries previously delivered downstream work.
    func retryPending(root: URL) -> RetryResult {
        refreshProcessingCancellations()
        guard Config.transcriptionEnabled() else { return .disabled }
        let registered = (try? journal.jobs()) ?? []
        let count = queuePending(root: root, registered: registered)
        drainIfIdle()
        return .queued(count)
    }

    /// Recover only finished, opted-in sessions, plus older sessions whose
    /// transcript handoff was already registered. An explicit opt-out wins.
    func resumePending(root: URL) {
        RecordingReviewGate.registerCompletedSessionLeases(root: root)
        let registered = (try? journal.jobs()) ?? []
        // Resume only handoffs that were registered when a recording stopped.
        // Existing recordings do not acquire a newly configured external hook.
        for job in registered where job.state == .waitingForTranscript {
            guard !RecordingReviewGate.blocksProcessing(session: job.session) else { continue }
            if FileManager.default.fileExists(atPath: job.session.appendingPathComponent("transcript.json").path)
                || !Config.transcriptionEnabled() {
                try? journal.ready(job.id)
            }
        }
        Task { [delivery] in
            await delivery.setFailureHandler { name in
                notifyUser(title: "quill — completion handoff failed", body: "\(name) remains saved locally; its completion hook needs attention.")
            }
            await delivery.resume()
        }
        guard Config.transcriptionEnabled() else { return }
        let count = queuePending(root: root, registered: registered)
        if count > 0 {
            FileHandle.standardError.write(Data(
                "resuming \(count) untranscribed session(s)\n".utf8
            ))
        }
        drainIfIdle()
    }

    private func queuePending(root: URL, registered: [CompletionJournal.Job]) -> Int {
        let waiting = Set(registered.filter { $0.state == .waitingForTranscript }.map { $0.session.standardizedFileURL })
        let pending = TranscriptionRecovery.pendingSessions(root: root, waitingForTranscript: waiting)
        var count = 0
        for candidate in pending {
            let dir = candidate.standardizedFileURL
            guard !contains(dir) else { continue }
            let completion = registered.first(where: { $0.session.standardizedFileURL == dir && $0.state == .waitingForTranscript })?.id
            queue.append(Work(dir: dir, completion: completion))
            count += 1
        }
        refreshQueueStatus()
        return count
    }

    private func contains(_ dir: URL) -> Bool {
        activeSession == dir || queue.contains { $0.dir == dir }
    }

    private func refreshQueueStatus() {
        if case .transcribing(let session, _, let id) = status, reservation?.id == id {
            publish(.transcribing(session: session, queued: queue.count, reservation: id))
        }
    }

    // MARK: -

    private func drainIfIdle() {
        guard !draining, !queue.isEmpty else { return }
        draining = true
        Task { await drain() }
    }

    private func drain() async {
        while !queue.isEmpty {
            let work = queue.removeFirst()
            let dir = work.dir
            // Reserve before any await, including lease contention and model
            // preparation, so repeated scans cannot enqueue this session again.
            activeSession = dir
            activeCompletion = work.completion
            defer { reservation = nil; activeSession = nil; activeCompletion = nil }
            guard !RecordingReviewGate.blocksProcessing(session: dir) else { continue }
            let lease: RecordingReviewGate.ProcessingLease
            do { lease = try RecordingReviewGate.acquireProcessingLease(session: dir) }
            catch RecordingReviewGate.LeaseError.busy {
                queue.append(work)
                try? await Task.sleep(for: .seconds(2))
                continue
            } catch {
                // A deleted, moved, held, or unsafe folder is not a place to
                // write an error log. Its old work must not recreate files.
                continue
            }
            defer { withExtendedLifetime(lease) {} }
            // Recheck before each session: disabling transcription also
            // prevents a waiting session from starting new model work.
            guard Config.transcriptionEnabled() else {
                runHook(completion: activeCompletion, for: dir)
                continue
            }
            // A session can finish elsewhere, be opted out, or disappear while
            // waiting behind another job. Never overwrite its transcript or
            // replay its handoff from this stale queue entry.
            guard TranscriptionRecovery.isPending(session: dir, root: dir.deletingLastPathComponent(),
                                                  registeredWaiting: work.completion != nil) else { continue }
            do {
                reservation = try Reservation(session: dir, sessionID: RecordingReviewGate.read(session: dir)?.sessionID, lease: lease)
                try await transcribeUntilCancelled(dir)
                try RecordingReviewGate.requireProcessingAllowed(session: dir)
                if lastFailure == dir { lastFailure = nil }
                notifyUser(title: "quill — transcript ready", body: dir.lastPathComponent)
                runHook(completion: activeCompletion, for: dir)
            } catch is CancellationError {
                // A user cancellation or removal stops this session only. Keep the
                // shared lease until all model work has actually unwound.
            } catch RecordingReviewGate.GateError.processingHeld {
                // A held/deleted session must not acquire new output or hooks.
            } catch {
                guard !RecordingReviewGate.blocksProcessing(session: dir) else { continue }
                log(dir, "transcription failed: \(error)")
                lastFailure = dir
                notifyUser(
                    title: "quill — transcription failed",
                    body: "\(dir.lastPathComponent) — see transcribe.log"
                )
                // The raw recording can still be archived; notes remain held
                // by the external worker when no usable transcript exists.
                runHook(completion: activeCompletion, for: dir)
            }
        }
        await engine?.release()
        engine = nil
        refreshProcessingCancellations()
        publish(lastFailure.map { .failed(session: $0.lastPathComponent) } ?? .idle)
        draining = false
        // An enqueue that landed between the loop exiting and the release
        // finishing would otherwise sit until the next enqueue.
        drainIfIdle()
    }

    private func transcribeUntilCancelled(_ dir: URL) async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask { try await self.transcribe(dir) }
            group.addTask {
                while true {
                    try Task.checkCancellation()
                    guard !RecordingReviewGate.blocksProcessing(session: dir) else {
                        await self.refreshProcessingCancellations()
                        throw CancellationError()
                    }
                    try await Task.sleep(for: .milliseconds(250))
                }
            }
            defer { group.cancelAll() }
            // Scope exit waits for both children. A model which takes time to
            // observe cancellation cannot keep writing after the lease ends.
            try await group.next()
        }
    }

    private func transcribe(_ dir: URL) async throws {
        try Task.checkCancellation()
        try RecordingReviewGate.requireProcessingAllowed(session: dir)
        let meta = try SessionMeta.read(from: dir)
        let engine = try await preparedEngine()
        try RecordingReviewGate.requireProcessingAllowed(session: dir)
        guard let reservation, reservation.session == dir else { throw CancellationError() }
        publish(.transcribing(session: dir.lastPathComponent, queued: queue.count, reservation: reservation.id))

        var merged: [Transcript.Segment] = []
        var speakers: [String: Transcript.Speaker] = [:]
        var speakerStatuses: [String: String] = [:]
        for track in meta.tracks {
            try Task.checkCancellation()
            try RecordingReviewGate.requireProcessingAllowed(session: dir)
            let audio = dir.appendingPathComponent(track.file)
            if meta.speakerIdentificationRequested { speakerStatuses[track.source] = "unavailable" }
            guard FileManager.default.fileExists(atPath: audio.path) else {
                log(dir, "skipping missing track \(track.file)")
                continue
            }
            log(dir, "transcribing \(track.file) (\(engine.name))")
            // One bad track (empty, truncated) shouldn't cost us the other's
            // transcript — log it and keep going.
            let segments: [TranscriptSegment]
            do {
                segments = try await engine.transcribe(audio, identifySpeakers: meta.speakerIdentificationRequested)
            } catch {
                try Task.checkCancellation()
                try RecordingReviewGate.requireProcessingAllowed(session: dir)
                log(dir, "skipping \(track.file): \(error)")
                continue
            }
            try Task.checkCancellation()
            try RecordingReviewGate.requireProcessingAllowed(session: dir)
            if meta.speakerIdentificationRequested {
                let status = await engine.speakerIdentificationStatus()
                let diagnostic = await engine.speakerIdentificationDiagnostic()
                try Task.checkCancellation()
                try RecordingReviewGate.requireProcessingAllowed(session: dir)
                speakerStatuses[track.source] = status == .disabled ? "unavailable" : status.rawValue
                if let diagnostic { log(dir, "speakers \(track.source): \(diagnostic)") }
            }
            let offset = TimeInterval(track.offsetMs) / 1000
            var trackSpeakers: [String: String] = [:]
            for segment in segments {
                var speaker = track.speaker
                if meta.speakerIdentificationRequested {
                    speaker = "unknown"
                    if let cluster = segment.speakerID, !cluster.isEmpty, cluster.utf8.count <= 80 {
                        if trackSpeakers[cluster] == nil {
                            let number = trackSpeakers.count + 1
                            let identity = "\(track.source)_speaker_\(number)"
                            trackSpeakers[cluster] = identity
                            speakers[identity] = .init(label: "\(track.source == "mic" ? "Microphone" : "Other") speaker \(number)", source: track.source)
                        }
                        speaker = trackSpeakers[cluster]!
                    }
                }
                merged.append(Transcript.Segment(speaker: speaker,
                    start_ms: Int((segment.start + offset) * 1000),
                    end_ms: Int((segment.end + offset) * 1000), text: segment.text,
                    source: meta.speakerIdentificationRequested ? track.source : nil))
            }
        }
        merged.sort { $0.start_ms < $1.start_ms }

        let names = SpeakerIdentityNames.names(in: merged.map {
            .init(speaker: $0.speaker, text: $0.text, startMs: $0.start_ms, endMs: $0.end_ms)
        }, policy: .explicitIntroV3)
        for (identity, name) in names where speakers[identity] != nil {
            speakers[identity]?.name = name.name
            speakers[identity]?.name_source = "self_introduction"
            speakers[identity]?.evidence = name.evidence
        }
        let identification: Transcript.Identification?
        if meta.speakerIdentificationRequested {
            let states = Array(speakerStatuses.values)
            let status = !states.isEmpty && states.allSatisfy({ $0 == "completed" }) ? "completed"
                : (states.contains("completed") || states.contains("partial") ? "partial" : "unavailable")
            identification = .init(status: status, tracks: speakerStatuses)
        } else { identification = nil }

        let transcript = Transcript(
            engine: engine.name,
            model: engine.model,
            speaker_labels: meta.speakerIdentificationRequested ? "diarized_v1" : "audio_source",
            created_at: ISO8601DateFormatter().string(from: Date()),
            segments: merged,
            speakers: meta.speakerIdentificationRequested ? speakers : nil,
            speaker_name_policy: meta.speakerIdentificationRequested ? SpeakerIdentityNames.Policy.explicitIntroV3.rawValue : nil,
            speaker_identification: identification,
            remote_microphone_owner: meta.remoteMicrophoneOwner,
            capture_quality: meta.captureQuality
        )
        try Task.checkCancellation()
        try RecordingReviewGate.requireProcessingAllowed(session: dir)
        try transcript.write(to: dir)
        log(dir, "done — \(merged.count) segments")
    }

    private func preparedEngine() async throws -> TranscriptionEngine {
        if let engine { return engine }
        guard let reservation else { throw CancellationError() }
        publish(.preparing(session: reservation.session.lastPathComponent, reservation: reservation.id))
        let configured = Config.transcriptionEngine()
        if configured != "parakeet" {
            FileHandle.standardError.write(Data(
                "warning: unknown transcription engine \"\(configured)\" — using parakeet\n".utf8
            ))
        }
        let engine = ParakeetEngine()
        do {
            try await engine.prepare()
            try Task.checkCancellation()
        } catch {
            await engine.release()
            throw error
        }
        self.engine = engine
        return engine
    }

    /// Fires the configured on_stop shell command with the session directory
    /// as its sole argument, after the transcript exists (or immediately after
    /// recording when transcription is disabled).
    private func runHook(completion: String?, for dir: URL) {
        guard let completion,
              let lease = try? RecordingReviewGate.acquireProcessingLease(session: dir) else { return }
        defer { withExtendedLifetime(lease) {} }
        do {
            try journal.ready(completion)
            Task { [delivery] in await delivery.resume() }
        } catch {
            log(dir, "on_stop handoff could not be marked ready: \(error)")
        }
    }

    private func log(_ dir: URL, _ message: String) {
        let line = "\(ISO8601DateFormatter().string(from: Date())) \(message)\n"
        let url = dir.appendingPathComponent("transcribe.log")
        if let handle = FileHandle(forWritingAtPath: url.path) {
            handle.seekToEndOfFile()
            handle.write(Data(line.utf8))
            try? handle.close()
        } else {
            try? Data(line.utf8).write(to: url)
        }
    }

    private func publish(_ status: Status) {
        self.status = status
        statusHandler?(status)
    }
}

/// The slice of meta.json the coordinator needs: which files exist, who they
/// represent, and how far each track started after the earliest one.
private struct SessionMeta {
    struct Track {
        let file: String
        let speaker: String
        let offsetMs: Int
        let source: String
    }

    let tracks: [Track]
    let speakerIdentificationRequested: Bool
    let remoteMicrophoneOwner: String?
    let captureQuality: Transcript.CaptureQuality?

    enum MetaError: Error, CustomStringConvertible {
        case unreadable(URL)

        var description: String {
            switch self {
            case .unreadable(let url): return "can't parse \(url.path)"
            }
        }
    }

    static func read(from dir: URL) throws -> SessionMeta {
        let url = dir.appendingPathComponent("meta.json")
        guard
            let data = try? Data(contentsOf: url),
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let files = json["files"] as? [String: String]
        else { throw MetaError.unreadable(url) }

        // Sessions recorded before offsets were captured default to 0 —
        // tracks start within tens of milliseconds of each other anyway.
        let offsets = json["start_offset_ms"] as? [String: Int] ?? [:]
        var tracks: [Track] = []
        if let mic = files["mic"] {
            tracks.append(Track(file: mic, speaker: "me", offsetMs: offsets["mic"] ?? 0, source: "mic"))
        }
        if let system = files["system"] {
            tracks.append(Track(file: system, speaker: "them", offsetMs: offsets["system"] ?? 0, source: "system"))
        }
        var owner: String?
        if let name = json["remote_microphone_owner"] as? String {
            if let frozen = json["remote_microphone_provider"] {
                let provider = frozen as? String
                let context = json["meeting_context"]
                if context == nil || context is NSNull || (context as? [String: Any])?["provider"] as? String == provider {
                    owner = MeetingContext.microphoneOwner(name, joinedProvider: provider)
                }
            } else if let saved = json["meeting_context"] as? [String: Any],
                      let bytes = try? JSONSerialization.data(withJSONObject: saved),
                      let context = try? JSONDecoder().decode(MeetingContext.self, from: bytes) {
                // Build 17 bound the profile to a validated context before
                // capture rather than storing the joined provider separately.
                owner = MeetingContext.microphoneOwner(name, joinedProvider: context.validated()?.provider)
            }
        }
        return SessionMeta(tracks: tracks,
            speakerIdentificationRequested: json["speaker_identification_requested"] as? Bool == true,
            remoteMicrophoneOwner: owner,
            captureQuality: Transcript.CaptureQuality.read(from: json))
    }
}

/// Canonical transcript. Property names are the JSON schema — this struct
/// exists to be serialized.
private struct Transcript: Codable {
    struct Segment: Codable {
        let speaker: String
        let start_ms: Int
        let end_ms: Int
        let text: String
        var source: String? = nil
    }

    struct Speaker: Codable {
        let label: String
        let source: String
        var name: String? = nil
        var name_source: String? = nil
        var evidence: [Int]? = nil
    }

    struct Identification: Codable {
        let status: String
        var engine = "fluidaudio-offline-vbx"
        let tracks: [String: String]
    }

    /// A bounded caveat, not a copy of recorder diagnostics. Older sessions
    /// without capture reports keep their existing transcript shape.
    struct CaptureQuality: Codable {
        let status: String
        let affected_sources: [String]

        private init(affected_sources: [String]) {
            self.status = "incomplete"
            self.affected_sources = affected_sources
        }

        static let notice = "Some audio was interrupted or could not be captured. The transcript and notes may be incomplete."

        static func read(from metadata: [String: Any]) -> CaptureQuality? {
            let affected = ["mic", "system"].filter { source in
                guard let report = metadata["\(source)_capture"] as? [String: Any] else { return false }
                return isFalse(report["coverage_complete"]) || isFalse(report["received_audio"])
                    || isPositiveCount(report["interruption_count"]) || isPositiveCount(report["error_count"])
                    || hasError(report) || hasInterruption(report)
            }
            return affected.isEmpty ? nil : CaptureQuality(affected_sources: affected)
        }

        private static func isFalse(_ value: Any?) -> Bool {
            guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return false }
            return !number.boolValue
        }

        private static func isPositiveCount(_ value: Any?) -> Bool {
            guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                  ["c", "s", "i", "l", "q", "C", "S", "I", "L", "Q"].contains(String(cString: number.objCType))
            else { return false }
            return number.compare(0) == .orderedDescending
        }

        private static func hasText(_ value: Any?) -> Bool {
            guard let text = value as? String else { return false }
            return !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }

        private static func hasError(_ report: [String: Any]) -> Bool {
            if hasText(report["error"]) { return true }
            guard let errors = report["errors"] as? [String] else { return false }
            return errors.contains { hasText($0) }
        }

        private static func hasInterruption(_ report: [String: Any]) -> Bool {
            guard let interruptions = report["interruptions"] as? [[String: Any]] else { return false }
            return interruptions.contains { hasText($0["reason"]) }
        }
    }

    let engine: String
    let model: String
    var speaker_labels: String = "audio_source"
    let created_at: String
    let segments: [Segment]
    var speakers: [String: Speaker]? = nil
    var speaker_name_policy: String? = nil
    var speaker_identification: Identification? = nil
    var remote_microphone_owner: String? = nil
    var capture_quality: CaptureQuality? = nil

    /// Publish readable text before the canonical completion marker. If the
    /// Markdown write fails, recovery must still see an unfinished session.
    /// Each file is atomic; JSON is committed only after Markdown succeeds.
    func write(to dir: URL) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let json = try encoder.encode(self)
        let markdown = Data(rendered(title: dir.lastPathComponent).utf8)
        try Task.checkCancellation()
        try RecordingReviewGate.requireProcessingAllowed(session: dir)
        try markdown.write(to: dir.appendingPathComponent("transcript.md"), options: .atomic)
        try Task.checkCancellation()
        try RecordingReviewGate.requireProcessingAllowed(session: dir)
        try json.write(to: dir.appendingPathComponent("transcript.json"), options: .atomic)
    }

    private func rendered(title: String) -> String {
        let owner = remote_microphone_owner.flatMap { candidate in
            // One other identified voice is enough to establish that the
            // microphone is shared. Leave its unresolved speech unassigned.
            let shared = speakers?.values.contains { person in
                guard person.source == "mic", let name = person.name else { return false }
                return name.caseInsensitiveCompare(candidate) != .orderedSame
            } ?? false
            return shared ? nil : candidate
        }
        let notice: String
        if owner != nil {
            notice = "The microphone uses the recorder’s configured name unless a speaker has an identified name."
        } else {
            notice = speaker_labels == "diarized_v1"
                ? "Speaker labels are estimated from audio. Names come from explicit self-introductions; unclear speech keeps its audio-source label."
                : "Microphone and system audio identify audio sources, not individual speakers."
        }
        var lines = ["# \(title)", "", "engine: \(engine) (\(model))", "", notice, ""]
        if capture_quality != nil { lines += [CaptureQuality.notice, ""] }
        if let identification = speaker_identification, identification.status != "completed" {
            let detail = owner == nil ? " Unidentified speech remains labeled by audio source." : ""
            lines += ["Speaker detection \(identification.status).\(detail)", ""]
        }
        for seg in segments {
            let label: String
            let identity = speakers?[seg.speaker]
            let source = seg.source ?? seg.speaker
            if let name = identity?.name { label = name }
            else if let owner, ["mic", "me"].contains(source) { label = owner }
            else if let identity { label = identity.label }
            else {
                label = switch source {
                case "me", "mic": "Microphone"
                case "them", "system": "System audio"
                default: "Audio source unspecified"
                }
            }
            lines.append("**[\(Self.clock(seg.start_ms))] \(label):** \(seg.text)")
            lines.append("")
        }
        return lines.joined(separator: "\n")
    }

    private static func clock(_ ms: Int) -> String {
        let total = ms / 1000
        let h = total / 3600, m = (total % 3600) / 60, s = total % 60
        return h > 0
            ? String(format: "%d:%02d:%02d", h, m, s)
            : String(format: "%d:%02d", m, s)
    }
}
