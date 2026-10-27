import AppKit
import Foundation
import MeetingWorkflow

// No capture, app windows, preferences, permissions, process execution, or user
// notifications. The coordinator, recording session, notification adapter, and
// MeetingWorkflow state machines under test are the production implementations.
@MainActor
enum Fixture {
    static var root = FileManager.default.temporaryDirectory
    static var center = NotificationCenter()
    static var startGate = Gate(open: true)
    static var stopGate = Gate(open: true)
    static var screenEnabled = false
    static var captureCatalogUnavailable = false
    static var automationEnabled = true
    static var screenPauseGate = Gate(open: true)
    static var screenResumeGate = Gate(open: true)
    static var screenRecorders: [ScreenVideoRecorder] = []
    static var micStarts = 0
    static var micStops = 0
    static var systemStarts = 0
    static var systemStops = 0
    static var systemStopRequests = 0
    static var systemRecorders: [SystemAudioRecorder] = []
    static var enqueued: [URL] = []
    static var registered: [URL] = []
    static var notices: [String] = []
    static var transcriptionEnabled = true
    static var retryGate = Gate(open: true)
    static var retryCalls = 0
    static var retryResult = TranscriptionCoordinator.RetryResult.queued(1)
    static var allowInformationAlerts = false
    static var informationAlerts: [String] = []
    static var micCaptureOrigin: Double?
    static var systemCaptureOrigin: Double?

    static func reset(root: URL) {
        self.root = root
        center = NotificationCenter()
        startGate = Gate(open: true)
        stopGate = Gate(open: true)
        screenEnabled = false; captureCatalogUnavailable = false; automationEnabled = true
        screenPauseGate = Gate(open: true); screenResumeGate = Gate(open: true)
        screenRecorders = []
        micStarts = 0; micStops = 0; systemStarts = 0; systemStops = 0
        systemStopRequests = 0; systemRecorders = []
        enqueued = []; registered = []; notices = []
        transcriptionEnabled = true; retryGate = Gate(open: true); retryCalls = 0
        retryResult = .queued(1); allowInformationAlerts = false; informationAlerts = []
        micCaptureOrigin = nil; systemCaptureOrigin = nil
    }
}

@MainActor
final class Gate {
    var open: Bool
    private var waiters: [CheckedContinuation<Void, Never>] = []
    init(open: Bool) { self.open = open }
    func wait() async {
        if open { return }
        await withCheckedContinuation { waiters.append($0) }
    }
    func release() {
        open = true
        let pending = waiters
        waiters = []
        for waiter in pending { waiter.resume() }
    }
}

@MainActor
final class MicRecorder {
    var captureTimeline: RecordingCaptureTimeline?
    var onStatusChange: (@MainActor @Sendable (MicCaptureStatus) -> Void)?
    var firstBufferAt: Date? = nil
    var captureReport: [String: Any] {
        var report: [String: Any] = ["coverage_complete": true]
        if let origin = Fixture.micCaptureOrigin { report["capture_start_offset_seconds"] = origin }
        return report
    }
    func start(writingTo: URL) throws { Fixture.micStarts += 1 }
    func stop() { Fixture.micStops += 1 }
}

@MainActor
final class SystemAudioRecorder {
    var captureTimeline: RecordingCaptureTimeline?
    var onStatusChange: (@MainActor @Sendable (SystemAudioCaptureStatus) -> Void)?
    var captureFailure: String?
    var hasCapturedAudio = true
    var coverageComplete = true
    private(set) var stopRequested = false
    var firstBufferAt: Date? { nil }
    var captureReport: [String: Any] {
        var report: [String: Any] = ["received_audio": hasCapturedAudio, "coverage_complete": coverageComplete]
        if let origin = Fixture.systemCaptureOrigin { report["capture_start_offset_seconds"] = origin }
        return report
    }
    init() { Fixture.systemRecorders.append(self) }

