#!/bin/sh
set -eu
repo_directory=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/quill-audio-checks.XXXXXX")
trap 'rm -rf "$test_directory"' EXIT HUP INT TERM
cd "$repo_directory"
xcrun swiftc -swift-version 6 -parse-as-library \
  Sources/quill/RecordingCaptureTimeline.swift \
  Sources/quill/Audio/SerialAudioControl.swift \
  Sources/quill/Audio/MicCaptureState.swift \
  Sources/quill/Audio/SystemAudioCaptureState.swift \
  Sources/quill/RecordingSession.swift \
  Tests/AudioLifecycleChecks/AudioLifecycleChecks.swift \
  -o "$test_directory/audio-lifecycle-checks"
"$test_directory/audio-lifecycle-checks"
