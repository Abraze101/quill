#!/bin/sh
# Build and package a local preview; never launch it or install a login item.
set -eu
quill_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec /usr/bin/python3 "$quill_root/scripts/build-preview-app.py" "$quill_root"
