import Foundation

/// Reads the native browser bridge's bounded, atomic signal files. No browser
/// history, meeting chat, title, attendee list, or media passes through here.
public struct MeetingSignalStore: Sendable {
    public let directory: URL

    public init(directory: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".local/state/quill/meeting-signals")) {
        self.directory = directory
    }

    public func read(now: TimeInterval = Date().timeIntervalSince1970) -> [MeetingSignal] {
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: [.isSymbolicLinkKey, .isRegularFileKey, .fileSizeKey, .contentModificationDateKey]
        ) else { return [] }
        return files.compactMap { file in
            guard file.pathExtension == "json",
                  let values = try? file.resourceValues(forKeys: [.isSymbolicLinkKey, .isRegularFileKey, .fileSizeKey, .contentModificationDateKey]),
                  values.isSymbolicLink != true, values.isRegularFile == true,
                  (values.fileSize ?? Int.max) < 4096,
                  let modified = values.contentModificationDate,
                  now - modified.timeIntervalSince1970 < 120,
                  let bytes = try? Data(contentsOf: file),
                  let signal = try? JSONDecoder().decode(MeetingSignal.self, from: bytes)
            else { return nil }
            return signal
        }.sorted { $0.timestamp < $1.timestamp }
    }
}
