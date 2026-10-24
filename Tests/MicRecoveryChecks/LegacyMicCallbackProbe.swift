import AVFoundation
import Foundation

// Deliberately reproduce the old callback boundary in a child process. This
// immutable test box permits dispatching the legacy, non-Sendable framework
// block; it does not make the block's inherited MainActor isolation valid.
private final class LegacyTap: @unchecked Sendable {
    // Keep the Objective-C block boundary used by installTap; an ordinary
    // Swift function call does not exercise the imported callback thunk.
    typealias Block = @convention(block) (AVAudioPCMBuffer, AVAudioTime) -> Void
    let block: Block
    init(_ block: @escaping Block) { self.block = block }
}

@MainActor
private final class LegacyRecorder {
    var events = 0

    func makeTap() -> LegacyTap {
        LegacyTap { [weak self] _, _ in
            Task { @MainActor [weak self] in self?.events += 1 }
        }
    }
}

@main
private struct LegacyMicCallbackProbe {
    @MainActor
    static func main() async {
        let recorder = LegacyRecorder()
        let tap = recorder.makeTap()
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                dispatchPrecondition(condition: .notOnQueue(.main))
                let format = AVAudioFormat(standardFormatWithSampleRate: 24_000, channels: 1)!
                let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 1)!
                FileHandle.standardOutput.write(Data("Invoking legacy tap off-main\n".utf8))
                tap.block(buffer, AVAudioTime(hostTime: mach_absolute_time()))
                continuation.resume()
            }
        }
        precondition(recorder.events >= 0)
    }
}