    // Deliberately allow stale delivery: the real coordinator must reject a
    // queued callback after Stop or after another session becomes current.
    func emit(_ status: SystemAudioCaptureStatus) {
        switch status {
        case .recording: hasCapturedAudio = true; captureFailure = nil
        case .recovering: coverageComplete = false
        case .failed(let message): coverageComplete = false; captureFailure = message
        }
        onStatusChange?(status)
    }
    func start(writingTo: URL) async throws {
        Fixture.systemStarts += 1
        await Fixture.startGate.wait()
    }
    func requestStop() {
        guard !stopRequested else { return }
        stopRequested = true
        Fixture.systemStopRequests += 1
    }
    func stop() async {
        Fixture.systemStops += 1
        await Fixture.stopGate.wait()
    }
}

@MainActor
final class FakeApplication {
    func setActivationPolicy(_ policy: NSApplication.ActivationPolicy) {}
    func unhideWithoutActivation() {}
    func activate(ignoringOtherApps: Bool) { precondition(Fixture.allowInformationAlerts, "Unexpected app activation") }
    func terminate(_ sender: Any?) { preconditionFailure("Unexpected app termination") }
}
@MainActor let NSApp = FakeApplication()

@MainActor
final class NSAlert {
    var messageText = ""
    var informativeText = ""
    func addButton(withTitle: String) {}
    func beginSheetModal(for window: NSWindow) { record() }
    func runModal() { record() }
    private func record() {
        precondition(Fixture.allowInformationAlerts, "Unexpected alert")
        Fixture.informationAlerts.append(messageText + ": " + informativeText)
    }
}

@MainActor
enum NSWorkspace {
    static let shared = Workspace()
    static let willSleepNotification = Notification.Name("test-will-sleep")
    static let didWakeNotification = Notification.Name("test-did-wake")
    static let screensDidSleepNotification = Notification.Name("test-display-sleep")
    static let screensDidWakeNotification = Notification.Name("test-display-wake")
    @MainActor final class Workspace {
        var notificationCenter: NotificationCenter { Fixture.center }
        func activateFileViewerSelecting(_ files: [URL]) { preconditionFailure("Unexpected Finder request") }
        func open(_ url: URL) { preconditionFailure("Unexpected URL open") }
    }
}

// Intercept defaults so even an accidental recovery decision cannot write the
// current user's Quill preferences from the test executable.
@MainActor
final class UserDefaults {
    static let standard = UserDefaults()
    func stringArray(forKey: String) -> [String]? { nil }
    func set(_ value: Any?, forKey: String) { preconditionFailure("Unexpected preference write") }
}

@MainActor
func notifyUser(title: String, body: String) { Fixture.notices.append(title + ": " + body) }

struct QuillLoginItemState {}
@MainActor
final class LoginItemController {
    func snapshot() -> QuillLoginItemState { .init() }
    func setEnabled(_ enabled: Bool) { preconditionFailure("Unexpected login change") }
    func openSystemSettings() { preconditionFailure("Unexpected settings open") }
}

@MainActor
final class MenuBarController {
    var onToggle: (() -> Void)?
    var onOpenFolder: (() -> Void)?
    var onQuit: (() -> Void)?
    var onAutomaticRecordingChange: ((Bool) -> Void)?
    var onIncludeScreenVideoChange: ((Bool) -> Void)?
    var onSkipMeeting: (() -> Void)?
    var onRetryMeeting: ((String) -> Void)?
    var onRetryTranscriptions: (() -> Void)?
    var onSettings: (() -> Void)?
    var onReviewMeetings: (() -> Void)?
    var onShowControls: (() -> Void)?
    var onWarningHistory: (() -> Void)?
    var onStopRecording: ((String, Int?) -> Void)?
    var onPauseRecording: ((String, Int, Bool) -> Void)?
    func update(recording: Bool, elapsed: String?) {}
    func updateOptions(_ options: MenuBarOptions) {}
    func updateStatus(_ snapshot: RecordingStatusSnapshot) {}
    func updateTranscription(_ text: String?) {}
}

