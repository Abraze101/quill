#!/bin/sh
# Opt-in workaround for mixed-version Command Line Tools installations.
# Copies only SwiftPM's modules into this checkout; never modifies the SDK.
set -eu
quill_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
quill_compiler=$(xcrun --find swiftc)
quill_pm=$(dirname "$quill_compiler")/../lib/swift/pm
quill_overlay="$quill_root/.build/local-swiftpm"
mkdir -p "$quill_overlay"
for quill_api in ManifestAPI PluginAPI; do
    if [ ! -d "$quill_overlay/$quill_api" ]; then
        cp -R "$quill_pm/$quill_api" "$quill_overlay/$quill_api"
    fi
    for quill_module in "$quill_overlay/$quill_api/"*.swiftmodule; do
        for quill_interface in "$quill_module/"*.swiftinterface; do
            case "$quill_interface" in *.private.swiftinterface|*.package.swiftinterface) continue ;; esac
            cp "$quill_interface" "${quill_interface%.swiftinterface}.private.swiftinterface"
        done
    done
done
cd "$quill_root"
SWIFTPM_CUSTOM_LIBS_DIR="$quill_overlay" swift "$@"
