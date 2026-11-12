#!/bin/sh
# on_stop only: never enumerate or enqueue pre-existing recordings.
set -eu
quill_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ "$#" -eq 2 ] && [ "$1" = "--recording-ready" ]; then
    exec /usr/bin/python3 "$quill_root/integrations/meeting-workflow/workflow.py" recording-ready "$2"
fi
if [ "$#" -eq 2 ] && [ "$1" = "--transcription-unavailable" ]; then
    exec /usr/bin/python3 "$quill_root/integrations/meeting-workflow/workflow.py" complete "$2"
fi
if [ "$#" -ne 1 ]; then
    printf 'Usage: quill-workflow-hook.sh SESSION_DIRECTORY\n' >&2
    exit 64
fi
exec /usr/bin/python3 "$quill_root/integrations/meeting-workflow/workflow.py" complete "$1"
