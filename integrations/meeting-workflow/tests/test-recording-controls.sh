#!/bin/sh
set -eu
repo_directory=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/quill-recording-control-checks.XXXXXX")
trap 'rm -rf "$test_directory"' EXIT HUP INT TERM
cd "$repo_directory"
xcrun swiftc -swift-version 6 -target "$(uname -m)-apple-macosx15.0" \
  Sources/quill/UI/RecordingStatusController.swift \
  Sources/quill/UI/MeetingReminderController.swift \
  Sources/quill/RecordingStatusHeartbeat.swift \
  Sources/quill/UI/MenuBarController.swift \
  Sources/quill/UI/SettingsWindowController.swift \
  Sources/quill/LoginItemController.swift \
  Tests/RecordingControlChecks/RecordingControlChecks.swift \
  -o "$test_directory/recording-controls-checks"
"$test_directory/recording-controls-checks"
