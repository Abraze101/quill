#!/bin/sh
set -eu
repo_directory=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/quill-mic-checks.XXXXXX")
trap 'rm -rf "$test_directory"' EXIT HUP INT TERM
cd "$repo_directory"
xcrun swiftc -swift-version 6 -parse-as-library -Xfrontend -enable-actor-data-race-checks \
  -target "$(uname -m)-apple-macosx15.0" \
  Tests/MicRecoveryChecks/LegacyMicCallbackProbe.swift \
  -o "$test_directory/legacy-mic-callback-probe"
# The old callback must reproduce the isolation failure in a separate process;
# no microphone, capture device, or existing recording is opened by this probe.
python3 - "$test_directory/legacy-mic-callback-probe" <<'PY'
import resource
import signal
import subprocess
import sys

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
result = subprocess.run([sys.argv[1]], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
if result.returncode != -signal.SIGTRAP or b"Invoking legacy tap off-main" not in result.stdout:
    raise SystemExit("Legacy tap did not reproduce the expected isolation SIGTRAP: " + str(result.returncode))
print("PASS isolated legacy callback reproduces SIGTRAP off-main (regression control)")
PY
xcrun swiftc -swift-version 6 -parse-as-library -Xfrontend -enable-actor-data-race-checks \
  -target "$(uname -m)-apple-macosx15.0" \
  Sources/quill/RecordingCaptureTimeline.swift \
  Sources/quill/Audio/MicCaptureState.swift \
  Sources/quill/Audio/MicAudioSink.swift \
  Sources/quill/Audio/MicAudioCallbacks.swift \
  Tests/MicRecoveryChecks/MicRecoveryChecks.swift \
  -o "$test_directory/mic-recovery-checks"
"$test_directory/mic-recovery-checks"
