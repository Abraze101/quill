import Foundation
import MeetingWorkflow

/// Small integration surface between upstream recording and the independent
/// meeting detector. It never calls a generic toggle to stop a recording.
@MainActor
final class MeetingAutomationController {
    private var machine = MeetingAutomation()
    private let store = MeetingSignalStore()
    private var timer: Timer?
    private var previousStatus: MeetingAutomation.Status = .disabled
    private let skipFile = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".local/state/quill/meeting-skips.json")
    private var savedSuppression = MeetingAutomation.Suppression()

    var onStart: ((String) -> Void)?
    var onStop: ((String) -> Void)?
    var onStatus: ((MeetingAutomation.Status) -> Void)?

    var status: MeetingAutomation.Status { machine.status }
    var deliberatelySkipped: Set<String> {
        machine.suppression.skipped.subtracting(machine.suppression.failedStarts)
    }
    func hasFailedStart(meeting: String) -> Bool { machine.suppression.failedStarts.contains(meeting) }
    var failedMeetingToRetry: String? {
        guard Config.meetingAutomationEnabled() else { return nil }
        return machine.failedStartToRetry(now: Date().timeIntervalSince1970)
    }
    var canSkipMeeting: Bool {
        if !joinedSignals().isEmpty { return true }
        if case .meeting = machine.recording?.owner { return true }
        return false
    }

    func joinedSignals() -> [MeetingSignal] {
        let now = Date().timeIntervalSince1970
        return store.read(now: now).filter { $0.state == .joined && now - $0.timestamp <= 20 }
            .sorted { $0.timestamp > $1.timestamp }
    }

    func reminderSignals() -> [MeetingSignal] { store.read() }

    func signal(for meeting: String) -> MeetingSignal? {
        joinedSignals().first { $0.meeting == meeting }
    }

    func windowContinuity(meeting: String, source: String, windowID: Int) -> MeetingWindowContinuity.Decision {
        let now = Date().timeIntervalSince1970
        return MeetingWindowContinuity.evaluate(signals: store.read(now: now), meeting: meeting,
                                                source: source, windowID: windowID, now: now)
    }

    func mayStart(meeting: String) -> Bool {
        Config.meetingAutomationEnabled() && !machine.skipped.contains(meeting) && signal(for: meeting) != nil
    }

    func startFailed(meeting: String, retryable: Bool = true) {
        machine.startFailed(meeting: meeting, retryable: retryable)
        saveSkips()
    }

    func retryFailedStart(meeting: String) {
        guard let onStart else { return }
        let now = Date().timeIntervalSince1970
        machine.enabled = Config.meetingAutomationEnabled()
        for signal in store.read(now: now) { machine.ingest(signal, now: now) }
        guard case .start(let selected) = machine.retryFailedStart(meeting: meeting, now: now) else { return }
        saveSkips()
        onStart(selected)
    }

    init() {
        if let data = try? Data(contentsOf: skipFile), data.count < 100_000,
           let saved = try? JSONDecoder().decode(MeetingAutomation.Suppression.self, from: data) {
            machine = MeetingAutomation(skipped: saved.skipped, failedStarts: saved.failedStarts)
            savedSuppression = saved
        }
        timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.poll() }
        }
        if let timer { RunLoop.main.add(timer, forMode: .common) }
    }

    func recordingStarted(id: String, meeting: String?) {
        machine.didStart(id: id, owner: meeting.map { .meeting($0) } ?? .manual)
    }

    func recordingStopped(id: String, manually: Bool) {
        let now = Date().timeIntervalSince1970
        if manually {
            for signal in store.read(now: now) { machine.ingest(signal, now: now) }
        }
        machine.didStop(id: id, manually: manually, now: now)
        saveSkips()
    }

    func skipCurrent() {
        let now = Date().timeIntervalSince1970
        for signal in store.read(now: now) { machine.ingest(signal, now: now) }
        if case .stop(let id) = machine.skipCurrent(now: Date().timeIntervalSince1970) { onStop?(id) }
        saveSkips()
    }

    func skip(meeting: String) {
        let now = Date().timeIntervalSince1970
        for signal in store.read(now: now) { machine.ingest(signal, now: now) }
        if case .stop(let id) = machine.skip(meeting: meeting, now: now) { onStop?(id) }
        saveSkips()
    }

    func stopPolling() { timer?.invalidate(); timer = nil }

    private func poll() {
        let now = Date().timeIntervalSince1970
        machine.enabled = Config.meetingAutomationEnabled()
        // Keep tracking an owned recording after auto-start is switched off,
        // so a subsequent leave still stops that same recording cleanly.
        for signal in store.read(now: now) { machine.ingest(signal, now: now) }
        switch machine.tick(now: now) {
        case .start(let meeting):
            if let onStart { onStart(meeting) }
            else { machine.startFailed(meeting: meeting) }
        case .stop(let id): onStop?(id)
        case nil: break
        }
        saveSkips()
        if machine.status != previousStatus {
            previousStatus = machine.status
            onStatus?(machine.status)
            if machine.status == .signalLost {
                notifyUser(title: "quill — meeting state unavailable",
                           body: "Recording is still running. Use Stop recording if the meeting has ended.")
            }
        }
    }

    private func saveSkips() {
        guard machine.suppression != savedSuppression else { return }
        do {
            try FileManager.default.createDirectory(at: skipFile.deletingLastPathComponent(), withIntermediateDirectories: true)
            try JSONEncoder().encode(machine.suppression).write(to: skipFile, options: .atomic)
            savedSuppression = machine.suppression
        } catch {
            FileHandle.standardError.write(Data("quill: could not persist meeting skips\n".utf8))
        }
    }
}
