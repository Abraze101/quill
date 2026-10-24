import AppKit
import Foundation

@main
@MainActor
enum LoginItemChecks {
    private enum TestError: LocalizedError {
        case refused
        var errorDescription: String? { "Test registration refused" }
    }

    @MainActor
    private final class FakeService {
        var installed = true
        var legacyPresent = false
        var status: LoginItemController.Registration = .notRegistered
        var registrationReads = 0
        var registerCalls = 0
        var unregisterCalls = 0
        var registerFails = false
        var unregisterFails = false
        var registerResult: LoginItemController.Registration = .enabled

        func controller() -> LoginItemController {
            LoginItemController(
                installed: { self.installed },
                legacyPresent: { self.legacyPresent },
                registration: {
                    self.registrationReads += 1
                    return self.status
                },
                register: {
                    self.registerCalls += 1
                    if self.registerFails { throw TestError.refused }
                    self.status = self.registerResult
                },
                unregister: {
                    self.unregisterCalls += 1
                    if self.unregisterFails { throw TestError.refused }
                    self.status = .notRegistered
                }
            )
        }
    }

    static func main() {
        // Every controller dependency is injected; these checks never read or
        // register a real login item, open Settings, or create an app instance.
        let service = FakeService()
        let controller = service.controller()
        let off = controller.snapshot()
        precondition(!off.enabled && !off.requiresApproval && off.canChange)
        precondition(off.detail.hasPrefix("Off."))
        precondition(service.registerCalls == 0 && service.unregisterCalls == 0)

        controller.setEnabled(true)
        let on = controller.snapshot()
        precondition(on.enabled && !on.requiresApproval && on.canChange)
        precondition(service.registerCalls == 1 && service.unregisterCalls == 0)
        controller.setEnabled(true)
        precondition(service.registerCalls == 1, "Repeated enable must not register twice")
        controller.setEnabled(false)
        precondition(!controller.snapshot().enabled && service.unregisterCalls == 1)
        controller.setEnabled(false)
        precondition(service.unregisterCalls == 1, "Repeated disable must not unregister twice")

        let pendingService = FakeService()
        pendingService.registerResult = .requiresApproval
        let pending = pendingService.controller()
        pending.setEnabled(true)
        let approval = pending.snapshot()
        precondition(!approval.enabled && approval.requiresApproval && approval.canChange)
        precondition(approval.detail.contains("Waiting for approval"))
        pending.setEnabled(true)
        precondition(pendingService.registerCalls == 1, "Pending registration must not be repeated")
        pending.setEnabled(false)
        precondition(!pending.snapshot().requiresApproval && pendingService.unregisterCalls == 1,
                     "A pending item must be removable")

        let unavailableService = FakeService()
        unavailableService.status = .unavailable
        let unavailable = unavailableService.controller()
        let missing = unavailable.snapshot()
        precondition(!missing.enabled && !missing.requiresApproval && !missing.canChange)
        unavailable.setEnabled(true)
        unavailable.setEnabled(false)
        precondition(unavailableService.registerCalls == 0 && unavailableService.unregisterCalls == 0)

        let absentService = FakeService()
        absentService.status = .notFound
        let absent = absentService.controller()
        let firstUse = absent.snapshot()
        precondition(!firstUse.enabled && !firstUse.requiresApproval && firstUse.canChange)
        precondition(firstUse.detail.contains("Not set up."))
        precondition(absentService.registerCalls == 0 && absentService.unregisterCalls == 0,
                     "A missing background-task record must not register automatically")
        absent.setEnabled(false)
        precondition(absentService.registerCalls == 0 && absentService.unregisterCalls == 0,
                     "An absent background-task record has nothing to unregister")
        absent.setEnabled(true)
        precondition(absent.snapshot().enabled && absentService.registerCalls == 1,
                     "Explicit first-use enable must attempt registration")

        let absentFailureService = FakeService()
        absentFailureService.status = .notFound
        absentFailureService.registerFails = true
        let absentFailure = absentFailureService.controller()
        absentFailure.setEnabled(true)
        let failedFirstUse = absentFailure.snapshot()
        precondition(absentFailureService.registerCalls == 1 && !failedFirstUse.enabled)
        precondition(failedFirstUse.canChange && !failedFirstUse.requiresApproval)
        precondition(absentFailure.error != nil && failedFirstUse.detail.contains("Test registration refused"),
                     "First-use registration failures must show the actual error")

        let previewService = FakeService()
        previewService.installed = false
        previewService.status = .enabled
        let preview = previewService.controller()
        let previewState = preview.snapshot()
        precondition(!previewState.enabled && !previewState.requiresApproval && !previewState.canChange)
        precondition(previewState.detail.contains("Applications"))
        preview.setEnabled(true)
        preview.setEnabled(false)
        precondition(previewService.registerCalls == 0 && previewService.unregisterCalls == 0)
        precondition(previewService.registrationReads == 0, "Preview copies must not query the installed login item")

        let legacyService = FakeService()
        legacyService.legacyPresent = true
        let legacy = legacyService.controller()
        precondition(!legacy.snapshot().canChange && legacy.snapshot().detail.contains("older Quill login agent"))
        legacy.setEnabled(true)
        precondition(legacyService.registerCalls == 0, "Do not create a duplicate legacy login agent")
        legacyService.status = .notFound
        precondition(!legacy.snapshot().canChange && legacy.snapshot().detail.contains("older Quill login agent"))
        legacy.setEnabled(true)
        legacy.setEnabled(false)
        precondition(legacyService.registerCalls == 0 && legacyService.unregisterCalls == 0,
                     "Legacy agents must also block first-use registration without unregistering an absent native item")
        legacyService.status = .enabled
        precondition(legacy.snapshot().enabled && legacy.snapshot().canChange)
        legacy.setEnabled(false)
        precondition(legacyService.unregisterCalls == 1 && !legacy.snapshot().canChange,
                     "Legacy presence must not prevent disabling the native item")
        legacyService.status = .requiresApproval
        legacy.setEnabled(false)
        precondition(legacyService.unregisterCalls == 2,
                     "Legacy presence must not prevent withdrawing native approval")

        let failingService = FakeService()
        failingService.registerFails = true
        let failing = failingService.controller()
        failing.setEnabled(true)
        let failedEnable = failing.snapshot()
        precondition(!failedEnable.enabled && failing.error != nil)
        precondition(failedEnable.detail.contains("Test registration refused") && failedEnable.detail.contains("Off."))
        failingService.registerFails = false
        failing.setEnabled(true)
        precondition(failing.error == nil && failing.snapshot().enabled, "A successful retry must clear the error")
        failingService.unregisterFails = true
        failing.setEnabled(false)
        precondition(failing.error != nil && failing.snapshot().enabled,
                     "Failed unregister must keep the true enabled state")
        failingService.unregisterFails = false
        failing.setEnabled(false)
        precondition(failing.error == nil && !failing.snapshot().enabled)

        let restoredService = FakeService()
        restoredService.status = .enabled
        let restored = restoredService.controller()
        precondition(restored.snapshot().enabled, "A fresh controller must restore macOS state")
        restoredService.status = .notRegistered
        precondition(!restored.snapshot().enabled, "External changes must be reflected without cached preferences")
        restoredService.status = .requiresApproval
        precondition(restored.snapshot().requiresApproval)
        restoredService.status = .enabled
        precondition(restored.snapshot().enabled && !restored.snapshot().requiresApproval)
        precondition(restoredService.registerCalls == 0 && restoredService.unregisterCalls == 0,
                     "Restoring state must not mutate registration")

        precondition(!LoginItemController.isLoginLaunch(nil))
        let ordinary = event(kAEOpenApplication)
        precondition(!LoginItemController.isLoginLaunch(ordinary))
        ordinary.setParam(NSAppleEventDescriptor(enumCode: keyAELaunchedAsLogInItem), forKeyword: keyAEPropData)
        precondition(LoginItemController.isLoginLaunch(ordinary), "A login launch must stay quiet")
        let document = event(kAEOpenDocuments)
        document.setParam(NSAppleEventDescriptor(enumCode: keyAELaunchedAsLogInItem), forKeyword: keyAEPropData)
        precondition(!LoginItemController.isLoginLaunch(document), "A document-open event is not a login launch")
        let other = event(kAEOpenApplication)
        other.setParam(NSAppleEventDescriptor(enumCode: 0), forKeyword: keyAEPropData)
        precondition(!LoginItemController.isLoginLaunch(other))
        let malformed = event(kAEOpenApplication)
        malformed.setParam(NSAppleEventDescriptor(string: "login"), forKeyword: keyAEPropData)
        precondition(!LoginItemController.isLoginLaunch(malformed))

        print("Login items: 12 checks passed (off, on, approval, unavailable, first use, first-use error, preview, legacy, errors, restored state, idempotence, quiet login events)")
    }

    private static func event(_ id: AEEventID) -> NSAppleEventDescriptor {
        NSAppleEventDescriptor(eventClass: kCoreEventClass, eventID: id,
                               targetDescriptor: nil, returnID: AEReturnID(kAutoGenerateReturnID),
                               transactionID: AETransactionID(kAnyTransactionID))
    }
}
