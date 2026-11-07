import Darwin
import Foundation

/// Exercise the real child process and browser handoff without opening a browser
/// or reading any recording, workflow settings, or saved meeting choices.
@MainActor
func meetingReviewLauncherChecks() async throws {
    let files = FileManager.default
    let directory = files.temporaryDirectory.appendingPathComponent("quill-review-check-\(UUID())")
    try files.createDirectory(at: directory, withIntermediateDirectories: true)
    defer { try? files.removeItem(at: directory) }
    let workflowDirectory = directory.appendingPathComponent("integrations/meeting-workflow")
    try files.createDirectory(at: workflowDirectory, withIntermediateDirectories: true)
    let script = workflowDirectory.appendingPathComponent("workflow.py")
    try #"""
    import json, os, sys, time
    from pathlib import Path
    directory = Path(__file__).resolve().parents[2]
    if "status" in sys.argv:
        temporary = directory / "custom-arguments.tmp"
        temporary.write_text(json.dumps(sys.argv[1:]))
        temporary.rename(directory / "custom-arguments")
        sys.exit()
    starts = directory / "starts"
    with starts.open("a") as out:
        print(os.getpid(), file=out)
    sequence = len(starts.read_text().splitlines())
    assert sys.argv[1:].count("--no-open") == sys.argv[1:].count("--print-url") == 1
    assert sys.argv[1] == "--state" and Path(sys.argv[2]).samefile(directory) and "review" in sys.argv[3:]
    end = time.monotonic() + 8
    while not (directory / "ready").exists() and time.monotonic() < end:
        time.sleep(.01)
    if not directory.exists():
        sys.exit()
    address = "http://127.0.0.1:49158/" + chr(64 + sequence) * 43 + "/"
    override = directory / "override"
    if override.exists():
        address = override.read_text()
    # A pipe read can split the URL anywhere.
    os.write(1, address[:12].encode())
    time.sleep(.02)
    os.write(1, (address[12:] + "\n").encode())
    while (directory.exists() and not (directory / ("exit-" + str(sequence))).exists()
           and time.monotonic() < end):
        time.sleep(.01)
    """#.write(to: script, atomically: true, encoding: .utf8)

    let command = ["/usr/bin/python3", script.path, "--state", directory.path, "review"]
    var opened = [URL]()
    var errors = [String]()
    let launcher = MeetingReviewLauncher(openURL: { opened.append($0); return true },
                                         reportError: { errors.append($0) })
    defer { launcher.close() }
    func started() -> [pid_t] {
        guard let text = try? String(contentsOf: directory.appendingPathComponent("starts"), encoding: .utf8)
        else { return [] }
        return text.split(separator: "\n").compactMap { pid_t($0) }
    }
    func wait(_ description: String, until ready: () -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(5))
        while !ready() {
            if ContinuousClock.now >= deadline {
                throw NSError(domain: "MeetingReviewChecks", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: description])
            }
            try await Task.sleep(for: .milliseconds(10))
        }
    }
    func write(_ name: String, _ value: String = "") throws {
        try value.write(to: directory.appendingPathComponent(name), atomically: true, encoding: .utf8)
    }

    launcher.open(command)
    launcher.open(command)
    try await wait("Review helper did not start") { !started().isEmpty }
    try write("ready")
    try await wait("The initial review never opened") { !opened.isEmpty }
    precondition(opened.count == 1 && started().count == 1 && errors.isEmpty)
    let firstURL = opened[0]
    let firstPID = started()[0]

    // The tab is closed but the helper is still running. A second click must
    // reach the browser again with the same session and no duplicate child.
    launcher.open(command)
    try await wait("A closed review tab could not be reopened") { opened.count == 2 }
    precondition(opened[1] == firstURL && started() == [firstPID] && errors.isEmpty)

    try write("exit-1")
    try await wait("The first review helper did not exit") { kill(firstPID, 0) == -1 && errno == ESRCH }
    launcher.open(command)
    try await wait("Expired review session did not restart") { opened.count == 3 }
    precondition(opened[2] != firstURL && started().count == 2 && errors.isEmpty)
    let secondPID = started()[1]
    try write("exit-2")
    try await wait("The replacement helper did not exit") { kill(secondPID, 0) == -1 && errno == ESRCH }

    try write("override", "http://127.0.0.1@example.com:49158/" + String(repeating: "Z", count: 43) + "/")
    launcher.open(command)
    try await wait("Unsafe helper output was not reported") { !errors.isEmpty }
    precondition(opened.count == 3, "A non-loopback review URL reached the browser")
    try files.removeItem(at: directory.appendingPathComponent("override"))
    launcher.open(command)
    try await wait("A failed review start blocked the next request") { opened.count == 4 }
    precondition(started().count == 4 && errors.count == 1)

    // "review" can also be a value passed to another configured command.
    let otherArguments = ["--state", "review", "status"]
    launcher.open(["/usr/bin/python3", script.path] + otherArguments)
    let savedArguments = directory.appendingPathComponent("custom-arguments")
    try await wait("The custom command did not run") { files.fileExists(atPath: savedArguments.path) }
    let receivedArguments = try JSONDecoder().decode([String].self, from: Data(contentsOf: savedArguments))
    precondition(receivedArguments == otherArguments && opened.count == 4)
}
