import Foundation

@main
enum ScreenCaptureChecks {
    @MainActor
    static func main() async throws {
        for target in [ScreenCaptureTarget.window(id: 123, ownerPID: 456), .display(id: 789)] {
            let encoded = try JSONEncoder().encode(target)
            let decoded = try JSONDecoder().decode(ScreenCaptureTarget.self, from: encoded)
            precondition(decoded == target)
        }

        let resolved = ScreenCaptureRequest<Int>()
        let first = Task { try await resolved.wait(seconds: 5, timeout: .startTimedOut) }
        let second = Task { try await resolved.wait(seconds: 5, timeout: .startTimedOut) }
        await Task.yield()
        resolved.resolve(.success(42))
        resolved.resolve(.failure(.cancelled))
        let values = try await [first.value, second.value]
        precondition(values == [42, 42])

        let timedOut = ScreenCaptureRequest<Void>()
        do {
            try await timedOut.wait(seconds: 0, timeout: .finalizationTimedOut)
            preconditionFailure("Expected a bounded timeout")
        } catch ScreenVideoError.finalizationTimedOut {}
        timedOut.resolve(.success(()))
        do {
            try await timedOut.wait(seconds: 5, timeout: .startTimedOut)
            preconditionFailure("A late callback must not turn a timed-out file into success")
        } catch ScreenVideoError.finalizationTimedOut {}

        let idle = ScreenVideoRecorder()
        let idleResult = try await idle.stop()
        precondition(idleResult == nil && !idle.needsCleanup && !idle.isRecording)

        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let existing = directory.appendingPathComponent("preserve.mp4")
        try Data("preserve this file".utf8).write(to: existing)
        let existingRecorder = ScreenVideoRecorder()
        do {
            try await existingRecorder.start(target: .display(id: 0), writingTo: existing)
            preconditionFailure("Existing recording was not rejected")
        } catch ScreenVideoError.destinationExists {}
        let preserved = try Data(contentsOf: existing)
        precondition(preserved == Data("preserve this file".utf8))
        precondition(!existingRecorder.needsCleanup)

        let nonlocal = ScreenVideoRecorder()
        do {
            try await nonlocal.start(target: .display(id: 0), writingTo: URL(string: "https://example.invalid/video.mp4")!)
            preconditionFailure("Nonlocal destination was not rejected")
        } catch ScreenVideoError.invalidDestination {}

        let symlink = directory.appendingPathComponent("linked.mp4")
        try FileManager.default.createSymbolicLink(at: symlink, withDestinationURL: directory.appendingPathComponent("missing.mp4"))
        let linked = ScreenVideoRecorder()
        do {
            try await linked.start(target: .display(id: 0), writingTo: symlink)
            preconditionFailure("Dangling symlink destination was not rejected")
        } catch ScreenVideoError.destinationExists {}

        let cancelledBeforeRequest = ScreenMicrophonePermission()
        cancelledBeforeRequest.cancel()
        do {
            _ = try await cancelledBeforeRequest.request { _ in preconditionFailure("Cancelled recording requested permission") }
            preconditionFailure("Permission cancellation was ignored")
        } catch ScreenVideoError.cancelled {}

        let cancelledPermission = ScreenMicrophonePermission()
        let pendingReply = FakePermissionReply()
        let permissionWait = Task { try await cancelledPermission.request(using: pendingReply.request) }
        let replyDeadline = Date().addingTimeInterval(1)
        while pendingReply.completion == nil && Date() < replyDeadline { await Task.yield() }
        precondition(pendingReply.completion != nil, "Permission request did not start")
        cancelledPermission.cancel()
        do {
            _ = try await permissionWait.value
            preconditionFailure("Waiting recording did not cancel")
        } catch ScreenVideoError.cancelled {}
        pendingReply.completion?(true)
        await Task.yield()
        do {
            _ = try await cancelledPermission.request(using: pendingReply.request)
            preconditionFailure("A late permission grant revived a cancelled recording")
        } catch ScreenVideoError.cancelled {}
        precondition(pendingReply.requestCount == 1)

        let expiredPermission = ScreenMicrophonePermission()
        let lateReply = FakePermissionReply()
        do {
            _ = try await expiredPermission.request(timeoutSeconds: 0, using: lateReply.request)
            preconditionFailure("Permission wait did not time out")
        } catch ScreenVideoError.captureFailed {}
        lateReply.completion?(true)
        await Task.yield()
        do {
            _ = try await expiredPermission.request(using: lateReply.request)
            preconditionFailure("A late permission grant revived an expired recording")
        } catch ScreenVideoError.captureFailed {}
        precondition(lateReply.requestCount == 1)

        let approvedPermission = ScreenMicrophonePermission()
        let approved = try await approvedPermission.request { completion in completion(true) }
        precondition(approved)

        let writerOnly = ScreenVideoStartup()
        writerOnly.outputStarted()
        precondition(!writerOnly.isReady, "Writer startup alone must not confirm screen video")
        writerOnly.videoFrameReceived()
        try await writerOnly.wait(seconds: 0)
        precondition(writerOnly.isReady)

        let frameFirst = ScreenVideoStartup()
        frameFirst.videoFrameReceived()
        precondition(!frameFirst.isReady, "A frame alone must not confirm a recording output")
        frameFirst.outputStarted()
        try await frameFirst.wait(seconds: 0)
        precondition(frameFirst.isReady)

        for error in [ScreenVideoError.cancelled, .captureFailed("Output failed") ] {
            let failedStartup = ScreenVideoStartup()
            failedStartup.outputStarted()
            failedStartup.fail(error)
            failedStartup.videoFrameReceived()
            precondition(!failedStartup.isReady, "Late frames revived a failed/cancelled startup")
            do {
                try await failedStartup.wait(seconds: 0)
                preconditionFailure("Failed video startup was accepted")
            } catch is ScreenVideoError {}
        }

        let missingFrames = ScreenVideoStartup()
        missingFrames.outputStarted()
        do {
            try await missingFrames.wait(seconds: 0)
            preconditionFailure("Video with no frames must time out")
        } catch ScreenVideoError.startTimedOut {}
        missingFrames.videoFrameReceived()
        precondition(!missingFrames.isReady, "A frame after timeout revived startup")

        // Regression: the real file's audio lasted 148.87s but its four video
        // samples covered only 0.135s. Container duration alone is insufficient.
        precondition(!ScreenVideoIntegrity.coversRecording(duration: 148.871667, videoStart: 0, videoDuration: 0.135))
        // Few samples are valid when a static frame spans the full recording.
        precondition(ScreenVideoIntegrity.coversRecording(duration: 149, videoStart: 0, videoDuration: 149))
        precondition(ScreenVideoIntegrity.coversRecording(duration: 149, videoStart: 0.1, videoDuration: 148.8))
        precondition(!ScreenVideoIntegrity.coversRecording(duration: 149, videoStart: 75, videoDuration: 74))
        for invalid in [0.0, -1.0, Double.nan, Double.infinity] {
            precondition(!ScreenVideoIntegrity.coversRecording(duration: invalid, videoStart: 0, videoDuration: 149))
            precondition(!ScreenVideoIntegrity.coversRecording(duration: 149, videoStart: 0, videoDuration: invalid))
        }
        print("20 screen-capture checks passed (no capture or permission requests)")
    }
}

@MainActor
private final class FakePermissionReply {
    var completion: (@Sendable (Bool) -> Void)?
    var requestCount = 0
    func request(_ completion: @escaping @Sendable (Bool) -> Void) {
        requestCount += 1
        self.completion = completion
    }
}
