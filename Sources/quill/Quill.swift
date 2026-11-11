import AppKit
import ArgumentParser
import Foundation
import MeetingWorkflow

@main
struct Quill: ParsableCommand {
    static let configuration = CommandConfiguration(
        commandName: "quill",
        abstract: "Local meeting recorder + transcriber. Records mic and system audio as two tracks, then transcribes on-device.",
        subcommands: [Run.self, Doctor.self, Install.self, DiarizeLocal.self],
        defaultSubcommand: Run.self
    )
}

struct Run: ParsableCommand {
    static let configuration = CommandConfiguration(
        commandName: "run",
        abstract: "Run the menu-bar daemon (default)."
    )

    @Option(name: .long, help: "Recordings root directory (overrides the config file).")
    var out: String?

    func run() throws {
        // ArgumentParser invokes run() on the main thread; promote that fact
        // to the type system so AppKit calls are cleanly isolated.
        try MainActor.assumeIsolated { try runMain() }
    }

    @MainActor
    private func runMain() throws {
        // The helper needs the instance lock to finish an interrupted swap.
        // Hand off before starting recording, transcription, or workflow services.
        if try QuillAppUpdater.handOffInterruptedUpdate(confirmRepair: confirmUpdateRepair) { return }
        let instanceLock: RunInstanceLock
        do {
            instanceLock = try RunInstanceLock()
        } catch RunInstanceLock.Failure.alreadyRunning {
            NSRunningApplication.runningApplications(withBundleIdentifier: "com.digimata.quill")
                .first { $0.processIdentifier != getpid() }?.activate(options: [])
            FileHandle.standardError.write(Data("Quill is already running. Use its menu-bar controls.\n".utf8))
            return
        }
        let root = Config.resolveRoot(cliOverride: out)

        // Non-blocking: permissions prompt on first recording, so warnings at
        // startup are informational, not fatal.
        let checks = DoctorReport.run(recordingsRoot: root)
        if !DoctorReport.allOK(checks) {
            FileHandle.standardError.write(Data("startup checks failed:\n".utf8))
            DoctorReport.print(checks)
            throw ExitCode(1)
        }

        let app = NSApplication.shared
        app.setActivationPolicy(Config.showInDock() ? .regular : .accessory)

        let controller = AppController(root: root)
        // The optional app bundle opens a useful window when launched or
        // reopened from Finder. The bare menu-bar executable keeps its startup.
        let appDelegate = QuillAppDelegate(mainMenu: controller.mainMenu,
                                          openControls: { controller.reopen() },
                                          shouldTerminate: { controller.shouldTerminate() },
                                          restoreVisibility: { controller.restoreRecordingVisibility() })
        app.delegate = appDelegate

        let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        sigint.setEventHandler {
            FileHandle.standardError.write(Data("\nshutting down\n".utf8))
            MainActor.assumeIsolated { controller.shutdown() }
        }
        sigint.resume()
        signal(SIGINT, SIG_IGN)

        FileHandle.standardError.write(Data(
            "quill up · recordings → \(root.path) · ^C to quit\n".utf8
        ))
        withExtendedLifetime((instanceLock, appDelegate)) { app.run() }
    }

    @MainActor
    private func confirmUpdateRepair(_ message: String) -> Bool {
        NSApplication.shared.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Quill needs to finish its update"
        alert.informativeText = message
        alert.addButton(withTitle: "Retry Repair")
        alert.addButton(withTitle: "Open Update Guide")
        alert.addButton(withTitle: "Quit")
        switch alert.runModal() {
        case .alertFirstButtonReturn: return true
        case .alertSecondButtonReturn:
            NSWorkspace.shared.open(URL(string: "https://github.com/Abraze101/quill/blob/main/docs/updating-local-source.md")!)
            return false
        default: return false
        }
    }
}

@MainActor
private final class QuillAppDelegate: NSObject, NSApplicationDelegate {
    private let mainMenu: NSMenu
    private let openControls: () -> Void
    private let shouldTerminate: () -> NSApplication.TerminateReply
    private let restoreVisibility: () -> Void

    init(mainMenu: NSMenu, openControls: @escaping () -> Void,
         shouldTerminate: @escaping () -> NSApplication.TerminateReply,
         restoreVisibility: @escaping () -> Void) {
        self.mainMenu = mainMenu
        self.openControls = openControls
        self.shouldTerminate = shouldTerminate
        self.restoreVisibility = restoreVisibility
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = mainMenu
        if Bundle.main.bundleURL.pathExtension == "app",
           !LoginItemController.isLoginLaunch(NSAppleEventManager.shared().currentAppleEvent) {
            openControls()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openControls()
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        shouldTerminate()
    }

    func applicationDidHide(_ notification: Notification) { restoreVisibility() }

}

struct Doctor: ParsableCommand {
    static let configuration = CommandConfiguration(
        abstract: "Check microphone, system audio, and recordings folder."
    )

    func run() throws {
        let checks = DoctorReport.run(recordingsRoot: Config.resolveRoot(cliOverride: nil))
        DoctorReport.print(checks)
        if !DoctorReport.allOK(checks) {
            throw ExitCode(1)
        }
    }
}
