#!/bin/sh
set -eu
quill_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
icon_work=$(mktemp -d "${TMPDIR:-/tmp}/quill-app-icon.XXXXXX")
trap 'rm -rf "$icon_work"' EXIT HUP INT TERM
xcrun swiftc "$quill_root/scripts/render-app-icon.swift" -o "$icon_work/render-app-icon"
"$icon_work/render-app-icon" "$quill_root/scripts/app-icon/Quill.svg" "$icon_work/Quill.iconset"
/usr/bin/iconutil -c icns "$icon_work/Quill.iconset" -o "$icon_work/Quill.icns"
cp "$icon_work/Quill.icns" "$quill_root/scripts/app-icon/Quill.icns"
