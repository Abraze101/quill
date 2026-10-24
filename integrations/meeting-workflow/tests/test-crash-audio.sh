#!/bin/sh
set -eu
repo_directory=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/quill-crash-audio.XXXXXX")
trap 'rm -rf "$test_directory"' EXIT HUP INT TERM
cd "$repo_directory"

# The native crash checks need no SwiftPM build. When its resolved checkout is
# available, compile the actual dependency preprocessing sources too; no model
# or network access is involved. An explicit alternate checkout must exist.
fluid_directory=${QUILL_FLUIDAUDIO_CHECKOUT:-"$repo_directory/.build/checkouts/FluidAudio"}
set --
if [ -f "$fluid_directory/Sources/FluidAudio/Shared/AudioConverter.swift" ]; then
  set -- -D QUILL_FLUID_AUDIO_CONVERTER \
    "$fluid_directory/Sources/FluidAudio/Shared/AppLogger.swift" \
    "$fluid_directory/Sources/FluidAudio/Shared/AudioConverter.swift"
elif [ -n "${QUILL_FLUIDAUDIO_CHECKOUT:-}" ]; then
  echo "Explicit FluidAudio checkout is missing its preprocessing sources" >&2
  exit 1
else
  echo "SKIP FluidAudio preprocessing: dependency checkout not available; AVAudioFile compatibility still checked"
fi
xcrun swiftc -swift-version 6 -parse-as-library -Xfrontend -enable-actor-data-race-checks \
  -target "$(uname -m)-apple-macosx15.0" \
  Sources/quill/RecordingCaptureTimeline.swift \
  Sources/quill/Audio/MicCaptureState.swift \
  Sources/quill/Audio/MicAudioSink.swift \
  Sources/quill/Audio/SystemAudioSink.swift \
  "$@" Tests/CrashAudioChecks/CrashAudioChecks.swift \
  -o "$test_directory/crash-audio-checks"
"$test_directory/crash-audio-checks"
if [ "$#" -gt 0 ]; then
  echo "PASS actual FluidAudio preprocessing accepts crashed PCM and historical AAC at 16 kHz mono"
fi
