import Foundation

/// Names stated in complete, direct introductions. This is not voiceprint
/// identification; names never come from a calendar, track, or contact list.
public enum SpeakerIdentityNames {
    public enum Policy: String, Sendable {
        case legacy
        case explicitIntroV2 = "explicit_intro_v2"
    }

    public struct Segment: Sendable {
        public let speaker: String
        public let text: String
        public let startMs: Int?
        public let endMs: Int?
        public init(speaker: String, text: String, startMs: Int? = nil, endMs: Int? = nil) {
            self.speaker = speaker
            self.text = text
            self.startMs = startMs
            self.endMs = endMs
        }
    }

    public struct Name: Equatable, Sendable {
        public let name: String
        public let evidence: [Int]
    }

    // Keep both versioned grammars in sync with transcript_sources.py. Existing
    // transcripts continue to use the original whole-utterance grammar.
    private static let token = #"[\p{Lu}\p{Lt}][\p{L}\p{M}]*(?:['’\-][\p{L}\p{M}]+)*"#
    private static let disallowed: Set<String> = [
        "a", "an", "the", "your", "you", "me", "my", "not", "no", "yes",
        "ready", "sorry", "happy", "excited", "here", "back", "new", "good",
        "speaking", "testing", "test", "unknown", "speaker", "name", "fine",
        "okay", "ok", "sure", "done", "available", "well", "late", "early",
        "tired", "sick", "busy", "honored", "delighted", "glad", "afraid"
    ]

