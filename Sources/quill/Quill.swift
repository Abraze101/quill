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
        let appDelegate = QuillAppDelegate(openControls: { controller.reopen() },
                                          shouldTerminate: { controller.shouldTerminate() },
                                          restoreVisibility: { controller.restoreRecordingVisibility() },
                                          showControls: { controller.showRecordingControls() },
                                          showSettings: { controller.showSettings() },
                                          retryTranscriptions: { controller.retryTranscriptions() },
                                          canRetryTranscriptions: { controller.canRetryTranscriptions })
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
}

@MainActor
private final class QuillAppDelegate: NSObject, NSApplicationDelegate, NSMenuItemValidation {
    private let openControls: () -> Void
    private let shouldTerminate: () -> NSApplication.TerminateReply
    private let restoreVisibility: () -> Void
    private let showControls: () -> Void
    private let showSettings: () -> Void
    private let retryTranscriptions: () -> Void
    private let canRetryTranscriptions: () -> Bool

    init(openControls: @escaping () -> Void, shouldTerminate: @escaping () -> NSApplication.TerminateReply,
         restoreVisibility: @escaping () -> Void, showControls: @escaping () -> Void,
         showSettings: @escaping () -> Void, retryTranscriptions: @escaping () -> Void,
         canRetryTranscriptions: @escaping () -> Bool) {
        self.openControls = openControls
        self.shouldTerminate = shouldTerminate
        self.restoreVisibility = restoreVisibility
        self.showControls = showControls
        self.showSettings = showSettings
        self.retryTranscriptions = retryTranscriptions
        self.canRetryTranscriptions = canRetryTranscriptions
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let mainMenu = NSMenu()
        let applicationItem = NSMenuItem(title: "Quill", action: nil, keyEquivalent: "")
        let applicationMenu = NSMenu(title: "Quill")
        let about = NSMenuItem(title: "About Quill…", action: #selector(openAbout), keyEquivalent: "")
        about.target = self
        applicationMenu.addItem(about)
        applicationMenu.addItem(.separator())
        let controls = NSMenuItem(title: "Show recording controls", action: #selector(openRecordingControls), keyEquivalent: "")
        controls.target = self
        applicationMenu.addItem(controls)
        let retry = NSMenuItem(title: "Retry unfinished transcriptions…",
                               action: #selector(retryUnfinishedTranscriptions), keyEquivalent: "")
        retry.target = self
        applicationMenu.addItem(retry)
        let settings = NSMenuItem(title: "Settings…", action: #selector(openSettings), keyEquivalent: ",")
        settings.target = self
        applicationMenu.addItem(settings)
        applicationMenu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit Quill", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        quit.target = NSApp
        applicationMenu.addItem(quit)
        applicationItem.submenu = applicationMenu
        mainMenu.addItem(applicationItem)
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

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if menuItem.action == #selector(retryUnfinishedTranscriptions) { return canRetryTranscriptions() }
        return true
    }

    @objc private func openRecordingControls() { showControls() }
    @objc private func openSettings() { showSettings() }
    @objc private func retryUnfinishedTranscriptions() {
        guard canRetryTranscriptions() else { return }
        retryTranscriptions()
    }
    @objc private func openAbout() {
        NSApp.orderFrontStandardAboutPanel(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
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
