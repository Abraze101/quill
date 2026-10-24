import CoreGraphics
import Foundation
import ScreenCaptureKit

/// A specific user-selected window or display. A window selection is never
/// widened to an application or display if it disappears.
enum ScreenCaptureTarget: Codable, Hashable, Sendable {
    case window(id: UInt32, ownerPID: Int32)
    case display(id: UInt32)
}

struct ScreenCaptureBounds: Codable, Equatable, Sendable {
    let left: Double
    let top: Double
    let width: Double
    let height: Double

    init(_ frame: CGRect) {
        left = frame.minX
        top = frame.minY
        width = frame.width
        height = frame.height
    }
}

struct ScreenCaptureChoice: Sendable {
    let target: ScreenCaptureTarget
    let title: String
    let bounds: ScreenCaptureBounds
}

enum ScreenVideoError: Error, LocalizedError, Sendable {
    case permissionRequired
    case microphonePermissionRequired
    case targetUnavailable
    case invalidDestination
    case destinationExists
    case alreadyStarted
    case cancelled
    case captureFailed(String)
    case startTimedOut
    case contentQueryTimedOut
    case finalizationTimedOut
    case emptyRecording

    var errorDescription: String? {
        switch self {
        case .permissionRequired:
            return "Allow Quill to record the screen in System Settings, then choose the meeting window again."
        case .microphonePermissionRequired:
            return "Allow Quill to use the microphone before recording video with audio."
        case .targetUnavailable:
            return "The selected Chrome window or display is no longer available. Choose it again in Settings."
        case .invalidDestination:
            return "The screen recording needs a new local MP4 file."
        case .destinationExists:
            return "A screen recording already exists at this location. It was preserved."
        case .alreadyStarted:
            return "This screen recorder has already been used for a session."
        case .cancelled:
            return "Screen recording was stopped before it started."
        case .captureFailed(let message):
            return "Screen recording failed: \(message)"
        case .startTimedOut:
            return "Screen recording did not start in time."
        case .contentQueryTimedOut:
            return "macOS did not return the available recording windows in time. Try again in Settings."
        case .finalizationTimedOut:
            return "The screen recording has not finished saving. Its file will not be sent for processing."
        case .emptyRecording:
            return "Screen capture finished without a usable video file."
        }
    }
}

@MainActor
enum ScreenCaptureCatalog {
    static func availableChoices(requestPermission: Bool = false) async throws -> [ScreenCaptureChoice] {
        let content = try await shareableContent(requestPermission: requestPermission)
        let windows = content.windows.filter(isChromeWindow).map { window in
            ScreenCaptureChoice(
                target: .window(id: window.windowID, ownerPID: window.owningApplication!.processID),
                title: window.title?.isEmpty == false ? window.title! : "Chrome window \(window.windowID)",
                bounds: ScreenCaptureBounds(window.frame)
            )
        }.sorted { $0.title.localizedStandardCompare($1.title) == .orderedAscending }
        let displays = content.displays.sorted { $0.displayID < $1.displayID }.enumerated().map { index, display in
            ScreenCaptureChoice(
                target: .display(id: display.displayID),
                title: "Entire screen \(index + 1) (\(display.width) × \(display.height))",
                bounds: ScreenCaptureBounds(display.frame)
            )
        }
        return windows + displays
    }

    static func contentFilter(for target: ScreenCaptureTarget) async throws -> SCContentFilter {
        let content = try await shareableContent()
        switch target {
        case .window(let id, let ownerPID):
            guard let window = content.windows.first(where: {
                $0.windowID == id && $0.owningApplication?.processID == ownerPID && isChromeWindow($0)
            }) else { throw ScreenVideoError.targetUnavailable }
            return SCContentFilter(desktopIndependentWindow: window)
        case .display(let id):
            guard let display = content.displays.first(where: { $0.displayID == id }) else {
                throw ScreenVideoError.targetUnavailable
            }
            // Exclude the whole running app so new/collapsed indicator windows
            // also stay out of Quill's display recording. This does not affect
            // what another application's screen sharing captures.
            let ownApplications = content.applications.filter { $0.processID == getpid() }
            guard !ownApplications.isEmpty else {
                throw ScreenVideoError.captureFailed("Quill could not exclude its recording controls from the screen video. Try starting again.")
            }
            return SCContentFilter(display: display, excludingApplications: ownApplications, exceptingWindows: [])
        }
    }

    private static func shareableContent(requestPermission: Bool = false) async throws -> SCShareableContent {
        // Enumerating content can otherwise trigger a system permission prompt.
        // The Settings action owns that request, not a background meeting event.
        guard requestPermission || CGPreflightScreenCaptureAccess() else { throw ScreenVideoError.permissionRequired }
        let request = ScreenCaptureRequest<ScreenShareableSnapshot>()
        Task {
            do {
                let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: false)
                request.resolve(.success(ScreenShareableSnapshot(content)))
            }
            catch { request.resolve(.failure(.captureFailed(error.localizedDescription))) }
        }
        return try await request.wait(seconds: requestPermission ? 60 : 10, timeout: .contentQueryTimedOut).content
    }

    private static func isChromeWindow(_ window: SCWindow) -> Bool {
        window.owningApplication?.bundleIdentifier == "com.google.Chrome"
            && window.windowLayer == 0 && window.frame.width > 1 && window.frame.height > 1
    }
}

@MainActor
private final class ScreenShareableSnapshot {
    let content: SCShareableContent
    init(_ content: SCShareableContent) { self.content = content }
}

/// A bounded callback wait. A late native result is ignored after a timeout;
/// callers that own active capture still stop that specific stream themselves.
@MainActor
final class ScreenCaptureRequest<Value: Sendable> {
    private var result: Result<Value, ScreenVideoError>?
    private var waiters: [CheckedContinuation<Value, Error>] = []
    private var timer: Task<Void, Never>?
    var isResolved: Bool { result != nil }

    func resolve(_ result: Result<Value, ScreenVideoError>) {
        guard self.result == nil else { return }
        self.result = result
        timer?.cancel()
        timer = nil
        let pending = waiters
        waiters.removeAll()
        for waiter in pending { waiter.resume(with: result.mapError { $0 as Error }) }
    }

    func wait(seconds: UInt64, timeout: ScreenVideoError) async throws -> Value {
        if let result { return try result.get() }
        return try await withCheckedThrowingContinuation { continuation in
            waiters.append(continuation)
            if timer == nil {
                timer = Task { [weak self] in
                    do { try await Task.sleep(nanoseconds: seconds * 1_000_000_000) }
                    catch { return }
                    self?.resolve(.failure(timeout))
                }
            }
        }
    }
}
