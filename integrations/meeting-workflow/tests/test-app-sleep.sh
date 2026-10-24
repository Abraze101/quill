#!/bin/sh
set -eu
repo_directory=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/quill-app-sleep-checks.XXXXXX")
trap 'rm -rf "$test_directory"' EXIT HUP INT TERM
cd "$repo_directory"
# Build an isolated real MeetingWorkflow module; do not depend on a stale app
# build or link any installed application/recording resources.
xcrun swiftc -swift-version 6 -parse-as-library -target "$(uname -m)-apple-macosx15.0" -emit-library -emit-module \
  -module-name MeetingWorkflow Sources/MeetingWorkflow/*.swift \
  -o "$test_directory/libMeetingWorkflow.dylib" \
  -emit-module-path "$test_directory/MeetingWorkflow.swiftmodule"
# Extract unchanged display value types, omitting all AppKit controller bodies.
# AppController itself is copied byte-for-byte, with a same-file test extension
# appended solely so tests can inspect and invoke its private coordinator state.
python3 - "$test_directory" <<'PY'
from pathlib import Path
import sys
out = Path(sys.argv[1])
models = []
for name, marker in [
    ('UI/MenuBarController.swift', 'final class MenuBarController'),
    ('UI/SettingsWindowController.swift', 'final class SettingsWindowController'),
    ('UI/RecordingStatusController.swift', 'final class RecordingStatusController'),
]:
    source = (Path('Sources/quill') / name).read_text()
    prefix, found, _ = source.partition(marker)
    assert found and prefix.rstrip().endswith('@MainActor'), name
    models.append(prefix.rsplit('@MainActor', 1)[0])
(out / 'DisplayModels.swift').write_text('\n'.join(models))
(out / 'AppControllerChecks.swift').write_text(
    Path('Sources/quill/AppController.swift').read_text() + '\n' +
    Path('Tests/AppSleepChecks/AppSleepChecks.swift').read_text())
PY
xcrun swiftc -swift-version 6 -parse-as-library -target "$(uname -m)-apple-macosx15.0" \
  -I "$test_directory" -L "$test_directory" -lMeetingWorkflow \
  -Xlinker -rpath -Xlinker "$test_directory" \
  Sources/quill/RecordingSession.swift Sources/quill/RecordingCrashRecovery.swift Sources/quill/RecordingCaptureTimeline.swift Sources/quill/RecordingSleepController.swift \
  Sources/quill/RecordingSafetyClock.swift Sources/quill/Audio/MicCaptureState.swift \
  Sources/quill/Audio/SystemAudioCaptureState.swift \
  Sources/quill/WorkflowPreferences.swift Sources/quill/CompletionUIStatus.swift \
  "$test_directory/DisplayModels.swift" Tests/AppSleepChecks/BoundaryFakes.swift \
  "$test_directory/AppControllerChecks.swift" -o "$test_directory/app-sleep-checks"
"$test_directory/app-sleep-checks"
