import Foundation

/// Model results contain anonymous, recording-local clusters only. Embeddings
/// never enter this interchange format or the saved transcript.
struct SpeakerInterval: Codable, Sendable {
    let speaker: String
    let start: Double
    let end: Double
    let quality: Double
}

struct LocalDiarizationResult: Codable, Sendable {
    var schema_version = 1
    let segments: [SpeakerInterval]
}

enum SpeakerAlignment {
    /// Assign at word boundaries. Any competing voice or inadequate coverage
    /// leaves a word anonymous; never assign a whole mixed sentence by vote.
    static func segments(words: [TranscriptWord], intervals: [SpeakerInterval] = []) -> [TranscriptSegment] {
        let spans = intervals.filter {
            !$0.speaker.isEmpty && $0.start.isFinite && $0.end.isFinite
                && $0.start >= 0 && $0.end > $0.start && $0.quality.isFinite
        }.sorted { $0.start == $1.start ? $0.end < $1.end : $0.start < $1.start }
        var firstPossible = 0
        var result: [TranscriptSegment] = []
        var current: [TranscriptWord] = []
        var currentSpeaker: String?

        func identity(for word: TranscriptWord) -> String? {
            guard word.start.isFinite, word.end.isFinite, word.start >= 0, word.end > word.start else { return nil }
            while firstPossible < spans.count && spans[firstPossible].end <= word.start { firstPossible += 1 }
            var candidate: String?
            var ranges: [(Double, Double)] = []
            var index = firstPossible
            while index < spans.count && spans[index].start < word.end {
                let span = spans[index]
                index += 1
                let start = max(word.start, span.start), end = min(word.end, span.end)
                guard end > start else { continue }
                guard span.quality >= 0.5 else { return nil }
                if let candidate, candidate != span.speaker { return nil }
                candidate = span.speaker
                ranges.append((start, end))
            }
            var covered = 0.0, coveredThrough = word.start
            for (start, end) in ranges {
                covered += max(0, end - max(start, coveredThrough))
                coveredThrough = max(coveredThrough, end)
            }
            let duration = word.end - word.start
            // ASR word spans can include a sentence's trailing silence. Count
            // actual voiced support instead of treating silence as another
            // speaker: short words need 60% coverage (at least 40 ms), longer
            // spans need 120 ms. More than one second without speech remains
            // ambiguous. Any other voice or low-quality span above still vetoes.
            let required = min(0.12, max(0.04, duration * 0.6))
            return covered >= required && duration - covered <= 1.0 ? candidate : nil
        }
        func flush() {
            guard let first = current.first, let last = current.last else { return }
            result.append(TranscriptSegment(start: first.start, end: last.end,
                text: current.map(\.text).joined(separator: " "), words: current, speakerID: currentSpeaker))
            current = []
        }
        for word in words {
            let speaker = identity(for: word)
            if let last = current.last,
               speaker != currentSpeaker || word.start - last.end > 1.0 { flush() }
            currentSpeaker = speaker
            current.append(word)
            if word.text.hasSuffix(".") || word.text.hasSuffix("?") || word.text.hasSuffix("!") || current.count >= 60 { flush() }
        }
        flush()
        return result
    }
}
