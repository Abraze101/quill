import Foundation
import MeetingWorkflow

final class MeetingWindowMatcherTests {
    private let window = MeetingSignal.BrowserWindow(id: 7, left: -1200, top: 30, width: 1200, height: 800)
    private func candidate(_ id: String = "native-meeting", left: Double = -1200, top: Double = 30,
                           width: Double = 1200, height: Double = 800) -> MeetingWindowMatcher.Candidate {
        MeetingWindowMatcher.Candidate(identifier: id, left: left, top: top, width: width, height: height)
    }

    func testMissingOrInvalidBrowserBoundsNeverChooseAWindow() {
        expectNil(MeetingWindowMatcher.uniqueIdentifier(for: nil, candidates: [candidate()]))
        expectNil(MeetingWindowMatcher.uniqueIdentifier(for: window, candidates: []))
        for invalid in [
            MeetingSignal.BrowserWindow(id: 0, left: -1200, top: 30, width: 1200, height: 800),
            MeetingSignal.BrowserWindow(id: 7, left: -1200, top: 30, width: 0, height: 800),
            MeetingSignal.BrowserWindow(id: 7, left: -131073, top: 30, width: 1200, height: 800)
        ] {
            expectNil(MeetingWindowMatcher.uniqueIdentifier(for: invalid, candidates: [candidate()]))
        }
    }

    func testOnlyTheMatchingWindowIsChosenRegardlessOfOrdering() {
        let unrelated = candidate("other-chrome-window", left: 0, top: 30)
        expectEqual(MeetingWindowMatcher.uniqueIdentifier(for: window, candidates: [unrelated, candidate()]), "native-meeting")
        expectEqual(MeetingWindowMatcher.uniqueIdentifier(for: window, candidates: [candidate(), unrelated]), "native-meeting")
        expectNil(MeetingWindowMatcher.uniqueIdentifier(for: window, candidates: [unrelated]))
    }

    func testAmbiguousOverlappingWindowsAreRejected() {
        let overlapping = candidate("another-native-window", left: -1199, width: 1199)
        expectNil(MeetingWindowMatcher.uniqueIdentifier(for: window, candidates: [candidate(), overlapping]))
        expectNil(MeetingWindowMatcher.uniqueIdentifier(for: window, candidates: [candidate(), candidate()]))
    }

    func testToleranceAppliesToEachEdgeInclusively() {
        expectEqual(MeetingWindowMatcher.uniqueIdentifier(for: window, candidates: [
            candidate(left: -1198, top: 28, width: 1196, height: 804)
        ]), "native-meeting")
        for outside in [
            candidate(left: -1197.99), candidate(top: 32.01),
            candidate(width: 1202.01), candidate(height: 802.01),
            // Left and width individually differ by <=2, but the right edge
            // differs by 4. Native and browser rectangles do not match.
            candidate(left: -1198, width: 1202)
        ] {
            expectNil(MeetingWindowMatcher.uniqueIdentifier(for: window, candidates: [outside]))
        }
    }

    func testNonfiniteOrNonpositiveNativeBoundsNeverMatch() {
        for invalid in [
            candidate(left: .nan), candidate(top: .infinity), candidate(width: .infinity),
            candidate(height: -.infinity), candidate(width: 0), candidate(height: -1),
            candidate(""), candidate(left: .greatestFiniteMagnitude, width: .greatestFiniteMagnitude)
        ] {
            expectNil(MeetingWindowMatcher.uniqueIdentifier(for: window, candidates: [invalid]))
            expectEqual(MeetingWindowMatcher.uniqueIdentifier(for: window, candidates: [invalid, candidate()]), "native-meeting")
        }
    }

    func testOptionalWindowMetadataRemainsCodableAndBackwardsCompatible() throws {
        let decoder = JSONDecoder()
        let legacy = Data(#"{"source":"tab","meeting":"call","provider":"google-meet","state":"joined","timestamp":100}"#.utf8)
        let decoded = try decoder.decode(MeetingSignal.self, from: legacy)
        expectNil(decoded.browserWindow)
        expectNil(decoded.meetingContext)
        let legacyInitializer = MeetingSignal(source: "tab", meeting: "call", provider: "google-meet", state: .joined, timestamp: 100)
        expectEqual(decoded, legacyInitializer)
        let withWindow = MeetingSignal(source: "tab", meeting: "call", provider: "google-meet", state: .joined,
                                       timestamp: 100, browserWindow: window)
        expectEqual(try decoder.decode(MeetingSignal.self, from: JSONEncoder().encode(withWindow)), withWindow)
        let explicitNull = Data(#"{"source":"tab","meeting":"call","provider":"google-meet","state":"joined","timestamp":100,"browserWindow":null}"#.utf8)
        expectNil(try decoder.decode(MeetingSignal.self, from: explicitNull).browserWindow)
        let context = MeetingContext(provider: "google-meet", key: "abc-defg-hij", participants: [
            .init(name: "Jamie Smith", isSelf: false)])
        let withContext = MeetingSignal(source: "tab", meeting: "call", provider: "google-meet", state: .joined,
                                        timestamp: 100, meetingContext: context)
        expectEqual(try decoder.decode(MeetingSignal.self, from: JSONEncoder().encode(withContext)), withContext)
        expectEqual(context.validated(), context)
        let next = MeetingContext(provider: "google-meet", key: context.key, participants: [
            .init(name: "Jamie Smith", isSelf: false), .init(name: "Morgan", isSelf: true)])
        expectEqual(context.merging(next).participants.count, 2)
        let unrelated = MeetingContext(provider: "google-meet", key: "xyz-abcd-efg", participants: next.participants)
        expectEqual(context.merging(unrelated), context)
        for name in ["Guest", "someone@example.com", "https://example.com", " Jamie", "A\nB", "A\u{200B}B", "A\u{E000}B", String(repeating: "x", count: 101)] {
            expectNil(MeetingContext(provider: "google-meet", key: context.key,
                                     participants: [.init(name: name, isSelf: false)]).validated())
        }
        expectNil(MeetingContext(provider: "google-meet", key: "abc-defg-hij?authuser=1", participants: []).validated())
        expectNil(MeetingSignal(source: "tab", meeting: "call", provider: "zoom-web", state: .joined,
                                timestamp: 100, meetingContext: context).meetingContext)
        expectNil(MeetingContext(provider: "google-meet", key: context.key,
                                 participants: Array(repeating: context.participants[0], count: 51)).validated())
    }
}
