import AppKit
import ColorSync
import CoreGraphics
import Foundation
import ScreenCaptureKit

/// A specific user-selected window or display. macOS can give a reconnected
/// monitor a different numeric ID, including one previously used by another.
enum ScreenCaptureTarget: Codable, Hashable, Sendable {
    case window(id: UInt32, ownerPID: Int32)
    case display(id: UInt32, uuid: UUID? = nil)

    var identifier: String {
        switch self {
        case .window(let id, let ownerPID): return "window:\(id):\(ownerPID)"
        case .display(let id, let uuid): return "display:\(uuid?.uuidString ?? String(id))"
        }
    }

    func matchesSelection(_ selection: String) -> Bool {
        if selection == identifier { return true }
        // Older preferences stored the current numeric ID. Resolve them against
        // today's choices so the selected target carries its UUID into capture.
        guard case .display(let id, _) = self else { return false }
        return selection == "display:\(id)"
    }
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

/// Uses the same global display coordinates that ScreenCaptureKit supplies.
struct ScreenCaptureDisplay {
    let id: UInt32
    let uuid: UUID?
    let name: String
    let frame: CGRect
    let isMain: Bool

    var target: ScreenCaptureTarget { .display(id: id, uuid: uuid) }

    static func choices(from displays: [Self]) -> [ScreenCaptureChoice] {
        let ordered = displays.sorted {
            ($0.frame.minX, $0.frame.minY, $0.id) < ($1.frame.minX, $1.frame.minY, $1.id)
        }
        let byName = Dictionary(grouping: ordered, by: \.name)
        return ordered.map { display in
            var label = display.name
            if let peers = byName[display.name], peers.count > 1,
               let index = peers.firstIndex(where: { $0.id == display.id }) {
                var position = "\(index + 1) of \(peers.count)"
                if peers.count == 2 {
                    let other = peers[1 - index].frame
                    let dx = display.frame.midX - other.midX
                    let dy = display.frame.midY - other.midY
                    if abs(dx) >= abs(dy) && dx != 0 { position = dx < 0 ? "Left" : "Right" }
                    else if dy != 0 { position = dy < 0 ? "Top" : "Bottom" }
                }
                label += " (\(position))"
            }
            if display.isMain { label += " · Main" }
            label += " · \(Int(display.frame.width)) × \(Int(display.frame.height))"
            return ScreenCaptureChoice(target: display.target, title: label, bounds: ScreenCaptureBounds(display.frame))
        }
    }

    static func id(for target: ScreenCaptureTarget, in displays: [Self]) -> UInt32? {
        // Match the identity the user chose, even when building a new stream
        // after Pause. Never fall back to its old number if a UUID is missing.
        let matches = displays.filter { $0.target.identifier == target.identifier }
        return matches.count == 1 ? matches[0].id : nil
    }
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
            return "Allow Quill to record the screen in System Settings, then choose the window or display again."
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
            return "macOS did not return the available windows and displays in time. Try again in Settings."
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
        return windows + ScreenCaptureDisplay.choices(from: displayInventory(content))
    }

    static func contentFilter(for target: ScreenCaptureTarget) async throws -> SCContentFilter {
        let content = try await shareableContent()
        switch target {
        case .window(let id, let ownerPID):
            guard let window = content.windows.first(where: {
                $0.windowID == id && $0.owningApplication?.processID == ownerPID && isChromeWindow($0)
            }) else { throw ScreenVideoError.targetUnavailable }
            return SCContentFilter(desktopIndependentWindow: window)
        case .display:
            guard let id = ScreenCaptureDisplay.id(for: target, in: displayInventory(content)),
                  let display = content.displays.first(where: { $0.displayID == id }) else {
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

    private static func displayInventory(_ content: SCShareableContent) -> [ScreenCaptureDisplay] {
        var names: [UInt32: String] = [:]
        for screen in NSScreen.screens {
            if let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber {
                names[number.uint32Value] = screen.localizedName
            }
        }
        let main = CGMainDisplayID()
        return content.displays.map { display in
            let id = display.displayID
            let uuid = CGDisplayCreateUUIDFromDisplayID(id)?.takeRetainedValue()
            return ScreenCaptureDisplay(
                id: id, uuid: uuid.flatMap { UUID(uuidString: CFUUIDCreateString(nil, $0) as String) },
                name: names[id] ?? "Display", frame: display.frame, isMain: id == main
            )
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
