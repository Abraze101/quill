import Foundation

/// Finds geometry candidates among native Chrome windows. This is a consistency
/// check for an explicit user selection, not proof of shared window identity:
/// browser and native window IDs are different namespaces.
public enum MeetingWindowMatcher {
    public struct Candidate: Equatable, Sendable {
        public let identifier: String
        public let left: Double
        public let top: Double
        public let width: Double
        public let height: Double

        public init(identifier: String, left: Double, top: Double, width: Double, height: Double) {
            self.identifier = identifier
            self.left = left
            self.top = top
            self.width = width
            self.height = height
        }
    }

    /// A small rounding allowance applies to each outer edge. Missing or
    /// ambiguous geometry must not fall back to the frontmost Chrome window.
    public static func uniqueIdentifier(for window: MeetingSignal.BrowserWindow?, candidates: [Candidate]) -> String? {
        guard let window,
              (1...2147483647).contains(window.id),
              (-131072...131072).contains(window.left),
              (-131072...131072).contains(window.top),
              (1...65536).contains(window.width),
              (1...65536).contains(window.height) else { return nil }
        let expected = [Double(window.left), Double(window.top),
                        Double(window.left) + Double(window.width),
                        Double(window.top) + Double(window.height)]
        var match: String?
        for candidate in candidates {
            guard !candidate.identifier.isEmpty,
                  candidate.width > 0, candidate.height > 0 else { continue }
            let edges = [candidate.left, candidate.top,
                         candidate.left + candidate.width, candidate.top + candidate.height]
            guard candidate.width.isFinite, candidate.height.isFinite,
                  zip(edges, expected).allSatisfy({ actual, target in
                      actual.isFinite && abs(actual - target) <= 2
                  }) else { continue }
            guard match == nil else { return nil }
            match = candidate.identifier
        }
        return match
    }
}
