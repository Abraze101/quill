import MeetingWorkflow

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
    print("Speaker naming checks passed: direct introductions, exact spelling, conflicts, unknown and quoted text")
}
