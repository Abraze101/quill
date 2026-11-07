import Foundation

/// A short browser-owned window of visible active-speaker observations. This
/// evidence identifies a meeting endpoint, never every voice using its mic.
public struct MeetingSpeakerActivity: Codable, Equatable, Sendable {
    public struct Participant: Codable, Equatable, Sendable {
        public let id: String
        public let name: String
        public let isSelf: Bool
        enum CodingKeys: String, CodingKey { case id, name; case isSelf = "self" }
        public init(id: String, name: String, isSelf: Bool) {
            self.id = id; self.name = name; self.isSelf = isSelf
        }
    }
    public struct Sample: Codable, Equatable, Sendable {
        public let atMS: Int64
        public let active: [String]
        enum CodingKeys: String, CodingKey { case atMS = "at_ms", active }
        public init(atMS: Int64, active: [String]) { self.atMS = atMS; self.active = active }
    }
    public let schemaVersion: Int
    public let source: String
    public let documentID: String
    public let participants: [Participant]
    public let samples: [Sample]
    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version", source, documentID = "document_id", participants, samples
    }
    public init(source: String, documentID: String, participants: [Participant], samples: [Sample]) {
        schemaVersion = 1; self.source = source; self.documentID = documentID
        self.participants = participants; self.samples = samples
    }

    public func validated(provider: String, receivedAt: TimeInterval) -> Self? {
        let expectedSource = ["google-meet": "google-meet-dom-v1", "zoom-web": "zoom-web-dom-v1"][provider]
        guard schemaVersion == 1, source == expectedSource, receivedAt.isFinite,
              receivedAt > 0, receivedAt < 10_000_000_000,
              documentID.range(of: #"\A[A-Za-z0-9-]{1,64}\z"#, options: .regularExpression) != nil,
              participants.count <= 20, samples.count <= 32 else { return nil }
        let context = MeetingContext(provider: provider,
            key: provider == "google-meet" ? "abc-defg-hij" : "123456789",
            participants: participants.map { .init(name: $0.name, isSelf: $0.isSelf) })
        guard context.validated() != nil else { return nil }
        var ids = Set<String>()
        for person in participants {
            guard person.id.range(of: #"\A[A-Za-z0-9_-]{1,64}\z"#, options: .regularExpression) != nil,
                  ids.insert(person.id).inserted else { return nil }
        }
        var previous: Int64?
        for sample in samples {
            let timestamp = Double(sample.atMS) / 1000
            guard timestamp >= receivedAt - 20, timestamp <= receivedAt + 1,
                  previous == nil || sample.atMS > previous!, sample.active.count <= 4,
                  Set(sample.active).count == sample.active.count,
                  Set(sample.active).isSubset(of: ids) else { return nil }
            previous = sample.atMS
        }
        return self
    }
}

/// Collects only observed continuity. Missing observations, changing speakers,
/// document replacements and silence never inherit the last speaking label.
public struct MeetingSpeakerTimeline: Sendable {
    public struct Interval: Codable, Equatable, Sendable {
        public let startMS: Int64
        public let endMS: Int64
        public let active: [String]
        enum CodingKeys: String, CodingKey { case startMS = "start_ms", endMS = "end_ms", active }
        public init(startMS: Int64, endMS: Int64, active: [String]) {
            self.startMS = startMS; self.endMS = endMS; self.active = active
        }
    }
    public struct Saved: Codable, Equatable, Sendable {
        public let schemaVersion: Int
        public let provider: String
        public let key: String
        public let participants: [MeetingSpeakerActivity.Participant]
        public let intervals: [Interval]
        public let truncated: Bool
        enum CodingKeys: String, CodingKey {
            case schemaVersion = "schema_version", provider, key, participants, intervals, truncated
        }
    }
    public let provider: String
    public let key: String
    private let participantLimit: Int
    private let intervalLimit: Int
    private var people: [String: MeetingSpeakerActivity.Participant] = [:]
    private var invalidPeople = Set<String>()
    private var intervals: [Interval] = []
    private var lastSample: MeetingSpeakerActivity.Sample?
    private var currentDocument: String?
    private var retiredDocuments = Set<String>()
    private var latestMS: Int64?
    public private(set) var truncated = false

    public init(provider: String, key: String, participantLimit: Int = 100, intervalLimit: Int = 20_000) {
        self.provider = provider; self.key = key
        self.participantLimit = min(100, max(0, participantLimit))
        self.intervalLimit = min(20_000, max(0, intervalLimit))
    }

    public mutating func observe(_ candidate: MeetingSpeakerActivity, receivedAt: TimeInterval) {
        guard !truncated, let activity = candidate.validated(provider: provider, receivedAt: receivedAt),
              !retiredDocuments.contains(activity.documentID) else { return }
        if currentDocument != activity.documentID {
            guard retiredDocuments.count < 100 else { truncated = true; return }
            if let currentDocument { retiredDocuments.insert(currentDocument) }
            currentDocument = activity.documentID
            lastSample = nil
        }
        func identifier(_ id: String) -> String { activity.documentID + ":" + id }
        for person in activity.participants {
            let id = identifier(person.id)
            let value = MeetingSpeakerActivity.Participant(id: id, name: person.name, isSelf: person.isSelf)
            if let previous = people[id], previous != value {
                // A reused endpoint with conflicting identity is not silently
                // renamed across all of its earlier speech.
                invalidPeople.insert(id)
                intervals.removeAll { $0.active.contains(id) }
            } else if people[id] == nil {
                guard people.count < participantLimit else { truncated = true; return }
                people[id] = value
            }
        }
        for sample in activity.samples {
            guard latestMS == nil || sample.atMS > latestMS! else { continue }
            let active = sample.active.map(identifier).sorted()
            let current = MeetingSpeakerActivity.Sample(atMS: sample.atMS, active: active)
            defer { lastSample = current; latestMS = sample.atMS }
            guard !active.isEmpty, active.allSatisfy({ !invalidPeople.contains($0) }),
                  let previous = lastSample, previous.active == active,
                  sample.atMS - previous.atMS <= 1500 else { continue }
            let interval = Interval(startMS: previous.atMS, endMS: sample.atMS, active: active)
            if let last = intervals.last, last.endMS == interval.startMS, last.active == active {
                intervals[intervals.count - 1] = Interval(startMS: last.startMS, endMS: interval.endMS, active: active)
            } else {
                guard intervals.count < intervalLimit else { truncated = true; return }
                intervals.append(interval)
            }
        }
    }

    public mutating func breakContinuity() { lastSample = nil }
    public mutating func stopCollecting() { truncated = true; lastSample = nil }

    /// The recording's edit clock maps wall intervals to audio-relative output
    /// intervals, removing startup delay and paused sections before publication.
    public func saved(mapping: (Interval) -> [Interval]) -> Saved? {
        let participants = people.values.filter { !invalidPeople.contains($0.id) }.sorted { $0.id < $1.id }
        guard !participants.isEmpty else { return nil }
        var result: [Interval] = []
        var capped = truncated
        for interval in intervals {
            for mapped in mapping(interval) where mapped.startMS >= 0 && mapped.endMS > mapped.startMS {
                guard result.count < intervalLimit else { capped = true; break }
                result.append(mapped)
            }
            if capped && result.count >= intervalLimit { break }
        }
        return Saved(schemaVersion: 1, provider: provider, key: key,
                     participants: participants, intervals: result, truncated: capped)
    }
}
