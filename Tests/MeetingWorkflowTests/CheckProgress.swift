import Foundation

/// Write each test boundary directly so piped CI logs identify a stalled check.
/// This avoids stdout buffering without changing test scheduling or behavior.
func reportCheckProgress(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}
