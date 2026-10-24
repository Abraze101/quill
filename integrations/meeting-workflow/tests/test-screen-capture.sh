#!/bin/sh
set -eu

repo_directory=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/quill-screen-checks.XXXXXX")
trap 'rm -rf "$test_directory"' EXIT HUP INT TERM

cd "$repo_directory"
xcrun swiftc -swift-version 6 -target "$(uname -m)-apple-macosx15.0" \
  Sources/quill/RecordingCaptureTimeline.swift \
  Sources/quill/ScreenCapture/ScreenCaptureCatalog.swift \
  Sources/quill/ScreenCapture/ScreenVideoRecorder.swift \
  Sources/quill/ScreenCapture/PausableScreenVideoRecorder.swift \
  integrations/meeting-workflow/tests/ScreenCaptureChecks.swift \
  -o "$test_directory/screen-capture-checks"
"$test_directory/screen-capture-checks"
