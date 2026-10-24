#!/bin/sh
# on_stop only: never enumerate or enqueue pre-existing recordings.
set -eu
quill_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ "$#" -ne 1 ]; then
    printf 'Usage: quill-workflow-hook.sh SESSION_DIRECTORY\n' >&2
    exit 64
fi
exec /usr/bin/python3 "$quill_root/integrations/meeting-workflow/workflow.py" complete "$1"
