import AVFoundation
import CoreAudio
import Foundation

typealias SystemAudioBufferConsumer = @Sendable (AVAudioPCMBuffer, Double?) -> Void

/// Hardware boundary used by the recorder lifecycle checks. Production owns
/// one transport per attempt and only calls its methods on the control queue.
protocol SystemAudioCaptureTransport: AnyObject, Sendable {
    func start(isCurrent: @escaping @Sendable () -> Bool, prepare: @Sendable (AVAudioFormat) throws -> SystemAudioBufferConsumer,
               routeChanged: @escaping @Sendable () -> Void) throws
    func stop()
}

final class SystemAudioTransport: SystemAudioCaptureTransport, @unchecked Sendable {
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private let callbacks = DispatchQueue(label: "com.digimata.quill.system-tap.buffers")
    private let control: DispatchQueue
    private var routeListener: AudioObjectPropertyListenerBlock?
    private var registeredSelectors: [AudioObjectPropertySelector] = []
    private typealias RecorderError = SystemAudioRecorder.RecorderError

    init(control: DispatchQueue) { self.control = control }

    func start(isCurrent: @escaping @Sendable () -> Bool, prepare: @Sendable (AVAudioFormat) throws -> SystemAudioBufferConsumer,
               routeChanged: @escaping @Sendable () -> Void) throws {
        guard isCurrent() else { throw CancellationError() }
        let description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        description.name = "quill system tap"
        description.isPrivate = true
        description.muteBehavior = .unmuted
        var newTapID = AudioObjectID(kAudioObjectUnknown)
        let status = AudioHardwareCreateProcessTap(description, &newTapID)
        guard status == noErr else { throw RecorderError.tapCreationFailed(status) }
        tapID = newTapID
        do {
            let format = try tapStreamFormat()
            // The owner checks the synchronous Stop fence in this closure,
            // including a stop while the permission request was pending.
            let consume = try prepare(format)
            try createAggregateDevice(tapUUID: description.uuid)
            guard isCurrent() else { throw CancellationError() }
            try installIOProc(format: format, consume: consume, isCurrent: isCurrent)
            observeRoute(routeChanged)
        } catch { stop(); throw error }
    }

    private func tapStreamFormat() throws -> AVAudioFormat {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyFormat,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        let status = AudioObjectGetPropertyData(tapID, &address, 0, nil, &size, &asbd)
        guard status == noErr, let format = AVAudioFormat(streamDescription: &asbd) else {
            throw RecorderError.tapFormatUnreadable(status)
        }
        return format
    }

    private func createAggregateDevice(tapUUID: UUID) throws {
        let desc: [String: Any] = [
            kAudioAggregateDeviceNameKey: "quill-tap",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [] as [[String: Any]],
            kAudioAggregateDeviceTapListKey: [
                [
                    kAudioSubTapUIDKey: tapUUID.uuidString,
                    kAudioSubTapDriftCompensationKey: true,
                ]
            ],
        ]
        var newAggregateID = AudioObjectID(kAudioObjectUnknown)
        let status = AudioHardwareCreateAggregateDevice(desc as CFDictionary, &newAggregateID)
        guard status == noErr else { throw RecorderError.aggregateCreationFailed(status) }
        aggregateID = newAggregateID
    }

    private func installIOProc(format: AVAudioFormat, consume: @escaping SystemAudioBufferConsumer,
                               isCurrent: @Sendable () -> Bool) throws {
        var status = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, callbacks) {
            _, input, timestamp, _, _ in
            guard let buffer = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: input, deallocator: nil) else { return }
            let stamp = timestamp.pointee
            let seconds = stamp.mFlags.contains(.hostTimeValid)
                ? AVAudioTime.seconds(forHostTime: stamp.mHostTime) : nil
            consume(buffer, seconds)
        }
        guard status == noErr, let procID else { throw RecorderError.ioProcCreationFailed(status) }
        guard isCurrent() else { throw CancellationError() }
        status = AudioDeviceStart(aggregateID, procID)
        guard status == noErr else { throw RecorderError.deviceStartFailed(status) }
    }

    private func observeRoute(_ changed: @escaping @Sendable () -> Void) {
        let listener: AudioObjectPropertyListenerBlock = { _, _ in changed() }
        routeListener = listener
        for selector in [kAudioHardwarePropertyDefaultOutputDevice, kAudioHardwarePropertyDevices] {
            var address = AudioObjectPropertyAddress(mSelector: selector,
                mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
            let status = AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject),
                &address, control, listener)
            if status == noErr { registeredSelectors.append(selector) }
            else { FileHandle.standardError.write(Data("system route listener unavailable: \(status)\n".utf8)) }
        }
    }

    func stop() {
        if let routeListener {
            for selector in registeredSelectors {
                var address = AudioObjectPropertyAddress(mSelector: selector,
                    mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
                AudioObjectRemovePropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject),
                    &address, control, routeListener)
            }
        }
        routeListener = nil
        registeredSelectors = []
        if let procID, aggregateID != kAudioObjectUnknown {
            AudioDeviceStop(aggregateID, procID)
            AudioDeviceDestroyIOProcID(aggregateID, procID)
        }
        procID = nil
        // The callback owns its sink/converter. Drain before releasing any
        // native handles; no timeout may falsely claim a finalized recording.
        callbacks.sync {}
        if aggregateID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggregateID)
            aggregateID = AudioObjectID(kAudioObjectUnknown)
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
    }
}
