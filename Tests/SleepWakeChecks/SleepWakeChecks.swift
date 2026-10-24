import AppKit

// Each check posts into an isolated center. No actual system sleep, display
// change, capture device, permission request, or user notification is involved.
@MainActor
private final class Probe {
    weak var controller: RecordingSleepController?
    var events: [String] = []
    var pendingStartAllowed = true
}

@main
private struct SleepWakeChecks {
    @MainActor
    static func synchronousSleepAndWake() {
        let center = NotificationCenter()
        let probe = Probe()
        let controller = RecordingSleepController(center: center, onSleep: {
            precondition(probe.controller?.isSleeping == true,
                         "Sleep callback ran before the start-blocking latch was set")
            probe.pendingStartAllowed = false
            probe.events.append("sleep")
        }, onWake: {
            precondition(probe.controller?.isSleeping == false,
                         "Wake callback ran before the sleep latch was cleared")
            probe.events.append("wake")
        })
        probe.controller = controller
        defer { controller.stopObserving() }

        precondition(!controller.isSleeping)
        center.post(name: NSWorkspace.willSleepNotification, object: nil)
        precondition(probe.events == ["sleep"] && !probe.pendingStartAllowed,
                     "Pending capture was not invalidated before the sleep notification returned")
        center.post(name: NSWorkspace.didWakeNotification, object: nil)
        precondition(probe.events == ["sleep", "wake"] && !controller.isSleeping,
                     "Wake reconciliation was deferred")
        precondition(!probe.pendingStartAllowed, "Wake unexpectedly restored the cancelled capture")
    }

    @MainActor
    static func displayNotificationsDoNotInterruptCapture() {
        let center = NotificationCenter()
        let probe = Probe()
        let controller = RecordingSleepController(center: center,
            onSleep: { probe.events.append("sleep") },
            onWake: { probe.events.append("wake") })
        defer { controller.stopObserving() }

        center.post(name: NSWorkspace.screensDidSleepNotification, object: nil)
        center.post(name: NSWorkspace.screensDidWakeNotification, object: nil)
        precondition(probe.events.isEmpty && !controller.isSleeping,
                     "Display sleep or wake was mistaken for system sleep")

        center.post(name: NSWorkspace.willSleepNotification, object: nil)
        center.post(name: NSWorkspace.screensDidWakeNotification, object: nil)
        precondition(probe.events == ["sleep"] && controller.isSleeping,
                     "A display notification cleared a real system-sleep latch")
        center.post(name: NSWorkspace.didWakeNotification, object: nil)
        precondition(probe.events == ["sleep", "wake"])
    }

    @MainActor
    static func duplicateEventsAndRepeatedCycles() {
        let center = NotificationCenter()
        let probe = Probe()
        let controller = RecordingSleepController(center: center, onSleep: {
            precondition(probe.controller?.isSleeping == true)
            precondition(probe.events.last != "sleep", "Duplicate sleep callback")
            probe.events.append("sleep")
            // Reentrant notifications exercise latch order without depending
            // on task scheduling or an arbitrary test delay.
            center.post(name: NSWorkspace.willSleepNotification, object: nil)
        }, onWake: {
            precondition(probe.controller?.isSleeping == false)
            precondition(probe.events.last == "sleep", "Unpaired wake callback")
            probe.events.append("wake")
            center.post(name: NSWorkspace.didWakeNotification, object: nil)
        })
        probe.controller = controller
        defer { controller.stopObserving() }

        center.post(name: NSWorkspace.didWakeNotification, object: nil)
        precondition(probe.events.isEmpty, "Initial wake created a false interruption")
        for cycle in 1...3 {
            center.post(name: NSWorkspace.willSleepNotification, object: nil)
            center.post(name: NSWorkspace.willSleepNotification, object: nil)
            precondition(controller.isSleeping && probe.events.count == cycle * 2 - 1)
            center.post(name: NSWorkspace.didWakeNotification, object: nil)
            center.post(name: NSWorkspace.didWakeNotification, object: nil)
            precondition(!controller.isSleeping && probe.events.count == cycle * 2)
        }
        precondition(probe.events == ["sleep", "wake", "sleep", "wake", "sleep", "wake"])
    }

    @MainActor
    static func stoppingObservationRemovesOnlyOwnedCallbacks() {
        let center = NotificationCenter()
        let stopped = Probe()
        let active = Probe()
        let first = RecordingSleepController(center: center,
            onSleep: { stopped.events.append("sleep") },
            onWake: { stopped.events.append("wake") })
        let second = RecordingSleepController(center: center,
            onSleep: { active.events.append("sleep") },
            onWake: { active.events.append("wake") })
        defer { second.stopObserving() }

        center.post(name: NSWorkspace.willSleepNotification, object: nil)
        precondition(stopped.events == ["sleep"] && active.events == ["sleep"])
        first.stopObserving()
        first.stopObserving()
        center.post(name: NSWorkspace.didWakeNotification, object: nil)
        center.post(name: NSWorkspace.willSleepNotification, object: nil)
        center.post(name: NSWorkspace.didWakeNotification, object: nil)
        precondition(stopped.events == ["sleep"], "Stopped observer received more callbacks")
        precondition(active.events == ["sleep", "wake", "sleep", "wake"],
                     "Observer cleanup removed another controller's notifications")
    }

    @MainActor
    static func main() {
        synchronousSleepAndWake()
        displayNotificationsDoNotInterruptCapture()
        duplicateEventsAndRepeatedCycles()
        stoppingObservationRemovesOnlyOwnedCallbacks()
        print("Sleep/wake notification checks passed (4 scenarios; no system settings or capture changed).")
    }
}
