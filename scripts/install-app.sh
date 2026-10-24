#!/bin/sh
# Install the existing signed preview separately from building or launching it.
set -eu
quill_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec /usr/bin/python3 "$quill_root/scripts/install-app.py" "$quill_root" "$@"
