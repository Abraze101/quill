#!/bin/sh
# Build first, then launch the policy checks without SwiftPM's executable wrapper.
set -eu
quill_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$quill_root"
sh scripts/swift-local.sh build --product meeting-workflow-checks
quill_check_bin=$(sh scripts/swift-local.sh build --show-bin-path)
exec "$quill_check_bin/meeting-workflow-checks"
