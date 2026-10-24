import AppKit
import ServiceManagement

struct QuillLoginItemState {
    var enabled = false
    var requiresApproval = false
    var canChange = false
    var detail = "Open Quill from Applications to use Launch at login."
}

/// macOS is the source of truth; no separate preference can drift from it.
@MainActor
final class LoginItemController {
    enum Registration { case notRegistered, notFound, enabled, requiresApproval, unavailable }
    private let installed: () -> Bool
    private let legacyPresent: () -> Bool
    private let registration: () -> Registration
    private let register: () throws -> Void
    private let unregister: () throws -> Void
    private(set) var error: String?

    init(
        installed: @escaping () -> Bool = {
            Bundle.main.bundleIdentifier == "com.digimata.quill"
                && Bundle.main.bundleURL.standardizedFileURL.path == "/Applications/Quill.app"
        },
        legacyPresent: @escaping () -> Bool = {
            let url = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/LaunchAgents/com.digimata.quill.plist")
            if FileManager.default.fileExists(atPath: url.path) { return true }
            let status = SMAppService.statusForLegacyPlist(at: url)
            return status == .enabled || status == .requiresApproval
        },
        registration: @escaping () -> Registration = {
            switch SMAppService.mainApp.status {
            case .notRegistered: return .notRegistered
            case .enabled: return .enabled
            case .requiresApproval: return .requiresApproval
            case .notFound: return .notFound
            @unknown default: return .unavailable
            }
        },
        register: @escaping () throws -> Void = { try SMAppService.mainApp.register() },
        unregister: @escaping () throws -> Void = { try SMAppService.mainApp.unregister() }
    ) {
        self.installed = installed
        self.legacyPresent = legacyPresent
        self.registration = registration
        self.register = register
        self.unregister = unregister
    }

    func snapshot() -> QuillLoginItemState {
        guard installed() else { return QuillLoginItemState() }
        let status = registration()
        var state = QuillLoginItemState(
            enabled: status == .enabled,
            requiresApproval: status == .requiresApproval,
            canChange: status != .unavailable,
            detail: "Off. Turn on to open Quill in the menu bar when you log in."
        )
        if state.enabled { state.detail = "Quill will open in the menu bar when you log in." }
        if state.requiresApproval {
            state.detail = "Waiting for approval in System Settings → General → Login Items."
        }
        if status == .notFound {
            // macOS can return this before the app has its first background-task
            // record. Only an explicit user action may attempt registration.
            state.detail = "Not set up. Turn on to open Quill in the menu bar when you log in."
        }
        if status == .unavailable { state.detail = "macOS could not find this Quill login item. Reopen Quill from Applications." }
        // An existing native item may still be disabled while a legacy one exists.
        if legacyPresent(), status == .notRegistered || status == .notFound {
            state.canChange = false
            state.detail = "An older Quill login agent is installed. Remove that agent before enabling this option."
        }
        if let error { state.detail = error + " " + state.detail }
        return state
    }

    func setEnabled(_ enabled: Bool) {
        error = nil
        guard snapshot().canChange else { return }
        let current = registration()
        do {
            if enabled {
                guard current == .notRegistered || current == .notFound else { return }
                guard !legacyPresent() else { return }
                try register()
            } else if current == .enabled || current == .requiresApproval {
                try unregister()
            }
        } catch {
            self.error = "Couldn’t change Launch at login: \(error.localizedDescription)"
        }
    }

    func openSystemSettings() { SMAppService.openSystemSettingsLoginItems() }

    static func isLoginLaunch(_ event: NSAppleEventDescriptor?) -> Bool {
        event?.eventID == kAEOpenApplication
            && event?.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
    }
}
