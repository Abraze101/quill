import Darwin
import Foundation

/// Unlike wall time, this clock cannot move backwards when the user changes
/// the date. It includes sleep, so waking after a deadline stops the session.
enum RecordingSafetyClock {
    static var now: TimeInterval {
        TimeInterval(clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW)) / 1_000_000_000
    }
}
