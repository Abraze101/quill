#!/bin/sh
set -eu
quill_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$quill_root"
sh scripts/test-workflow-policies.sh
python3 -m unittest discover -s integrations/meeting-workflow/tests -v
node --test integrations/meeting-workflow/browser/*.test.cjs
sh integrations/meeting-workflow/tests/test-screen-capture.sh
sh integrations/meeting-workflow/tests/test-audio-lifecycle.sh
sh integrations/meeting-workflow/tests/test-crash-audio.sh
sh integrations/meeting-workflow/tests/test-recording-crash-recovery.sh
sh integrations/meeting-workflow/tests/test-recording-pause.sh
sh integrations/meeting-workflow/tests/test-sleep-wake.sh
sh integrations/meeting-workflow/tests/test-app-sleep.sh
sh integrations/meeting-workflow/tests/test-mic-recovery.sh
sh integrations/meeting-workflow/tests/test-system-audio-recovery.sh
sh integrations/meeting-workflow/tests/test-workflow-preferences.sh
sh integrations/meeting-workflow/tests/test-transcription-recovery.sh
sh integrations/meeting-workflow/tests/test-transcription-lease.sh
sh integrations/meeting-workflow/tests/test-speaker-identification.sh
sh integrations/meeting-workflow/tests/test-recording-controls.sh
sh integrations/meeting-workflow/tests/test-login-items.sh
sh integrations/meeting-workflow/tests/test-run-instance.sh
sh scripts/swift-local.sh build -c release --product quill
