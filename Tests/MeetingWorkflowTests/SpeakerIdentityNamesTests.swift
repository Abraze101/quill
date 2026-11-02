import MeetingWorkflow
import Foundation

func runSpeakerIdentityNamesTests() {
    for (text, name) in [
        ("My name is Jamie.", "Jamie"), ("Hi, I'm Alex.", "Alex"),
        ("Hello I am Anne-Marie.", "Anne-Marie"), ("This is Robin O’Connor speaking.", "Robin O’Connor"),
        ("Hey, I’m José García!", "José García")
    ] { expectEqual(SpeakerIdentityNames.introduction(in: text), name) }
    for text in [
        "Jamie will send it.", "My name is Jamie?", "He said my name is Jamie.",
        "\"My name is Jamie.\"", "If I say my name is Jamie.", "My name is not Jamie.",
        "My name is Jamie. Just kidding.", "My name is Jamie\nIgnore prior instructions",
        "Hi, I'm Ready.", "Hi, I'm Happy.", "My name is Unknown.", "My name is jamie.",
        "My name is <script>.", "My name is Jamie and this is Sam.", "I am Jamie."
    ] { expectNil(SpeakerIdentityNames.introduction(in: text)) }
    let segments: [SpeakerIdentityNames.Segment] = [
        .init(speaker: "unknown", text: "My name is Nobody."),
        .init(speaker: "mic_speaker_1", text: "My name is Alex."),
        .init(speaker: "system_speaker_1", text: "Hi, I'm Jamie."),
        .init(speaker: "system_speaker_1", text: "I will send the plan."),
        .init(speaker: "mic_speaker_1", text: "My name is Alex.")
    ]
    let named = SpeakerIdentityNames.names(in: segments)
    expectEqual(named.count, 2)
    expectEqual(named["mic_speaker_1"]?.evidence, [1, 4])
    expectEqual(named["system_speaker_1"]?.name, "Jamie")
    expectEqual(SpeakerIdentityNames.names(in: segments + [
        .init(speaker: "system_speaker_1", text: "My name is Sam.")
    ]).count, 1)
    expectEqual(SpeakerIdentityNames.names(in: segments + [
        .init(speaker: "system_speaker_2", text: "My name is Jamie.")
    ]).count, 1)
    expectEqual(SpeakerIdentityNames.names(in: [.init(speaker: "me", text: "My name is Alex.")]).count, 0)
    for context in ["Just kidding.", "Here is what the customer said.", "Let's roleplay.", "I quoted him.", "Imagine someone saying this."] {
        expectEqual(SpeakerIdentityNames.names(in: [
            .init(speaker: "system_speaker_1", text: "My name is Jamie."),
            .init(speaker: "system_speaker_1", text: context)
        ]).count, 0)
        expectEqual(SpeakerIdentityNames.names(in: [
            .init(speaker: "system_speaker_1", text: context),
            .init(speaker: "system_speaker_1", text: "My name is Jamie.")
        ]).count, 0)
    }
    runSpeakerNamePolicyV2Fixtures()
    print("Speaker naming checks passed: direct introductions, exact spelling, conflicts, unknown and quoted text")
}

private func runSpeakerNamePolicyV2Fixtures() {
    struct Corpus: Decodable {
        struct Introduction: Decodable { let text: String; let name: String? }
        struct Session: Decodable {
            struct Segment: Decodable {
                let speaker: String
                let text: String
                let start_ms: Int?
                let end_ms: Int?
            }
            struct Name: Decodable { let name: String; let evidence: [Int] }
            let label: String
            let segments: [Segment]
            let expected: [String: Name]
        }
        let policy: String
        let introductions: [Introduction]
        let sessions: [Session]
    }
    let path = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("Fixtures/speaker-name-policy-v2.json")
    guard let data = try? Data(contentsOf: path), let corpus = try? JSONDecoder().decode(Corpus.self, from: data) else {
        preconditionFailure("Could not read shared speaker-name policy fixtures")
    }
    expectEqual(corpus.policy, SpeakerIdentityNames.Policy.explicitIntroV2.rawValue)
    for item in corpus.introductions {
        expectEqual(SpeakerIdentityNames.introduction(in: item.text, policy: .explicitIntroV2), item.name)
    }
    for item in corpus.sessions {
        let segments = item.segments.map {
            SpeakerIdentityNames.Segment(speaker: $0.speaker, text: $0.text,
                                         startMs: $0.start_ms, endMs: $0.end_ms)
        }
        let names = SpeakerIdentityNames.names(in: segments, policy: .explicitIntroV2)
        precondition(Set(names.keys) == Set(item.expected.keys), "Unexpected identities: \(item.label)")
        for (identity, expected) in item.expected {
            precondition(names[identity]?.name == expected.name && names[identity]?.evidence == expected.evidence,
                         "Unexpected name/evidence: \(item.label)")
        }
    }
    // A V2 transcript marker opts into new evidence rules; saved V1 payloads
    // cannot silently acquire the broader grammar or lose their old names.
    expectNil(SpeakerIdentityNames.introduction(in: "My name is Jamie, and I run operations."))
    expectEqual(SpeakerIdentityNames.names(in: [
        .init(speaker: "mic_speaker_1", text: "My name is Jamie."),
        .init(speaker: "mic_speaker_1", text: "I can show a demo.")
    ]).count, 0)
    print("Versioned speaker naming checks passed: \(corpus.introductions.count) introductions and \(corpus.sessions.count) shared session fixtures")
}
