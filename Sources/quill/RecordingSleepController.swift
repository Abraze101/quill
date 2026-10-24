import AppKit

/// System sleep ends capture; display sleep and locking do not. Deliver the
/// pre-sleep callback synchronously so its durable hold and mic shutdown happen
/// before macOS suspends the app. Native capture cleanup can finish after wake.
@MainActor
final class RecordingSleepController {
    private let center: NotificationCenter
    private var observers: [NSObjectProtocol] = []
    private let onSleep: () -> Void
    private let onWake: () -> Void
    private(set) var isSleeping = false

    init(center: NotificationCenter = NSWorkspace.shared.notificationCenter,
         onSleep: @escaping () -> Void, onWake: @escaping () -> Void) {
        self.center = center
        self.onSleep = onSleep
        self.onWake = onWake
        observers.append(center.addObserver(forName: NSWorkspace.willSleepNotification,
            object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.willSleep() }
            })
        observers.append(center.addObserver(forName: NSWorkspace.didWakeNotification,
            object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.didWake() }
            })
    }

    func stopObserving() {
        for observer in observers { center.removeObserver(observer) }
        observers.removeAll()
    }

    private func willSleep() {
        guard !isSleeping else { return }
        isSleeping = true
        onSleep()
    }

    private func didWake() {
        guard isSleeping else { return }
        isSleeping = false
        onWake()
    }
}
