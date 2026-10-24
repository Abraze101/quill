import AVFoundation
import Foundation

/// AVFoundation calls these blocks on its own audio queues. Construct them
/// outside the recorder's main-actor isolation, and transfer only status values
/// to the main actor. PCM buffers stay synchronous with the locked audio sink.
enum MicAudioCallbacks {
    typealias Tap = @Sendable (AVAudioPCMBuffer, AVAudioTime) -> Void

    nonisolated static func tap(
        sink: MicAudioSink, converter: MicPCMConverter, generation: Int,
        onEvent: @escaping @MainActor @Sendable (MicAudioSink.Result, Int) -> Void
    ) -> Tap {
        { buffer, time in
            let result = sink.append(buffer, time: time, generation: generation, converter: converter)
            if result.firstBuffer || result.needsRawFallback || result.error != nil {
                Task { @MainActor in onEvent(result, generation) }
            }
        }
    }

    nonisolated static func configurationChange(
        generation: Int, onChange: @escaping @MainActor @Sendable (Int) -> Void
    ) -> @Sendable (Notification) -> Void {
        { _ in
            Task { @MainActor in onChange(generation) }
        }
    }
}
