import Darwin
import Foundation

/// One monotonic edit clock shared by microphone, system audio and screen video.
/// Native callbacks may arrive late or out of order; cuts use capture timestamps,
/// never callback arrival time. The lock only protects this small value history.
final class RecordingCaptureTimeline: @unchecked Sendable {
    struct Slice: Equatable, Sendable {
        let sourceStart: Double
        let sourceEnd: Double
        let outputStart: Double
    }
    struct AudioSlice: Equatable, Sendable {
        let sourceFrames: Range<Int64>
        let outputStart: Double
    }
    private struct Pause: Sendable { let start: Double; var end: Double? }
    private let lock = NSLock()
    private let clock: @Sendable () -> Double
    let startedAt: Double
    private var pauses: [Pause] = []
    private var stoppedAt: Double?

    init(clock: @escaping @Sendable () -> Double = { RecordingCaptureTimeline.now }) {
        self.clock = clock
        startedAt = clock()
    }

    static var now: Double {
        var info = mach_timebase_info_data_t()
        mach_timebase_info(&info)
        return Double(mach_absolute_time()) * Double(info.numer) / Double(info.denom) / 1_000_000_000
    }

    var isPaused: Bool { lock.withLock { pauses.last?.end == nil && !pauses.isEmpty } }
    var hasPauses: Bool { lock.withLock { !pauses.isEmpty } }
    var activeDuration: Double { lock.withLock { max(0, mapped(stoppedAt ?? clock()) - startedAt) } }
    var pausedDuration: Double { lock.withLock { let end = stoppedAt ?? clock(); return max(0, end - mapped(end)) } }

    @discardableResult
    func pause(at seconds: Double? = nil) -> Bool {
        lock.withLock {
            guard stoppedAt == nil, pauses.isEmpty || pauses.last?.end != nil else { return false }
            let now = seconds ?? clock()
            guard now.isFinite, now >= (pauses.last?.end ?? startedAt) else { return false }
            pauses.append(Pause(start: now))
            return true
        }
    }

    @discardableResult
    func resume(at seconds: Double? = nil) -> Bool {
        lock.withLock {
            guard stoppedAt == nil, let last = pauses.last, last.end == nil else { return false }
            let now = seconds ?? clock()
            guard now.isFinite, now >= last.start else { return false }
            pauses[pauses.count - 1].end = now
            return true
        }
    }

    func stop(at seconds: Double? = nil) {
        lock.withLock {
            guard stoppedAt == nil else { return }
            let now = seconds ?? clock()
            guard now.isFinite else { return }
            stoppedAt = max(now, pauses.last?.end ?? pauses.last?.start ?? startedAt)
        }
    }

    func outputTime(for seconds: Double) -> Double { lock.withLock { mapped(seconds) } }

    func activeSlices(from start: Double, to end: Double) -> [Slice] {
        lock.withLock { slices(from: start, to: end) }
    }

    func audioSlices(start: Double, frames: Int64, sampleRate: Double) -> [AudioSlice] {
        guard start.isFinite, frames > 0, sampleRate.isFinite, sampleRate > 0 else { return [] }
        return lock.withLock {
            slices(from: start, to: start + Double(frames) / sampleRate).compactMap { slice in
                // Round inward: no sample whose time falls inside a pause is retained.
                let lower = max(0, min(frames, Int64(ceil((slice.sourceStart - start) * sampleRate - 0.000_001))))
                let upper = max(lower, min(frames, Int64(floor((slice.sourceEnd - start) * sampleRate + 0.000_001))))
                guard lower < upper else { return nil }
                return AudioSlice(sourceFrames: lower..<upper,
                                  outputStart: mapped(start + Double(lower) / sampleRate))
            }
        }
    }

    private func slices(from start: Double, to end: Double) -> [Slice] {
        guard start.isFinite, end.isFinite, end > start else { return [] }
        let end = min(end, stoppedAt ?? end)
        var cursor = max(start, startedAt)
        var result: [Slice] = []
        for pause in pauses {
            guard cursor < end else { break }
            if let resumed = pause.end, resumed <= cursor { continue }
            if pause.start > cursor {
                let stop = min(end, pause.start)
                if stop > cursor { result.append(Slice(sourceStart: cursor, sourceEnd: stop, outputStart: mapped(cursor))) }
            }
            guard let resumed = pause.end else { return result }
            cursor = max(cursor, resumed)
        }
        if cursor < end { result.append(Slice(sourceStart: cursor, sourceEnd: end, outputStart: mapped(cursor))) }
        return result
    }

    private func mapped(_ seconds: Double) -> Double {
        var excluded = 0.0
        for pause in pauses {
            guard seconds > pause.start else { break }
            excluded += max(0, min(seconds, pause.end ?? seconds) - pause.start)
        }
        return seconds - excluded
    }
}
