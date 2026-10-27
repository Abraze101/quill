#!/bin/sh
set -eu
repo_directory=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/quill-warning-history-checks.XXXXXX")
trap 'rm -rf "$test_directory"' EXIT HUP INT TERM
cd "$repo_directory"
xcrun swiftc -swift-version 6 -parse-as-library -target "$(uname -m)-apple-macosx15.0" \
  Sources/quill/RecordingWarningHistory.swift \
  Sources/quill/UI/RecordingWarningHistoryWindowController.swift \
  Tests/WarningHistoryChecks/WarningHistoryChecks.swift \
  -o "$test_directory/warning-history-checks"
"$test_directory/warning-history-checks"