    public static func introduction(in text: String, policy: Policy = .legacy) -> String? {
        guard text.utf8.count <= 300, !text.contains("\n"), !text.contains("\r") else { return nil }
        let value = text.trimmingCharacters(in: .whitespaces)
        if policy == .explicitIntroV2,
           value.contains("\"") || value.contains("“") || value.contains("”")
            || matches(correctionWarning, in: value)
            || matches(setupWarning, in: String(value.prefix { !".?!".contains($0) })) { return nil }
        let name = "(" + token + "(?: " + token + "){0,3})"
        let patterns = [
            "(?i:my name is) " + name,
            "(?i:hi|hello|hey),? (?i:i am|i'm|i’m) " + name,
            "(?i:this is) " + name + " (?i:speaking)"
        ]
        for pattern in patterns {
            // Legacy documents retain their original whole-utterance grammar.
            // V2 also permits an explicit introduction followed by a clause.
            let body = pattern + (policy == .legacy ? "[.!]?"
                : #"(?:[.!](?:\s+.*)?|,\s+.*| (?i:from|with|and I)\s+.*)?"#)
            guard let regex = try? NSRegularExpression(pattern: "\\A" + body + "\\z"),
                  let match = regex.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)),
                  let range = Range(match.range(at: 1), in: value) else { continue }
            let candidate = String(value[range])
            guard candidate.count <= 60,
                  candidate.split(separator: " ").allSatisfy({
                      $0.filter(\.isLetter).count >= 2 && !disallowed.contains($0.lowercased())
                  }) else { return nil }
            if policy == .explicitIntroV2 {
                let remainder = String(value[range.upperBound...])
                if matches(#"(?i)\b(?:my name is|(?:hi|hello|hey),? (?:i am|i'm|i’m)|this is .{1,80} speaking)\b"#,
                           in: remainder) { return nil }
            }
            return candidate
        }
        return nil
    }

    public static func names(in segments: [Segment], policy: Policy = .legacy) -> [String: Name] {
        var candidates: [String: [String: [Int]]] = [:]
        for (index, segment) in segments.enumerated() {
            guard segment.speaker.range(of: #"\A(?:mic|system)_speaker_[1-9][0-9]*\z"#,
                                        options: .regularExpression) != nil else { continue }
            if policy == .legacy {
                guard let name = introduction(in: segment.text),
                      !hasContextWarning(around: index, in: segments) else { continue }
                candidates[segment.speaker, default: [:]][name, default: []].append(index)
            } else if let match = introduction(startingAt: index, in: segments),
                      !hasV2ContextWarning(around: match.evidence, in: segments) {
                candidates[segment.speaker, default: [:]][match.name, default: []].append(contentsOf: match.evidence)
            }
        }
        var claimedBy: [String: Set<String>] = [:]
        for (speaker, names) in candidates {
            for name in names.keys { claimedBy[name.lowercased(), default: []].insert(speaker) }
        }
        var result: [String: Name] = [:]
        for (speaker, names) in candidates {
            guard names.count == 1, let (name, evidence) = names.first,
                  claimedBy[name.lowercased()]?.count == 1 else { continue }
            result[speaker] = Name(name: name, evidence: Array(Set(evidence)).sorted())
        }
        return result
    }

    // These phrases identify a setup for quoted/example speech or an explicit
    // correction. Ordinary discussion of a demo, example, or what someone was
    // told does not invalidate an otherwise direct introduction.
    private static let setupWarning = #"(?i)\b(?:role[ -]?play(?:ing)?|pretend(?:ing)?|(?:let['’]s|let us) (?:imagine|rehearse)|imagine (?:someone|me|you|us) (?:say(?:ing)?|introduc\w*)|(?:he|she|they|(?:the )?customer) (?:said|says)|say something like|said (?:he|she|they|(?:the )?customer|(?-i:[A-Z][a-z]+))|for (?:example|instance)|for the purpose of (?:this|an) example|as (?:an example|a joke)|hypothetically|act(?:ing)? as|(?:a|the|this|that) (?:quote|script)|quot(?:ed|ing)|rehears(?:e|ing)|(?:demo|demonstrat\w*) (?:how|what) (?:to|you|I|we) (?:say|introduce))\b"#
    private static let correctionWarning = #"(?i)\b(?:just (?:kidding|joking)|not really|(?:i (?:am|was)|i['’]m) (?:joking|kidding|pretending)|not my name|isn['’]t my name|(?:that|this|it)(?: (?:was|is)|['’]s) (?:only |just )?(?:a |an )?(?:joke|example|quote|script|role[ -]?play|fiction)|(?:my )?real name is|i misspoke|(?:that|this) (?:name|introduction) (?:was|is) (?:wrong|incorrect|fictional))\b"#

    private static func matches(_ pattern: String, in text: String) -> Bool {
        text.range(of: pattern, options: .regularExpression) != nil
    }

    private static func introduction(startingAt index: Int, in segments: [Segment]) -> Name? {
        let first = segments[index]
        var text = first.text.trimmingCharacters(in: .whitespaces)
        var evidence = [index]
        if let name = introduction(in: text, policy: .explicitIntroV2) {
            return Name(name: name, evidence: evidence)
        }
        // Only adjacent, timed fragments from the same identified voice may be
        // joined. An anonymous word or another voice always breaks the chain.
        for nextIndex in (index + 1)..<min(segments.count, index + 3) {
            let previous = segments[nextIndex - 1], next = segments[nextIndex]
            guard next.speaker == first.speaker,
                  !previous.text.trimmingCharacters(in: .whitespaces).hasSuffix("."),
                  !previous.text.trimmingCharacters(in: .whitespaces).hasSuffix("?"),
                  !previous.text.trimmingCharacters(in: .whitespaces).hasSuffix("!"),
                  let firstStart = first.startMs, let previousEnd = previous.endMs,
                  let nextStart = next.startMs, let nextEnd = next.endMs,
                  firstStart >= 0, previousEnd >= firstStart, nextStart >= previousEnd,
                  nextStart - previousEnd <= 1_500, nextEnd >= nextStart,
                  nextEnd - firstStart <= 10_000 else { break }
            text += " " + next.text.trimmingCharacters(in: .whitespaces)
            evidence.append(nextIndex)
            if let name = introduction(in: text, policy: .explicitIntroV2) {
                return Name(name: name, evidence: evidence)
            }
        }
        return nil
    }

    private static func hasV2ContextWarning(around evidence: [Int], in segments: [Segment]) -> Bool {
        guard let firstIndex = evidence.first, let lastIndex = evidence.last else { return true }
        let first = segments[firstIndex], last = segments[lastIndex]
        func nearby(_ segment: Segment, before: Bool) -> Bool {
            if before, let end = segment.endMs, let start = first.startMs {
                return start >= end && start - end <= 15_000
            }
            if !before, let start = segment.startMs, let end = last.endMs {
                return start >= end && start - end <= 15_000
            }
            // Untimed callers keep conservative neighboring-turn checks.
            return true
        }
        let before = segments[..<firstIndex].reversed().lazy
            .filter { $0.speaker == first.speaker }.prefix(2)
            .filter { nearby($0, before: true) }.map(\.text).joined(separator: " ")
        let after = segments.dropFirst(lastIndex + 1).lazy
            .filter { $0.speaker == first.speaker }.prefix(2)
            .filter { nearby($0, before: false) }.map(\.text).joined(separator: " ")
        return matches(setupWarning, in: before) || matches(correctionWarning, in: after)
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
