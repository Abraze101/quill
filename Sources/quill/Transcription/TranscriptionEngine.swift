import Foundation

/// One timed span of recognized speech from a single track, relative to that
/// track's own start.
struct TranscriptSegment: Sendable {
    let start: TimeInterval
    let end: TimeInterval
    let text: String
    var words: [TranscriptWord]? = nil
    var speakerID: String? = nil
}

struct TranscriptWord: Sendable {
    let start: TimeInterval
    let end: TimeInterval
    let text: String
}

enum SpeakerIdentificationState: String, Sendable { case disabled, completed, partial, unavailable }

/// A speech-to-text engine quill can run locally. Engines are prepared lazily
/// (model download + load) when the transcription queue has work and released
/// when it drains, so quill never idles holding gigabytes of model weights.
protocol TranscriptionEngine: Sendable {
    /// Short engine identifier recorded as transcript.json provenance.
    var name: String { get }
    /// Concrete model identifier recorded as transcript.json provenance.
    var model: String { get }
    func prepare() async throws
    func transcribe(_ audio: URL) async throws -> [TranscriptSegment]
    func transcribe(_ audio: URL, identifySpeakers: Bool) async throws -> [TranscriptSegment]
    func speakerIdentificationStatus() async -> SpeakerIdentificationState
    func release() async
}

extension TranscriptionEngine {
    func transcribe(_ audio: URL, identifySpeakers: Bool) async throws -> [TranscriptSegment] {
        try await transcribe(audio)
    }
    func speakerIdentificationStatus() async -> SpeakerIdentificationState { .disabled }
}