@MainActor
final class SettingsWindowController {
    var onToggleRecording: (() -> Void)?
    var onStopRecording: ((String, Int?) -> Void)?
    var onPauseRecording: ((String, Int, Bool) -> Void)?
    var onIncludeScreenVideoChange: ((Bool) -> Void)?
    var onCaptureScopeChange: ((QuillCaptureScope) -> Void)?
    var onCaptureTargetChange: ((String) -> Void)?
    var onRefreshCaptureTargets: (() -> Void)?
    var onTranscriptionChange: ((Bool) -> Void)?
    var onSpeakerIdentificationChange: ((Bool) -> Void)?
    var onSetupConnection: ((QuillConnection) -> Void)?
    var onSetupNotes: (() -> Void)?
    var onReviewProcessing: (() -> Void)?
    var onNotesChange: ((Bool) -> Void)?
    var onDriveDocumentsChange: ((Bool) -> Void)?
    var onDriveRecordingsChange: ((Bool) -> Void)?
    var onLaunchAtLoginChange: ((Bool) -> Void)?
    var onOpenLoginSettings: (() -> Void)?
    var onCheckInHoursChange: ((Int) -> Void)?
    var onShowInDockChange: ((Bool) -> Void)?
    var window: NSWindow? { nil }
    func update(_ state: QuillSettingsState) {}
    func show() { preconditionFailure("Unexpected settings UI") }
}

@MainActor
final class RecordingStatusController {
    var onStop: ((String, Int?) -> Void)?
    var onPause: ((String, Int, Bool) -> Void)?
    var onContinue: ((String, Int) -> Void)?
    var onKeepLocal: ((String) -> Void)?
    var onProcess: ((String) -> Void)?
    var onOpenFolder: ((String) -> Void)?
    var onSettings: (() -> Void)?
    var onWarningHistory: (() -> Void)?
    var snapshot = RecordingStatusSnapshot()
    var shown = 0
    func update(_ snapshot: RecordingStatusSnapshot) { self.snapshot = snapshot }
    func show() { shown += 1 }
}

@MainActor
final class RecordingWarningHistoryWindowController {
    func show(entries: [RecordingWarningHistory.Entry], storageIssue: String?) {}
    func update(entries: [RecordingWarningHistory.Entry], storageIssue: String?) {}
}

/// In-memory boundary only: the production coordinator may update reminders,
/// but this test executable must never create an AppKit panel.
@MainActor
final class MeetingReminderController {
    var onStart: ((String) -> Void)?
    var onSkip: ((String) -> Void)?
    private(set) var meetingID: String?
    private(set) var canStart = false
    private(set) var canSkip = false
    private(set) var detail = ""

    func update(meetingID: String?, canStart: Bool = false, canSkip: Bool = true, detail: String = "") {
        self.meetingID = meetingID
        self.canStart = canStart
        self.canSkip = canSkip
        self.detail = detail
    }
}

/// No status file is created, including in ~/.local/state/quill. Assertions
/// exercise what the real coordinator publishes, without a browser bridge.
@MainActor
final class RecordingStatusHeartbeat {
    struct Snapshot {
        let phase: String
        let deliberatelySkipped: Set<String>
        let timestamp: TimeInterval
    }
    private(set) var latest: Snapshot?
    private(set) var invalidations = 0

    func publish(phase: String, deliberatelySkipped: Set<String>, now: TimeInterval) {
        latest = Snapshot(phase: phase, deliberatelySkipped: deliberatelySkipped, timestamp: now)
    }
    func invalidate() {
        latest = nil
        invalidations += 1
    }
}

@MainActor
enum Config {
    static func screenVideoEnabled() -> Bool { Fixture.screenEnabled }
    static func meetingAutomationEnabled() -> Bool { Fixture.automationEnabled }
    static func transcriptionEnabled() -> Bool { Fixture.transcriptionEnabled }
    static func speakerIdentificationEnabled() -> Bool { false }
    static func showInDock() -> Bool { false }
    static func recordingCheckInHours() -> Int { 2 }
    static func onStop() -> String? { "test-only-spy-never-executed" }
    static func onStopMaxAttempts() -> Int { 1 }
    static func workflowSettingsURL() -> URL { Fixture.root.appendingPathComponent("missing-workflow.json") }
    static func completionStatusURL() -> URL { Fixture.root.appendingPathComponent("missing-status.json") }
    static func completionWorkerCommand() -> [String]? { nil }
    static func meetingReviewCommand() -> [String]? { nil }
    static func screenCaptureScope() -> String { Fixture.screenEnabled ? "display" : "window" }
    static func screenCaptureSelection() -> String { Fixture.screenEnabled ? "display:1" : "automatic" }
    static func setRecordingCheckInHours(_ value: Int) throws { preconditionFailure("Unexpected setting change") }
    static func setShowInDock(_ value: Bool) throws { preconditionFailure("Unexpected setting change") }
    static func setMeetingAutomationEnabled(_ value: Bool) throws { preconditionFailure("Unexpected setting change") }
    static func setScreenVideoEnabled(_ value: Bool) throws { preconditionFailure("Unexpected setting change") }
    static func setScreenCaptureScope(_ value: String) throws { preconditionFailure("Unexpected setting change") }
    static func setScreenCaptureSelection(_ value: String) throws { preconditionFailure("Unexpected setting change") }
    static func setTranscriptionEnabled(_ value: Bool) throws { preconditionFailure("Unexpected setting change") }
    static func setSpeakerIdentificationEnabled(_ value: Bool) throws { preconditionFailure("Unexpected setting change") }
}

