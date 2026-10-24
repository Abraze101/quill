#!/bin/sh
set -eu
repo_directory=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/quill-speaker-identification.XXXXXX")
trap 'rm -rf "$test_directory"' EXIT HUP INT TERM
cd "$repo_directory"
xcrun swiftc -swift-version 6 -parse-as-library -target "$(uname -m)-apple-macosx15.0" \
  Sources/quill/Transcription/TranscriptionEngine.swift \
  Sources/quill/Transcription/SpeakerAlignment.swift \
  Sources/quill/Transcription/LocalDiarizationProcess.swift \
  Tests/SpeakerIdentificationChecks/SpeakerIdentificationChecks.swift \
  -o "$test_directory/speaker-identification-checks"
"$test_directory/speaker-identification-checks"
