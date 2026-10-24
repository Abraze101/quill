#!/bin/sh
set -eu
repo_directory=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/quill-transcription-recovery.XXXXXX")
trap 'rm -rf "$test_directory"' EXIT HUP INT TERM
cd "$repo_directory"
xcrun swiftc -swift-version 6 -target "$(uname -m)-apple-macosx15.0" \
  Sources/MeetingWorkflow/TranscriptionRecovery.swift \
  Sources/MeetingWorkflow/RecordingReviewGate.swift \
  Tests/TranscriptionRecoveryChecks/TranscriptionRecoveryChecks.swift \
  -o "$test_directory/transcription-recovery-checks"
"$test_directory/transcription-recovery-checks"
