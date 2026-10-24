#!/bin/sh
set -eu
repo_directory=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/quill-transcription-lease.XXXXXX")
trap 'rm -rf "$test_directory"' EXIT HUP INT TERM
cd "$repo_directory"
xcrun swiftc -swift-version 6 -parse-as-library -target "$(uname -m)-apple-macosx15.0" -emit-library -emit-module \
  -module-name MeetingWorkflow Sources/MeetingWorkflow/*.swift \
  -o "$test_directory/libMeetingWorkflow.dylib" \
  -emit-module-path "$test_directory/MeetingWorkflow.swiftmodule"
xcrun swiftc -swift-version 6 -parse-as-library -target "$(uname -m)-apple-macosx15.0" \
  -I "$test_directory" -L "$test_directory" -lMeetingWorkflow \
  -Xlinker -rpath -Xlinker "$test_directory" \
  Sources/quill/Transcription/TranscriptionEngine.swift \
  Sources/quill/Transcription/TranscriptionCoordinator.swift \
  Tests/TranscriptionLeaseChecks/TranscriptionLeaseChecks.swift \
  -o "$test_directory/transcription-lease-checks"
"$test_directory/transcription-lease-checks"
