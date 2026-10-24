#!/bin/sh
set -eu
repo_directory=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/quill-system-audio-checks.XXXXXX")
trap 'rm -rf "$test_directory"' EXIT HUP INT TERM
cd "$repo_directory"
xcrun swiftc -swift-version 6 -parse-as-library -Xfrontend -enable-actor-data-race-checks \
  -target "$(uname -m)-apple-macosx15.0" \
  Sources/quill/RecordingCaptureTimeline.swift \
  Sources/quill/Audio/MicCaptureState.swift \
  Sources/quill/Audio/MicAudioSink.swift \
  Sources/quill/Audio/SystemAudioCaptureState.swift \
  Sources/quill/Audio/SystemAudioSink.swift \
  Sources/quill/Audio/SystemAudioTransport.swift \
  Sources/quill/Audio/SystemAudioRecorder.swift \
  Tests/SystemAudioRecoveryChecks/SystemAudioRecoveryChecks.swift \
  -o "$test_directory/system-audio-recovery-checks"
"$test_directory/system-audio-recovery-checks"
