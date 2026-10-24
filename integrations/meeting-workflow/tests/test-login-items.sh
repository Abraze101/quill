#!/bin/sh
set -eu
repo_directory=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/quill-login-items.XXXXXX")
trap 'rm -rf "$test_directory"' EXIT HUP INT TERM
cd "$repo_directory"
xcrun swiftc -swift-version 6 -target "$(uname -m)-apple-macosx15.0" \
  Sources/quill/LoginItemController.swift \
  Tests/LoginItemChecks/LoginItemChecks.swift \
  -o "$test_directory/login-item-checks"
"$test_directory/login-item-checks"
