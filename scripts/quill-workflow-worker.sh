#!/bin/sh
# Supervise only jobs already accepted by the completion queue while Quill runs.
set -eu
quill_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec /usr/bin/python3 "$quill_root/integrations/meeting-workflow/workflow.py" supervise --parent-pid "$PPID"
