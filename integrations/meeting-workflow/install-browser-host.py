#!/usr/bin/env python3
"""Register only the user-selected unpacked detector extension."""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import tempfile


def regular_bytes(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Refusing a linked or non-file install target: " + path.name)
    return path.read_bytes() if path.exists() else None


def atomic_write(path, data, mode):
    previous = regular_bytes(path)
    if previous == data and stat.S_IMODE(path.stat().st_mode) == mode:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".quill-install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def manifest_for(host, extension_id):
    return {"name": "com.digimata.quill.meetings", "description": "Local Quill meeting state bridge",
            "path": str(host), "type": "stdio",
            "allowed_origins": ["chrome-extension://" + extension_id + "/"]}


def install(browser, extension_id):
    source = Path(__file__).resolve().parent / "native-host.py"
    support = Path.home() / "Library/Application Support"
    bridge = support / "Quill/BrowserBridge"
    host = bridge / "native-host.py"
    folder = {"chrome": "Google/Chrome", "edge": "Microsoft Edge"}[browser]
    destination = support / folder / "NativeMessagingHosts/com.digimata.quill.meetings.json"
    backup = destination.with_name(destination.name + ".repo-backup")
    manifest = manifest_for(host, extension_id)
    legacy = manifest_for(source, extension_id)
    previous = regular_bytes(destination)
    migrating = False
    if previous is not None:
        registered = json.loads(previous)
        if registered == legacy:
            migrating = True
            original = regular_bytes(backup)
            if original is not None and json.loads(original) != legacy:
                raise ValueError("An existing registration backup differs; inspect it before changing it.")
        elif registered != manifest:
            raise ValueError("An existing host registration differs; inspect it before changing it.")

    # The installed host is a real file with only standard-library imports.
    # Chrome must never need to read code from the Documents checkout at runtime.
    payload = regular_bytes(source)
    if payload is None:
        raise ValueError("The source native host is missing")
    regular_bytes(host)
    for directory in (bridge.parent, bridge):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError("Refusing a linked or non-directory bridge target")
    bridge.mkdir(parents=True, exist_ok=True, mode=0o700)
    bridge.chmod(0o700)
    atomic_write(host, payload, 0o700)
    if migrating and not backup.exists():
        # Preserve the exact original bytes once, before switching registration.
        atomic_write(backup, previous, 0o600)
    encoded = (json.dumps(manifest, indent=2) + "\n").encode()
    atomic_write(destination, encoded, 0o600)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", choices=("chrome", "edge"), required=True)
    parser.add_argument("--extension-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-p]{32}", args.extension_id):
        parser.error("Use the exact extension ID shown by your browser")
    os.umask(0o077)
    try:
        destination = install(args.browser, args.extension_id)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print("Registered", destination)


if __name__ == "__main__":
    main()
