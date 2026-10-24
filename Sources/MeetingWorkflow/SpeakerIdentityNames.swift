import Foundation

/// Names stated in complete, direct introductions. This is not voiceprint
/// identification; names never come from a calendar, track, or contact list.
public enum SpeakerIdentityNames {
    public struct Segment: Sendable {
        public let speaker: String
        public let text: String
        public init(speaker: String, text: String) {
            self.speaker = speaker
            self.text = text
        }
    }

    public struct Name: Equatable, Sendable {
        public let name: String
        public let evidence: [Int]
    }

    // Keep this grammar in sync with transcript_sources.py. Only the complete
    // utterance qualifies: quotations, a question, a negation or an explanation
    // around an introduction cannot silently become a speaker identity.
    private static let token = #"[\p{Lu}\p{Lt}][\p{L}\p{M}]*(?:['’\-][\p{L}\p{M}]+)*"#
    private static let disallowed: Set<String> = [
        "a", "an", "the", "your", "you", "me", "my", "not", "no", "yes",
        "ready", "sorry", "happy", "excited", "here", "back", "new", "good",
        "speaking", "testing", "test", "unknown", "speaker", "name", "fine",
        "okay", "ok", "sure", "done", "available", "well", "late", "early",
        "tired", "sick", "busy", "honored", "delighted", "glad", "afraid"
    ]

    public static func introduction(in text: String) -> String? {
        guard text.utf8.count <= 300, !text.contains("\n"), !text.contains("\r") else { return nil }
        let value = text.trimmingCharacters(in: .whitespaces)
        let name = "(" + token + "(?: " + token + "){0,3})"
        let patterns = [
            "(?i:my name is) " + name + "[.!]?",
            "(?i:hi|hello|hey),? (?i:i am|i'm|i’m) " + name + "[.!]?",
            "(?i:this is) " + name + " (?i:speaking)[.!]?"
        ]
        for pattern in patterns {
            guard let regex = try? NSRegularExpression(pattern: "\\A" + pattern + "\\z"),
                  let match = regex.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)),
                  let range = Range(match.range(at: 1), in: value) else { continue }
            let candidate = String(value[range])
            guard candidate.count <= 60,
                  candidate.split(separator: " ").allSatisfy({
                      $0.filter(\.isLetter).count >= 2 && !disallowed.contains($0.lowercased())
                  }) else { return nil }
            return candidate
        }
        return nil
    }

    public static func names(in segments: [Segment]) -> [String: Name] {
        var candidates: [String: [String: [Int]]] = [:]
        for (index, segment) in segments.enumerated() {
            guard segment.speaker.range(of: #"\A(?:mic|system)_speaker_[1-9][0-9]*\z"#,
                                        options: .regularExpression) != nil,
                  let name = introduction(in: segment.text),
                  !hasContextWarning(around: index, in: segments) else { continue }
            candidates[segment.speaker, default: [:]][name, default: []].append(index)
        }
        var claimedBy: [String: Set<String>] = [:]
        for (speaker, names) in candidates {
            for name in names.keys { claimedBy[name.lowercased(), default: []].insert(speaker) }
        }
        var result: [String: Name] = [:]
        for (speaker, names) in candidates {
            guard names.count == 1, let (name, evidence) = names.first,
                  claimedBy[name.lowercased()]?.count == 1 else { continue }
            result[speaker] = Name(name: name, evidence: evidence)
        }
        return result
    }

    private static func hasContextWarning(around index: Int, in segments: [Segment]) -> Bool {
        // ASR sentence breaks must not hide a quoted example or immediate
        // correction. Inspect two neighboring turns from the same voice in
        // either direction, without including the introduction itself.
        let speaker = segments[index].speaker
        let before = segments[..<index].reversed().lazy.filter { $0.speaker == speaker }.prefix(2)
        let after = segments.dropFirst(index + 1).lazy.filter { $0.speaker == speaker }.prefix(2)
        let context = (Array(before) + Array(after)).map(\.text).joined(separator: " ")
        return context.range(of: #"(?i)\b(?:role.?play|pretend|imagin\w*|suppos\w*|hypothetic\w*|example|quot(?:e[ds]?|ing|ation)|script|jok(?:e|ing)|kidding|not my name|isn't my name|he said|she said|they said|customer said|said this|saying|told|act as|imitat\w*|say something like|for instance|practi[cs]\w*|rehears\w*|demo\w*|fiction\w*|character)\b"#,
                             options: .regularExpression) != nil
    }
}
