import AVFoundation
import FluidAudio
import Foundation

/// Parakeet TDT 0.6B v2 (English) via FluidAudio's Core ML port. Models
/// download once into FluidAudio's managed cache (~600 MB); after that,
/// transcription runs entirely on-device at roughly 20 seconds per hour of
/// audio on Apple Silicon.
actor ParakeetEngine: TranscriptionEngine {
    enum EngineError: Error, CustomStringConvertible {
        case notPrepared
        case unreadableAudio(URL, Error?)

        var description: String {
            switch self {
            case .notPrepared: return "parakeet engine used before prepare()"
            case .unreadableAudio(let url, let e):
                return "unreadable or empty audio \(url.lastPathComponent)"
                    + (e.map { ": \($0)" } ?? "")
            }
        }
    }

    nonisolated let name = "parakeet"
    nonisolated let model = "parakeet-tdt-0.6b-v2-coreml"

    private var manager: AsrManager?
    private var speakerStatus: SpeakerIdentificationState = .disabled
    private var speakerDiagnostic: String?

    func prepare() async throws {
        guard manager == nil else { return }
        let models = try await AsrModels.downloadAndLoad(version: .v2)
        let manager = AsrManager()
        try await manager.loadModels(models)
        self.manager = manager
    }

    func transcribe(_ audio: URL) async throws -> [TranscriptSegment] {
        try await transcribe(audio, identifySpeakers: false)
    }

    func speakerIdentificationStatus() async -> SpeakerIdentificationState { speakerStatus }
    func speakerIdentificationDiagnostic() async -> String? { speakerDiagnostic }

    func transcribe(_ audio: URL, identifySpeakers: Bool) async throws -> [TranscriptSegment] {
        guard let manager else { throw EngineError.notPrepared }
        speakerStatus = identifySpeakers ? .unavailable : .disabled
        speakerDiagnostic = nil

        // A track with no frames (recorder died before its first buffer)
        // makes AVFoundation raise an ObjC exception deep inside the
        // resampler — uncatchable from Swift, so it takes the whole daemon
        // down. Check readability up front instead.
        do {
            let probe = try AVAudioFile(forReading: audio)
            guard probe.length > 0 else { throw EngineError.unreadableAudio(audio, nil) }
        } catch let error as EngineError {
            throw error
        } catch {
            throw EngineError.unreadableAudio(audio, error)
        }

        var state = try TdtDecoderState()
        let result = try await manager.transcribe(audio, decoderState: &state)
        try Task.checkCancellation()

        let words = buildWordTimings(from: result.tokenTimings ?? []).map {
            TranscriptWord(start: $0.startTime, end: $0.endTime, text: $0.word)
        }
        guard !words.isEmpty, words.allSatisfy({ $0.start.isFinite && $0.end.isFinite && $0.start >= 0 && $0.end >= $0.start }) else {
            if identifySpeakers { speakerDiagnostic = "unavailable: missing_or_invalid_word_timings; words=\(words.count)" }
            let text = result.text.trimmingCharacters(in: .whitespacesAndNewlines)
            return text.isEmpty
                ? []
                : [TranscriptSegment(start: 0, end: result.duration.isFinite ? max(0, result.duration) : 0, text: text)]
        }
        guard identifySpeakers else { return SpeakerAlignment.segments(words: words) }
        do {
            let intervals = try await LocalDiarizationProcess.run(audio: audio)
            try Task.checkCancellation()
            let segments = SpeakerAlignment.segments(words: words, intervals: intervals)
            let identified = segments.filter { $0.speakerID != nil }.count
            speakerStatus = identified == 0 ? .unavailable : (identified == segments.count ? .completed : .partial)
            let supportedWords = segments.filter { $0.speakerID != nil }.reduce(0) { $0 + ($1.words?.count ?? 0) }
            let clusters = Set(segments.compactMap(\.speakerID)).count
            speakerDiagnostic = "\(speakerStatus.rawValue): \(identified == 0 ? "no_supported_alignment" : "aligned"); "
                + "words=\(words.count); supported_words=\(supportedWords); intervals=\(intervals.count); clusters=\(clusters)"
            return segments
        } catch {
            // Cancellation is never downgraded to a successful transcript.
            try Task.checkCancellation()
            speakerStatus = .unavailable
            let reason: String
            switch error {
            case LocalDiarizationProcess.Failure.timedOut: reason = "child_timeout"
            case LocalDiarizationProcess.Failure.invalidOutput, is DecodingError: reason = "invalid_child_output"
            default: reason = "child_unavailable"
            }
            speakerDiagnostic = "unavailable: \(reason); words=\(words.count)"
            return SpeakerAlignment.segments(words: words)
        }
    }

    func release() async {
        if let manager { await manager.cleanup() }
        manager = nil
        speakerStatus = .disabled
        speakerDiagnostic = nil
    }
}
