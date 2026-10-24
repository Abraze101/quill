import Darwin
import Foundation

@main
struct SpeakerIdentificationChecks {
    static func main() async throws {
        checkAlignment()
        try await checkChildBoundary()
        print("Speaker identification checks passed: word-boundary changes, overlap/low-coverage/low-quality abstention, exact word preservation, bounded child output, timeout, cancellation and child cleanup.")
    }

    private static func checkAlignment() {
        let words = [TranscriptWord(start: 0, end: 0.4, text: "I'm"),
                     .init(start: 0.4, end: 0.8, text: "Max."),
                     .init(start: 0.8, end: 1.2, text: "Hello,"),
                     .init(start: 1.2, end: 1.6, text: "Max.")]
        let changed = SpeakerAlignment.segments(words: words, intervals: [
            .init(speaker: "A", start: 0, end: 0.8, quality: 1),
            .init(speaker: "B", start: 0.8, end: 1.6, quality: 1)])
        precondition(changed.map(\.text) == ["I'm Max.", "Hello, Max."])
        precondition(changed.map(\.speakerID) == ["A", "B"])
        precondition(changed.flatMap { $0.words ?? [] }.map(\.text) == words.map(\.text))

        let midSentence = SpeakerAlignment.segments(words: [
            .init(start: 0, end: 1, text: "repeat"), .init(start: 1, end: 2, text: "repeat"),
            .init(start: 2, end: 3, text: "café😀")], intervals: [
            .init(speaker: "A", start: 0, end: 1, quality: 1),
            .init(speaker: "B", start: 1, end: 3, quality: 1)])
        precondition(midSentence.map(\.text) == ["repeat", "repeat café😀"])
        precondition(midSentence.map(\.speakerID) == ["A", "B"], "A speaker change must split an unpunctuated sentence")

        let word = [TranscriptWord(start: 1, end: 2, text: "speech")]
        let ambiguous: [[SpeakerInterval]] = [
            [.init(speaker: "A", start: 1, end: 2, quality: 1), .init(speaker: "B", start: 1.9, end: 2, quality: 1)],
            [.init(speaker: "A", start: 1, end: 1.119, quality: 1)],
            [.init(speaker: "A", start: 1, end: 1.001, quality: 1)],
            [.init(speaker: "A", start: 1, end: 2, quality: 0.49)],
            [.init(speaker: "A", start: .nan, end: 2, quality: 1)],
            [.init(speaker: "A", start: 1, end: .infinity, quality: 1)],
            [.init(speaker: "A", start: 2, end: 1, quality: 1)],
            []
        ]
        for spans in ambiguous {
            let result = SpeakerAlignment.segments(words: word, intervals: spans)
            precondition(result.count == 1 && result[0].speakerID == nil && result[0].text == "speech")
        }
        let adequate = SpeakerAlignment.segments(words: word, intervals: [.init(speaker: "A", start: 1, end: 1.121, quality: 1)])
        precondition(adequate[0].speakerID == "A")
        let duplicateCoverage = SpeakerAlignment.segments(words: word, intervals: [
            .init(speaker: "A", start: 1, end: 1.07, quality: 1),
            .init(speaker: "A", start: 1, end: 1.07, quality: 1)])
        precondition(duplicateCoverage[0].speakerID == nil, "Duplicate intervals cannot fabricate coverage")
        let combined = SpeakerAlignment.segments(words: word, intervals: [
            .init(speaker: "A", start: 1, end: 1.08, quality: 1),
            .init(speaker: "A", start: 1.06, end: 1.13, quality: 1)])
        precondition(combined[0].speakerID == "A")

        func assigned(duration: Double, speech: Double, competing: Bool = false) -> Bool {
            var spans = [SpeakerInterval(speaker: "A", start: 0, end: speech, quality: 1)]
            if competing { spans.append(.init(speaker: "B", start: duration - 0.001, end: duration, quality: 1)) }
            return SpeakerAlignment.segments(words: [.init(start: 0, end: duration, text: "word")],
                                             intervals: spans)[0].speakerID != nil
        }
        precondition(!assigned(duration: 0.1, speech: 0.059), "Short words need 60 percent voiced support")
        precondition(assigned(duration: 0.1, speech: 0.061))
        precondition(!assigned(duration: 0.05, speech: 0.039), "The minimum voiced support is 40 ms")
        precondition(assigned(duration: 0.05, speech: 0.041))
        precondition(assigned(duration: 1.119, speech: 0.121), "Ordinary trailing silence must not fragment a spoken word")
        precondition(!assigned(duration: 1.122, speech: 0.121), "A long gap remains unassigned")
        precondition(!assigned(duration: 1.119, speech: 0.121, competing: true), "Even brief competing speech vetoes an assignment")
        precondition(assigned(duration: 0.32, speech: 0.223), "A slightly early ASR boundary can retain supported speech")
    }

