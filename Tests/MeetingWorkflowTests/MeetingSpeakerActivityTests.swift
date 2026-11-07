import Foundation
import MeetingWorkflow

func runMeetingSpeakerActivityChecks() throws {
    typealias Activity = MeetingSpeakerActivity
    let me = Activity.Participant(id: "p1", name: "Jamie Rivera", isSelf: true)
    let guest = Activity.Participant(id: "p2", name: "Alex Chen", isSelf: false)
    func activity(_ points: [(Int64, [String])], doc: String = "doc-A", people: [Activity.Participant]? = nil) -> Activity {
        Activity(source: "google-meet-dom-v1", documentID: doc, participants: people ?? [me, guest],
                 samples: points.map { Activity.Sample(atMS: $0.0, active: $0.1) })
    }
    func saved(_ timeline: MeetingSpeakerTimeline) -> MeetingSpeakerTimeline.Saved {
        timeline.saved { [$0] }!
    }

    reportCheckProgress("START bounded browser speaker activity")
    let valid = activity([(99_000, ["p1"]), (99_500, ["p1"])])
    precondition(valid.validated(provider: "google-meet", receivedAt: 100) != nil)
    expectNil(valid.validated(provider: "zoom-web", receivedAt: 100))
    expectNil(valid.validated(provider: "google-meet", receivedAt: .infinity))
    expectNil(activity([(79_999, ["p1"])]).validated(provider: "google-meet", receivedAt: 100))
    expectNil(activity([(101_001, ["p1"])]).validated(provider: "google-meet", receivedAt: 100))
    expectNil(activity([(99_000, ["p1"]), (99_000, ["p1"])]).validated(provider: "google-meet", receivedAt: 100))
    expectNil(activity([(99_000, ["missing"])]).validated(provider: "google-meet", receivedAt: 100))
    expectNil(activity([(99_000, ["p1", "p1"])]).validated(provider: "google-meet", receivedAt: 100))
    expectNil(activity([(99_000, ["p1"])], people: [me, me]).validated(provider: "google-meet", receivedAt: 100))
    expectNil(activity([(99_000, ["p1"])], people: [.init(id: "p1", name: "jamie@example.test", isSelf: true)])
        .validated(provider: "google-meet", receivedAt: 100))

    let context = MeetingContext(provider: "google-meet", key: "abc-defg-hij", participants: [])
    let signal = MeetingSignal(source: "tab-1", meeting: "call-1", provider: "google-meet", state: .joined,
                               timestamp: 100, meetingContext: context, speakerActivity: valid)
    let encoded = try JSONEncoder().encode(signal)
    expectEqual(try JSONDecoder().decode(MeetingSignal.self, from: encoded), signal)
    var object = try JSONSerialization.jsonObject(with: encoded) as! [String: Any]
    for malformed: Any in ["invalid", ["schema_version": 2], ["samples": true]] {
        object["speakerActivity"] = malformed
        let decoded = try JSONDecoder().decode(MeetingSignal.self, from: JSONSerialization.data(withJSONObject: object))
        expectEqual(decoded.state, .joined)
        expectEqual(decoded.meetingContext, context)
        expectNil(decoded.speakerActivity)
    }
    object.removeValue(forKey: "speakerActivity")
    expectNil(try JSONDecoder().decode(MeetingSignal.self, from: JSONSerialization.data(withJSONObject: object)).speakerActivity)
    expectNil(MeetingSignal(source: "tab-1", meeting: "call-1", provider: "google-meet", state: .left,
                            timestamp: 100, speakerActivity: valid).speakerActivity)

    var timeline = MeetingSpeakerTimeline(provider: "google-meet", key: "abc-defg-hij")
    timeline.observe(activity([(90_000, ["p1"]), (90_500, ["p1"]), (91_000, ["p1"]),
                               (91_500, []), (92_000, ["p1"]), (94_000, ["p1"]),
                               (94_500, ["p1"]), (95_000, ["p2"]), (95_500, ["p2"]),
                               (96_000, ["p2", "p1"]), (96_500, ["p1", "p2"])]), receivedAt: 100)
    var output = saved(timeline)
    expectEqual(output.intervals, [
        .init(startMS: 90_000, endMS: 91_000, active: ["doc-A:p1"]),
        .init(startMS: 94_000, endMS: 94_500, active: ["doc-A:p1"]),
        .init(startMS: 95_000, endMS: 95_500, active: ["doc-A:p2"]),
        .init(startMS: 96_000, endMS: 96_500, active: ["doc-A:p1", "doc-A:p2"])
    ])
    precondition(output.participants.first { $0.id == "doc-A:p1" }!.isSelf)
    // Repeated rolling windows cannot duplicate saved observations.
    timeline.observe(activity([(96_000, ["p1", "p2"]), (96_500, ["p1", "p2"])]), receivedAt: 100)
    expectEqual(saved(timeline), output)
    var interrupted = MeetingSpeakerTimeline(provider: "google-meet", key: "abc-defg-hij")
    interrupted.observe(activity([(99_000, ["p1"])]), receivedAt: 100)
    interrupted.breakContinuity()
    interrupted.observe(activity([(99_000, ["p1"]), (99_500, ["p1"])]), receivedAt: 100)
    precondition(saved(interrupted).intervals.isEmpty, "Unavailable evidence bridged the previous active speaker")
    // A reload establishes a new namespace and never bridges old/new endpoints.
    timeline.observe(activity([(97_000, ["p1"]), (97_500, ["p1"])], doc: "doc-B"), receivedAt: 100)
    output = saved(timeline)
    expectEqual(output.intervals.last, .init(startMS: 97_000, endMS: 97_500, active: ["doc-B:p1"]))
    expectEqual(output.participants.count, 4)
    timeline.observe(activity([(98_000, ["p1"]), (98_500, ["p1"])]), receivedAt: 100)
    expectEqual(saved(timeline), output)

    // A changed name on the same endpoint invalidates its earlier attribution.
    timeline.observe(activity([(98_000, ["p1"]), (98_500, ["p1"])], doc: "doc-B", people: [
        .init(id: "p1", name: "Different Person", isSelf: true), guest]), receivedAt: 100)
    precondition(!saved(timeline).participants.contains { $0.id == "doc-B:p1" })
    precondition(!saved(timeline).intervals.contains { $0.active.contains("doc-B:p1") })

    var bounded = MeetingSpeakerTimeline(provider: "google-meet", key: "abc-defg-hij", intervalLimit: 1)
    bounded.observe(activity([(90_000, ["p1"]), (90_500, ["p1"]), (91_000, []),
                              (92_000, ["p1"]), (92_500, ["p1"])]), receivedAt: 100)
    expectEqual(saved(bounded).intervals.count, 1)
    precondition(saved(bounded).truncated)
    var cappedPeople = MeetingSpeakerTimeline(provider: "google-meet", key: "abc-defg-hij", participantLimit: 1)
    cappedPeople.observe(valid, receivedAt: 100)
    precondition(saved(cappedPeople).truncated)
    expectEqual(saved(cappedPeople).participants.count, 1)
    // Final mapping cannot publish negative/reversed ranges or exceed the cap.
    let mapped = bounded.saved { _ in [.init(startMS: -1, endMS: 10, active: ["doc-A:p1"]),
        .init(startMS: 1, endMS: 2, active: ["doc-A:p1"]), .init(startMS: 2, endMS: 3, active: ["doc-A:p1"])] }!
    expectEqual(mapped.intervals.count, 1)
    let savedObject = try JSONSerialization.jsonObject(with: JSONEncoder().encode(mapped)) as! [String: Any]
    expectEqual(Set(savedObject.keys), Set(["schema_version", "provider", "key", "participants", "intervals", "truncated"]))
    reportCheckProgress("PASS bounded activity, continuity, reloads, conflicts, caps and optional signal decoding")
}