@MainActor
final class CompletionJournal {
    func register(session: URL, command: String, waitingForTranscript: Bool, maxAttempts: Int) throws -> String {
        Fixture.registered.append(session)
        return "test-completion"
    }
}

@MainActor
final class TranscriptionCoordinator {
    enum Status: Sendable { case idle, preparing, transcribing(session: String, queued: Int), failed(session: String) }
    enum RetryResult { case disabled, queued(Int) }
    func setStatusHandler(_ handler: @Sendable @escaping (Status) -> Void) async {}
    func resumePending(root: URL) async {}
    func retryPending(root: URL) async -> RetryResult {
        Fixture.retryCalls += 1
        await Fixture.retryGate.wait()
        return Fixture.retryResult
    }
    func enqueue(_ session: URL, completion: String?) async { Fixture.enqueued.append(session) }
}

// The boundary supplies synthetic signals but uses the actual state machine;
// no file in ~/.local/state/quill is read or written.
@MainActor
final class MeetingAutomationController {
    private var machine = MeetingAutomation()
    private var now = 1_000.0
    private var joined: [MeetingSignal] = []
    private var reminderEvidence: [MeetingSignal]?
    var onStart: ((String) -> Void)?
    var onStop: ((String) -> Void)?
    var onStatus: ((MeetingAutomation.Status) -> Void)?
    var status: MeetingAutomation.Status { machine.status }
    var deliberatelySkipped: Set<String> { machine.suppression.skipped.subtracting(machine.suppression.failedStarts) }
    func hasFailedStart(meeting: String) -> Bool { machine.suppression.failedStarts.contains(meeting) }
    var failedMeetingToRetry: String? { machine.failedStartToRetry(now: now) }
    var canSkipMeeting: Bool { !joined.isEmpty }
    func joinedSignals() -> [MeetingSignal] { joined }
    func reminderSignals() -> [MeetingSignal] { reminderEvidence ?? joined }
    func signal(for meeting: String) -> MeetingSignal? { joined.first { $0.meeting == meeting } }
    func windowContinuity(meeting: String, source: String, windowID: Int) -> MeetingWindowContinuity.Decision { .keepRecording }
    func mayStart(meeting: String) -> Bool { Config.meetingAutomationEnabled() && !machine.skipped.contains(meeting) && signal(for: meeting) != nil }
    func startFailed(meeting: String, retryable: Bool = true) { machine.startFailed(meeting: meeting, retryable: retryable) }
    func retryFailedStart(meeting: String) {
        machine.enabled = Config.meetingAutomationEnabled()
        if case .start(let id) = machine.retryFailedStart(meeting: meeting, now: now) { onStart?(id) }
    }
    func recordingStarted(id: String, meeting: String?) {
        machine.didStart(id: id, owner: meeting.map { .meeting($0) } ?? .manual)
    }
    func recordingStopped(id: String, manually: Bool) { machine.didStop(id: id, manually: manually, now: now) }
    func skipCurrent() {
        if case .stop(let id) = machine.skipCurrent(now: now) { onStop?(id) }
    }
    func skip(meeting: String) {
        if case .stop(let id) = machine.skip(meeting: meeting, now: now) { onStop?(id) }
    }
    /// Supply current call evidence without causing the fake polling boundary
    /// to auto-start. Reminder actions still use the actual ownership machine.
    func observe(_ signals: [MeetingSignal], now: TimeInterval) {
        self.now = now
        machine.enabled = Config.meetingAutomationEnabled()
        reminderEvidence = signals
        joined = signals.filter { $0.state == .joined && now - $0.timestamp <= 20 && $0.timestamp <= now + 5 }
        for signal in signals { machine.ingest(signal, now: now) }
    }
    func stopPolling() {}
    func join(_ meeting: String) {
        machine.enabled = true
        let signal = MeetingSignal(source: "test-browser", meeting: meeting, provider: "google-meet", state: .joined, timestamp: now)
        joined = [signal]
        machine.ingest(signal, now: now)
        tick()
        now += 3
        tick()
    }
    func freshHeartbeat() {
        now += 60
        for signal in joined {
            machine.ingest(.init(source: signal.source, meeting: signal.meeting, provider: signal.provider,
                                 state: .joined, timestamp: now), now: now)
        }
        tick()
        now += 3
        tick()
    }
    private func tick() {
        switch machine.tick(now: now) {
        case .start(let id): onStart?(id)
        case .stop(let id): onStop?(id)
        case nil: break
        }
    }
}