    private static func checkChildBoundary() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("quill-speaker-checks-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: root) }
        let audio = root.appendingPathComponent("system.caf")
        try Data().write(to: audio)
        let fake = root.appendingPathComponent("fake-diarizer")
        let preamble = """
        #!/usr/bin/python3
        import json, os, signal, sys, time
        from pathlib import Path
        args = sys.argv[1:]
        assert args[0] == 'diarize-local'
        output = Path(args[args.index('--output') + 1])
        audio = Path(args[args.index('--input') + 1])
        if os.getpgrp() != os.getpid(): os.setpgid(0, 0)
        Path(str(audio) + '.worker').write_text(json.dumps({'pid': os.getpid(), 'directory': str(output.parent)}))

        """
        func script(_ body: String) throws {
            try (preamble + body + "\n").write(to: fake, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: fake.path)
        }
        func worker() throws -> [String: Any] {
            try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: audio.path + ".worker"))) as! [String: Any]
        }
        func checkRemoved() throws {
            let state = try worker()
            precondition(!FileManager.default.fileExists(atPath: state["directory"] as! String), "Temporary speaker output survived cleanup")
        }
        try script("output.write_text(json.dumps({'schema_version':1,'segments':[{'speaker':'S1','start':0,'end':1,'quality':1}]}))")
        let result = try await LocalDiarizationProcess.run(audio: audio, executable: fake, timeout: 3)
        precondition(result.count == 1 && result[0].speaker == "S1")
        try checkRemoved()

        for body in [
            "output.write_text(json.dumps({'schema_version':2,'segments':[]}))",
            "output.write_text(json.dumps({'schema_version':1,'segments':[{'speaker':'S1','start':-1,'end':1,'quality':1}]}))",
            "output.write_text(json.dumps({'schema_version':1,'segments':[{'speaker':'S1','start':0,'end':1,'quality':float('nan')}]}))",
            "output.write_text('x' * 20000001)",
            "output.symlink_to(audio)",
            "sys.exit(1)"
        ] {
            try script(body)
            do {
                _ = try await LocalDiarizationProcess.run(audio: audio, executable: fake, timeout: 3)
                preconditionFailure("Invalid child output was accepted")
            } catch {}
            try checkRemoved()
        }

        try script("signal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(0.1)")
        do {
            _ = try await LocalDiarizationProcess.run(audio: audio, executable: fake, timeout: 0.2)
            preconditionFailure("Speaker model timeout was ignored")
        } catch LocalDiarizationProcess.Failure.timedOut {}
        let timedOutPID = (try worker()["pid"] as! NSNumber).int32Value
        precondition(kill(timedOutPID, 0) != 0 && errno == ESRCH, "Timed-out helper was not reaped")
        try checkRemoved()

        try FileManager.default.removeItem(atPath: audio.path + ".worker")
        let task = Task { try await LocalDiarizationProcess.run(audio: audio, executable: fake, timeout: 10) }
        let startedBy = Date().addingTimeInterval(3)
        while !FileManager.default.fileExists(atPath: audio.path + ".worker") && Date() < startedBy {
            try await Task.sleep(for: .milliseconds(10))
        }
        let cancelledPID = (try worker()["pid"] as! NSNumber).int32Value
        task.cancel()
        do {
            _ = try await task.value
            preconditionFailure("Cancelled speaker work returned a result")
        } catch is CancellationError {}
        precondition(kill(cancelledPID, 0) != 0 && errno == ESRCH, "Cancelled helper was not reaped")
        try checkRemoved()
    }
}
