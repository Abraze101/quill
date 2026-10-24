import Darwin
import Foundation
import MeetingWorkflow

final class CompletionDeliveryTests {
    private func fixture(_ body: (URL, CompletionJournal) throws -> Void) throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try body(root, CompletionJournal(directory: root.appendingPathComponent("hooks")))
    }

    private func quote(_ value: String) -> String { "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'" }

    func testRegistrationIsDurableDeduplicatedAndWaitsForTranscription() throws {
        try fixture { root, journal in
            let session = root.appendingPathComponent("new-session")
            try FileManager.default.createDirectory(at: session, withIntermediateDirectories: true)
            let id = try journal.register(session: session, command: "echo", waitingForTranscript: true, now: 100)
            expectEqual(try journal.register(session: session, command: "echo", waitingForTranscript: true, now: 101), id)
            expectEqual(try journal.jobs().count, 1)
            expectNil(try journal.claim(now: 110))
            let reopened = CompletionJournal(directory: journal.directory)
            try reopened.ready(id, now: 111)
            expectEqual(try reopened.claim(now: 111)?.id, id)
        }
    }

    func testFailureBackoffAndFiniteAttempts() throws {
        try fixture { root, journal in
            let id = try journal.register(session: root, command: "false", waitingForTranscript: false, maxAttempts: 2, now: 100)
            expectEqual(try journal.claim(now: 100)?.attempts, 1)
            try journal.finish(id, exitCode: 7, now: 101, retryDelay: 30)
            expectNil(try journal.claim(now: 130))
            expectEqual(try journal.claim(now: 131)?.attempts, 2)
            try journal.finish(id, exitCode: 7, now: 132, retryDelay: 0)
            expectEqual(try journal.jobs().first?.state, .failed)
            expectNil(try journal.claim(now: 1000))
        }
    }

    func testRecoveryDoesNotRedispatchWhileAnActualChildIsLive() throws {
        try fixture { root, journal in
            let id = try journal.register(session: root, command: "true", waitingForTranscript: false, maxAttempts: 2, now: 100)
            _ = try journal.claim(now: 100)
            let child = Process()
            child.executableURL = URL(fileURLWithPath: "/bin/sleep")
            child.arguments = ["10"]
            try child.run()
            defer { if child.isRunning { child.terminate(); child.waitUntilExit() } }
            try journal.launched(id, processID: child.processIdentifier)
            try journal.recover(now: 200)
            expectEqual(try journal.jobs().first?.state, .running)
            expectNil(try journal.claim(now: 201))
            child.terminate()
            child.waitUntilExit()
            try journal.recover(now: 202)
            expectEqual(try journal.jobs().first?.state, .retry)
            expectEqual(try journal.claim(now: 202)?.id, id)
        }
    }

    func testNoRetryByDefaultForAnArbitraryHook() throws {
        try fixture { root, journal in
            _ = try journal.register(session: root, command: "false", waitingForTranscript: false, now: 100)
            _ = try journal.claim(now: 100)
            try journal.recover(now: 110, isAlive: { _ in false })
            expectEqual(try journal.jobs().first?.state, .failed)
            expectNil(try journal.claim(now: 120))
        }
    }

    func testOnlyExplicitRegistrationsAreDiscovered() throws {
        try fixture { root, journal in
            let old = root.appendingPathComponent("unregistered-old-session", isDirectory: true)
            try FileManager.default.createDirectory(at: old, withIntermediateDirectories: true)
            try Data("{}".utf8).write(to: old.appendingPathComponent("meta.json"))
            expectEqual(try journal.jobs().count, 0)
            expectNil(try journal.claim())
        }
    }

    func testSubprocessPreservesSessionArgumentAndDoesNotRepeatSuccess() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let journal = CompletionJournal(directory: root.appendingPathComponent("hooks"))
        let output = root.appendingPathComponent("received")
        let hook = root.appendingPathComponent("hook with spaces.sh")
        try Data(("printf '%s' \"$1\" > " + quote(output.path) + "\n").utf8).write(to: hook)
        let session = root.appendingPathComponent("call ' with $(printf injected) and spaces")
        try FileManager.default.createDirectory(at: session, withIntermediateDirectories: true)
        let id = try journal.register(session: session, command: "exec /bin/sh " + quote(hook.path),
                                      waitingForTranscript: false, maxAttempts: 2)
        let delivery = CompletionDelivery(journal: journal)
        expectEqual(try await delivery.runReadyOnce(timeout: 2), true)
        expectEqual(try String(contentsOf: output, encoding: .utf8), session.path)
        expectEqual(try journal.jobs().first?.state, .delivered)
        expectEqual(try await delivery.runReadyOnce(timeout: 2), false)
        expectEqual(try journal.jobs().first?.id, id)
    }

    func testTransientSubprocessFailureRetriesOnlyWhenOptedIn() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let journal = CompletionJournal(directory: root.appendingPathComponent("hooks"))
        let marker = root.appendingPathComponent("attempted")
        let hook = root.appendingPathComponent("flaky.sh")
        let text = "if [ ! -f " + quote(marker.path) + " ]; then\n  touch " + quote(marker.path) + "\n  exit 9\nfi\nexit 0\n"
        try Data(text.utf8).write(to: hook)
        _ = try journal.register(session: root, command: "exec /bin/sh " + quote(hook.path),
                                 waitingForTranscript: false, maxAttempts: 2)
        let delivery = CompletionDelivery(journal: journal)
        _ = try await delivery.runReadyOnce(timeout: 2, retryDelay: 0)
        expectEqual(try journal.jobs().first?.state, .retry)
        _ = try await delivery.runReadyOnce(timeout: 2, retryDelay: 0)
        expectEqual(try journal.jobs().first?.state, .delivered)
        expectEqual(try journal.jobs().first?.attempts, 2)
    }

    func testHungOwnedHookIsTimedOut() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let journal = CompletionJournal(directory: root.appendingPathComponent("hooks"))
        let hook = root.appendingPathComponent("hung.sh")
        try Data("exec /bin/sleep 10\n".utf8).write(to: hook)
        _ = try journal.register(session: root, command: "exec /bin/sh " + quote(hook.path), waitingForTranscript: false)
        let delivery = CompletionDelivery(journal: journal)
        _ = try await delivery.runReadyOnce(timeout: 0.1)
        expectEqual(try journal.jobs().first?.state, .failed)
        expectEqual(try journal.jobs().first?.error, "Completion hook exited with status -2")
    }

    func testMissingAndDeletedSessionFoldersStayBlocked() throws {
        try fixture { root, journal in
            let session = root.appendingPathComponent("session")
            expectEqual(RecordingReviewGate.blocksProcessing(session: session), true)
            expectFailure { _ = try journal.register(session: session, command: "true", waitingForTranscript: false) }
            expectFailure { _ = try RecordingReviewGate.acquireProcessingLease(session: session) }
            expectEqual(FileManager.default.fileExists(atPath: session.path), false)
            try FileManager.default.createDirectory(at: session, withIntermediateDirectories: true)
            expectEqual(RecordingReviewGate.blocksProcessing(session: session), false)
            let marker = session.appendingPathComponent(RecordingReviewGate.deletionFilename)
            try Data("malformed".utf8).write(to: marker)
            expectEqual(RecordingReviewGate.blocksProcessing(session: session), true)
            expectFailure { _ = try RecordingReviewGate.acquireProcessingLease(session: session) }
            let restored = root.appendingPathComponent("restored")
            try FileManager.default.moveItem(at: session, to: restored)
            expectEqual(RecordingReviewGate.blocksProcessing(session: restored), true)
            let restoredMarker = restored.appendingPathComponent(RecordingReviewGate.deletionFilename)
            try FileManager.default.removeItem(at: restoredMarker)
            try FileManager.default.createSymbolicLink(atPath: restoredMarker.path, withDestinationPath: "missing-target")
            expectEqual(RecordingReviewGate.blocksProcessing(session: restored), true)
            expectFailure { _ = try RecordingReviewGate.acquireProcessingLease(session: restored) }
            try FileManager.default.removeItem(at: restoredMarker)
            try FileManager.default.createDirectory(at: restoredMarker, withIntermediateDirectories: false)
            expectEqual(RecordingReviewGate.blocksProcessing(session: restored), true)
            expectFailure { _ = try RecordingReviewGate.acquireProcessingLease(session: restored) }
        }
    }

    func testProcessingLeaseRejectsUnsafeLockFilesAndSessionLinks() throws {
        try fixture { root, _ in
            let session = root.appendingPathComponent("session")
            try FileManager.default.createDirectory(at: session, withIntermediateDirectories: true)
            let sessionLink = root.appendingPathComponent("session-link")
            try FileManager.default.createSymbolicLink(at: sessionLink, withDestinationURL: session)
            expectEqual(RecordingReviewGate.blocksProcessing(session: sessionLink), true)
            expectFailure { _ = try RecordingReviewGate.acquireProcessingLease(session: sessionLink) }
            let lock = session.appendingPathComponent(RecordingReviewGate.processingLockFilename)
            let other = root.appendingPathComponent("other")
            try Data().write(to: other)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: other.path)
            try FileManager.default.createSymbolicLink(at: lock, withDestinationURL: other)
            expectFailure { _ = try RecordingReviewGate.acquireProcessingLease(session: session) }
            try FileManager.default.removeItem(at: lock)
            try FileManager.default.linkItem(at: other, to: lock)
            expectFailure { _ = try RecordingReviewGate.acquireProcessingLease(session: session) }
            try FileManager.default.removeItem(at: lock)
            try FileManager.default.createDirectory(at: lock, withIntermediateDirectories: false)
            expectFailure { _ = try RecordingReviewGate.acquireProcessingLease(session: session) }
            try FileManager.default.removeItem(at: lock)
            try Data().write(to: lock)
            try FileManager.default.setAttributes([.posixPermissions: 0o666], ofItemAtPath: lock.path)
            expectFailure { _ = try RecordingReviewGate.acquireProcessingLease(session: session) }
        }
    }

    func testProcessingCapabilityMarkerIsAtomicAndStrict() throws {
        try fixture { root, _ in
            let marker = root.appendingPathComponent(RecordingReviewGate.processingLeaseMarkerFilename)
            expectEqual(FileManager.default.fileExists(atPath: marker.path), false)
            let lease = try RecordingReviewGate.acquireProcessingLease(session: root)
            let bytes = try Data(contentsOf: marker)
            let object = try JSONSerialization.jsonObject(with: bytes) as! [String: Any]
            expectEqual(object["schema_version"] as? Int, 1)
            expectEqual(object["shared_processing_lease"] as? Bool, true)
            let inode = try FileManager.default.attributesOfItem(atPath: marker.path)[.systemFileNumber] as! NSNumber
            let second = try RecordingReviewGate.acquireProcessingLease(session: root)
            expectEqual(try FileManager.default.attributesOfItem(atPath: marker.path)[.systemFileNumber] as? NSNumber, inode)
            withExtendedLifetime((lease, second)) {}
            let fragments = [
                "not-json", "{}", "{\"schema_version\":true,\"shared_processing_lease\":true}",
                "{\"schema_version\":1.0,\"shared_processing_lease\":true}",
                "{\"schema_version\":1e0,\"shared_processing_lease\":true}",
                "{\"schema_version\":1,\"shared_processing_lease\":1}",
                "{\"schema_version\":1,\"shared_processing_lease\":false}",
                "{\"schema_version\":2,\"shared_processing_lease\":true}",
                "{\"schema_version\":1,\"shared_processing_lease\":true,\"extra\":1}",
                String(repeating: " ", count: 1_001)
            ]
            for fragment in fragments {
                try Data(fragment.utf8).write(to: marker)
                expectEqual(RecordingReviewGate.blocksProcessing(session: root), true)
                expectFailure { _ = try RecordingReviewGate.acquireProcessingLease(session: root) }
                expectEqual(try String(contentsOf: marker, encoding: .utf8), fragment)
            }
            try FileManager.default.removeItem(at: marker)
            let other = root.appendingPathComponent("marker-target")
            try bytes.write(to: other)
            try FileManager.default.createSymbolicLink(at: marker, withDestinationURL: other)
            expectEqual(RecordingReviewGate.blocksProcessing(session: root), true)
            expectFailure { _ = try RecordingReviewGate.acquireProcessingLease(session: root) }
            expectEqual(try Data(contentsOf: other), bytes)
            try FileManager.default.removeItem(at: marker)
            try FileManager.default.createDirectory(at: marker, withIntermediateDirectories: false)
            expectFailure { _ = try RecordingReviewGate.acquireProcessingLease(session: root) }
        }
    }

    func testBusyDeletionLeaseDoesNotSpendCompletionAttempt() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let journal = CompletionJournal(directory: root.appendingPathComponent("hooks"))
        let marker = root.appendingPathComponent("hook-ran")
        _ = try journal.register(session: root, command: "touch " + quote(marker.path), waitingForTranscript: false)
        let fd = open(root.appendingPathComponent(RecordingReviewGate.processingLockFilename).path,
            O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)
        precondition(fd >= 0 && flock(fd, LOCK_EX | LOCK_NB) == 0)
        defer { close(fd) }
        let delivery = CompletionDelivery(journal: journal)
        expectEqual(try await delivery.runReadyOnce(timeout: 2), true)
        let waiting = try journal.jobs()[0]
        expectEqual(waiting.state, .ready)
        expectEqual(waiting.attempts, 0)
        expectNil(waiting.processID)
        expectNil(waiting.attemptedAt)
        precondition(waiting.nextAttempt > Date().timeIntervalSince1970)
        expectEqual(FileManager.default.fileExists(atPath: marker.path), false)
        expectEqual(try await delivery.runReadyOnce(timeout: 2), false)
        precondition(flock(fd, LOCK_UN) == 0)
        try await Task.sleep(for: .milliseconds(2100))
        expectEqual(try await delivery.runReadyOnce(timeout: 2), true)
        expectEqual(try journal.jobs()[0].state, .delivered)
        expectEqual(try journal.jobs()[0].attempts, 1)
        expectEqual(FileManager.default.fileExists(atPath: marker.path), true)
    }

    func testCompletionLeaseLastsThroughActualChildExit() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let journal = CompletionJournal(directory: root.appendingPathComponent("hooks"))
        let started = root.appendingPathComponent("started")
        let finished = root.appendingPathComponent("finish")
        let hook = root.appendingPathComponent("hook.sh")
        try Data(("touch " + quote(started.path) + "\nwhile [ ! -f " + quote(finished.path) + " ]; do sleep 0.05; done\n").utf8).write(to: hook)
        _ = try journal.register(session: root, command: "exec /bin/sh " + quote(hook.path), waitingForTranscript: false)
        let delivery = CompletionDelivery(journal: journal)
        let work = Task { try await delivery.runReadyOnce(timeout: 5) }
        let deadline = Date().addingTimeInterval(3)
        while !FileManager.default.fileExists(atPath: started.path), Date() < deadline {
            try await Task.sleep(for: .milliseconds(25))
        }
        precondition(FileManager.default.fileExists(atPath: started.path), "Completion child did not start")
        let fd = open(root.appendingPathComponent(RecordingReviewGate.processingLockFilename).path, O_RDWR | O_NOFOLLOW)
        precondition(fd >= 0)
        defer { close(fd) }
        precondition(flock(fd, LOCK_EX | LOCK_NB) != 0 && errno == EWOULDBLOCK,
            "Deletion must be excluded for the completion child's lifetime")
        try Data().write(to: finished)
        expectEqual(try await work.value, true)
        precondition(flock(fd, LOCK_EX | LOCK_NB) == 0, "Completion lease leaked after child exit")
        flock(fd, LOCK_UN)
        expectEqual(try journal.jobs()[0].state, .delivered)
    }

    private func expectFailure(_ body: () throws -> Void) {
        do { try body(); preconditionFailure("Expected processing refusal") }
        catch { /* Expected. */ }
    }
}