enum ScreenCaptureTarget { case window(id: UInt32, ownerPID: Int32), display(id: UInt32) }
struct ScreenCaptureBounds { let left, top, width, height: Double }
struct ScreenCaptureChoice { let target: ScreenCaptureTarget; let title: String; let bounds: ScreenCaptureBounds }
enum ScreenVideoError: Error { case cancelled, targetUnavailable }
@MainActor
enum ScreenCaptureCatalog {
    static func availableChoices(requestPermission: Bool = false) async throws -> [ScreenCaptureChoice] {
        if Fixture.captureCatalogUnavailable {
            throw NSError(domain: "QuillCaptureFixture", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "The selected display is unavailable."])
        }
        precondition(Fixture.screenEnabled, "Unexpected screen capture discovery")
        return [ScreenCaptureChoice(target: .display(id: 1), title: "Synthetic display",
            bounds: ScreenCaptureBounds(left: 0, top: 0, width: 640, height: 480))]
    }
}
@MainActor
final class ScreenVideoRecorder {
    var onFailure: ((String) -> Void)?
    var onCleanup: (() -> Void)?
    private(set) var isRecording = false
    private(set) var didStart = false
    private(set) var needsCleanup = false
    private(set) var startedAt: Date?
    private(set) var pauses = 0
    private(set) var resumes = 0
    private(set) var stopRequested = false
    private var destination: URL?
    private var pendingPause = false
    private var pendingResume = false
    private var stopTask: Task<URL?, Error>?

    init() { Fixture.screenRecorders.append(self) }

    func start(target: ScreenCaptureTarget, writingTo url: URL, timeline: RecordingCaptureTimeline? = nil,
               validateStart: () throws -> Void) async throws {
        precondition(Fixture.screenEnabled, "Unexpected screen recording")
        try validateStart()
        destination = url
        try Data("synthetic screen fixture".utf8).write(to: url)
        startedAt = Date()
        isRecording = true; didStart = true; needsCleanup = true
    }
    func stop() async throws -> URL? {
        if let stopTask { return try await stopTask.value }
        stopRequested = true
        isRecording = false
        let task = Task<URL?, Error> { @MainActor in
            if self.pendingPause { await Fixture.screenPauseGate.wait() }
            if self.pendingResume { await Fixture.screenResumeGate.wait() }
            self.needsCleanup = false
            self.onCleanup?()
            return self.destination
        }
        stopTask = task
        return try await task.value
    }
    func pause() async throws {
        precondition(isRecording && !stopRequested)
        pauses += 1
        pendingPause = true
        defer { pendingPause = false }
        await Fixture.screenPauseGate.wait()
        isRecording = false; needsCleanup = false
    }
    func resume(validateStart: (() throws -> Void)?) async throws {
        guard !stopRequested else { throw ScreenVideoError.cancelled }
        resumes += 1
        pendingResume = true
        needsCleanup = true
        defer { pendingResume = false }
        await Fixture.screenResumeGate.wait()
        guard !stopRequested else { throw ScreenVideoError.cancelled }
        try validateStart?()
        isRecording = true
    }
}
