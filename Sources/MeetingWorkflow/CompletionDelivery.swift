import Darwin
import Foundation

/// At-least-once delivery of an explicitly registered completion hook. Hooks
/// that opt into retries must be idempotent. Delivery means queued, not uploaded.
public actor CompletionDelivery {
    private let journal: CompletionJournal
    private var draining = false
    private var onFailure: (@Sendable (String) -> Void)?

    public init(journal: CompletionJournal = CompletionJournal()) { self.journal = journal }

    public func setFailureHandler(_ handler: @escaping @Sendable (String) -> Void) { onFailure = handler }

    public func resume() {
        guard !draining else { return }
        draining = true
        Task { await drain() }
    }

    /// Public for the executable checks; no media or UI is involved.
    @discardableResult
    public func runReadyOnce(timeout: TimeInterval = 600, retryDelay: TimeInterval? = nil) async throws -> Bool {
        guard let job = try journal.claim() else { return false }
        let lease: RecordingReviewGate.ProcessingLease
        do { lease = try RecordingReviewGate.acquireProcessingLease(session: job.session) }
        catch {
            try journal.deferUnlaunched(job.id)
            return true
        }
        // Explicit lifetime includes subprocess shutdown, hook preparation,
        // and journal completion, even when an awaited operation suspends.
        defer { withExtendedLifetime(lease) {} }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", "\(job.command) \"$0\"", job.session.path]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        var environment = ProcessInfo.processInfo.environment
        environment["QUILL_COMPLETION_ID"] = job.id
        process.environment = environment
        if try journal.deferIfHeld(job.id) { return true }
        do {
            try process.run()
        } catch {
            try journal.finish(job.id, exitCode: -1, retryDelay: retryDelay)
            return true
        }
        // If recording the PID fails, leave the journal in its claimed state
        // for recovery. The child may already be enqueuing the durable snapshot.
        var launchJournalError: Error?
        do { try journal.launched(job.id, processID: process.processIdentifier) }
        catch { launchJournalError = error }
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline {
            try? await Task.sleep(for: .milliseconds(100))
        }
        if process.isRunning {
            process.terminate()
            let grace = Date().addingTimeInterval(2)
            while process.isRunning && Date() < grace { try? await Task.sleep(for: .milliseconds(100)) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            process.waitUntilExit()
            if let launchJournalError { throw launchJournalError }
            try journal.finish(job.id, exitCode: -2, retryDelay: retryDelay)
        } else {
            process.waitUntilExit()
            if let launchJournalError { throw launchJournalError }
            try journal.finish(job.id, exitCode: process.terminationStatus, retryDelay: retryDelay)
        }
        if let finished = try journal.jobs().first(where: { $0.id == job.id }), finished.state == .failed {
            onFailure?(job.session.lastPathComponent)
        }
        return true
    }

    private func drain() async {
        defer { draining = false }
        do {
            while true {
                try journal.recover()
                if try await runReadyOnce() { continue }
                let pending = try journal.jobs().filter {
                    [.ready, .retry, .running].contains($0.state)
                        && !RecordingReviewGate.blocksProcessing(session: $0.session)
                }
                guard !pending.isEmpty else { return }
                let next = pending.map(\.nextAttempt).min() ?? Date().timeIntervalSince1970 + 5
                let delay = max(0.25, min(5, next - Date().timeIntervalSince1970))
                try? await Task.sleep(for: .seconds(delay))
            }
        } catch {
            FileHandle.standardError.write(Data("quill: completion journal needs attention: \(error)\n".utf8))
            onFailure?("Completion queue")
        }
    }
}
