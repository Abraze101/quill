#!/bin/sh
set -eu
repo_directory=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/quill-pause-checks.XXXXXX")
trap 'rm -rf "$test_directory"' EXIT HUP INT TERM
cd "$repo_directory"
xcrun swiftc -swift-version 6 -parse-as-library -Xfrontend -enable-actor-data-race-checks \
  -target "$(uname -m)-apple-macosx15.0" \
  Sources/quill/RecordingCaptureTimeline.swift \
  Sources/quill/Audio/MicCaptureState.swift Sources/quill/Audio/MicAudioSink.swift \
  Sources/quill/Audio/SystemAudioSink.swift \
  Sources/quill/ScreenCapture/ScreenCaptureCatalog.swift \
  Sources/quill/ScreenCapture/ScreenVideoRecorder.swift \
  Sources/quill/ScreenCapture/PausableScreenVideoRecorder.swift \
  Tests/RecordingPauseChecks/RecordingPauseChecks.swift \
  -o "$test_directory/recording-pause-checks"
"$test_directory/recording-pause-checks"
