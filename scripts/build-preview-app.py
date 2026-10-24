"""Local release packaging, with an injectable runner for isolated tests.

The preview script/marker names remain stable for installer compatibility.
"""

import ctypes
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from xml.parsers.expat import ExpatError


SCHEMA = "quill-local-preview-v1"
MARKER = Path("Contents/Resources/quill-local-preview.json")
CONFIGURATION = "release"


class PackagingError(Exception):
    pass


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def require_idle(destination):
    """Inspect executable paths with libproc; never signal any process."""
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libproc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libproc.proc_listallpids.restype = ctypes.c_int
    libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    libproc.proc_pidpath.restype = ctypes.c_int
    count = libproc.proc_listallpids(None, 0)
    if count <= 0:
        raise PackagingError("Cannot check whether Quill is running; packaging stopped")
    capacity = count + 256
    pids = (ctypes.c_int * capacity)()
    found = libproc.proc_listallpids(pids, ctypes.sizeof(pids))
    if found <= 0 or found >= capacity:
        raise PackagingError("Process list changed while checking Quill; retry packaging")
    expected = str(destination / "Contents/MacOS/quill")
    for pid in pids[:found]:
        if pid <= 0:
            continue
        path = ctypes.create_string_buffer(4096)
        if libproc.proc_pidpath(pid, path, len(path)) > 0 and os.fsdecode(path.value) == expected:
            raise PackagingError(
                "Quill preview is running. Finish recording, turn automatic recording "
                "off, and quit Quill before packaging."
            )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def owned_output(path, root):
    marker = path / MARKER
    if path.is_symlink() or not path.is_dir():
        return False
    if any(part.is_symlink() for part in [path / "Contents", marker.parent, marker]):
        return False
    try:
        value = json.loads(marker.read_bytes())
        return value.get("schema") == SCHEMA and value.get("checkout") == str(root)
    except (OSError, ValueError, AttributeError):
        return False


def check_output(destination, root):
    if (destination.exists() or destination.is_symlink()) and not owned_output(destination, root):
        raise PackagingError("Refusing to replace an unrecognized .build/Quill.app")


def signing_identity(value, runner):
    if value == "-":
        return value
    # An exact existing certificate fingerprint avoids ambiguous name selection.
    if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise PackagingError("QUILL_SIGNING_IDENTITY must be '-' or an existing certificate's 40-character SHA-1 fingerprint")
    try:
        result = runner(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"], capture_output=True)
    except subprocess.CalledProcessError as error:
        raise PackagingError("Cannot verify the requested signing identity; no ad hoc fallback") from error
    available = {match.upper() for match in re.findall(r"^\s*\d+\)\s+([0-9a-fA-F]{40})\s", result.stdout, re.MULTILINE)}
    if value.upper() not in available:
        raise PackagingError("Requested signing identity is unavailable; no ad hoc fallback")
    return value.upper()


def configured_identity(value=None, *, config=None):
    """Keep the selected certificate outside the checkout and across rebuilds."""
    if value is not None:
        return value
    config = config or Path.home() / ".config/quill/signing.json"
    try:
        saved = json.loads(config.read_text())
        identity = saved["identity"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PackagingError(
            "A stable signing certificate is required. Configure ~/.config/quill/signing.json "
            "or set QUILL_SIGNING_IDENTITY to its exact fingerprint."
        ) from error
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", identity):
        raise PackagingError("Saved Quill signing identity must be a certificate fingerprint")
    return identity


def check_identity_continuity(destination, identity):
    if not destination.exists():
        return
    previous = json.loads((destination / MARKER).read_text()).get("signing_identity")
    if previous and previous != "-" and previous.upper() != identity.upper():
        raise PackagingError(
            "This would replace Quill's stable signing identity. Keep the original "
            "certificate; review a deliberate identity migration separately."
        )


def validate_app(app, runner):
    runner(["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(app)])
    runner(["/usr/bin/plutil", "-lint", str(app / "Contents/Info.plist")])
    runner([str(app / "Contents/MacOS/quill"), "doctor"])


def source_info(root):
    """Require intentional derivative versioning before building or signing."""
    try:
        raw = (root / "Sources/quill/Info.plist").read_bytes()
        info = plistlib.loads(raw)
    except (OSError, ValueError, ExpatError) as error:
        raise PackagingError("Cannot read the source Info.plist; packaging stopped") from error
    if not isinstance(info, dict) or info.get("CFBundleIdentifier") != "com.digimata.quill":
        raise PackagingError("Review the changed bundle identifier before packaging")
    version, build_number = info.get("CFBundleShortVersionString"), info.get("CFBundleVersion")
    if (not isinstance(version, str)
            or not re.fullmatch(r"(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})", version)):
        raise PackagingError("Source CFBundleShortVersionString must be an explicit numeric major.minor.patch version")
    if not isinstance(build_number, str) or not re.fullmatch(r"[1-9][0-9]{0,3}", build_number):
        raise PackagingError("Source CFBundleVersion must be an explicit positive build number from 1 to 9999")
    return info, raw


def check_source_info(root, expected):
    """Keep compiled metadata, the external plist, and signed marker aligned."""
    try:
        actual = (root / "Sources/quill/Info.plist").read_bytes()
    except OSError as error:
        raise PackagingError("Source Info.plist became unavailable during packaging") from error
    if actual != expected:
        raise PackagingError("Source Info.plist changed during packaging; rebuild from stable source metadata")


def package(root, identity=None, *, runner=run, idle_check=require_idle):
    root = Path(root).resolve()
    build = root / ".build"
    destination = build / "Quill.app"
    # Refuse a running preview before building, signing, or making a lock file.
    idle_check(destination)
    check_output(destination, root)
    if build.is_symlink() or (build.exists() and not build.is_dir()):
        raise PackagingError("Expected this checkout's ordinary .build directory")
    source_info(root)
    identity = signing_identity(configured_identity(identity), runner)
    check_identity_continuity(destination, identity)
    build.mkdir(exist_ok=True)
    lock_fd = os.open(build / ".quill-preview.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PackagingError("Another preview packager is running") from error
        idle_check(destination)
        check_output(destination, root)
        check_identity_continuity(destination, identity)
        info, source_plist = source_info(root)
        # Always build: an existing executable may predate the source/plist.
        runner(["sh", str(root / "scripts/swift-local.sh"), "build", "-c", CONFIGURATION,
                "--product", "quill"], cwd=str(root))
        check_source_info(root, source_plist)
        # A failed or missing release is an error, even if a debug build exists.
        binary = (build / CONFIGURATION / "quill").resolve(strict=True)
        source_hash = digest(binary)
        info.update({
            "CFBundleExecutable": "quill",
            "CFBundlePackageType": "APPL",
            "CFBundleDisplayName": "Quill",
            "CFBundleIconFile": "Quill.icns",
            "CFBundleInfoDictionaryVersion": "6.0",
            "LSUIElement": True,
            "LSMinimumSystemVersion": "15.0",
        })
        staging = Path(tempfile.mkdtemp(prefix=".quill-preview-", dir=build))
        preserve_recovery = False
        try:
            app = staging / "Quill.app"
            executable = app / "Contents/MacOS/quill"
            executable.parent.mkdir(parents=True)
            shutil.copy2(binary, executable)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
            marker = app / MARKER
            marker.parent.mkdir()
            # Bundle artwork before signing so every install carries the icon
            # and the signature seals it alongside the other app resources.
            shutil.copy2(root / "scripts/app-icon/Quill.icns", marker.parent / "Quill.icns")
            marker.write_text(json.dumps({
                "schema": SCHEMA, "checkout": str(root),
                "source_binary_sha256": source_hash, "signing_identity": identity,
                "build_configuration": CONFIGURATION,
                "version": info["CFBundleShortVersionString"], "build_number": info["CFBundleVersion"],
                "packaged_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2) + "\n")
            # Sign only the independent copy, sealing its external plist/resources.
            runner(["/usr/bin/codesign", "--force", "--sign", identity, "--timestamp=none", str(app)])
            validate_app(app, runner)
            if digest(binary) != source_hash:
                raise PackagingError("Source binary changed during packaging; rerun after the build finishes")

            # A user may have opened the existing preview during the build.
            idle_check(destination)
            check_output(destination, root)
            check_identity_continuity(destination, identity)
            check_source_info(root, source_plist)
            previous = staging / "previous.app"
            published = False
            try:
                if destination.exists():
                    destination.rename(previous)
                app.rename(destination)
                published = True
                validate_app(destination, runner)
                if digest(binary) != source_hash:
                    raise PackagingError("Source binary changed during verification")
                check_source_info(root, source_plist)
            except BaseException:
                try:
                    if published:
                        check_output(destination, root)
                        destination.rename(staging / "failed.app")
                    if previous.exists():
                        previous.rename(destination)
                except BaseException as error:
                    preserve_recovery = True
                    raise PackagingError(f"Could not restore the previous preview. Recovery files preserved at {staging}") from error
                raise
        finally:
            # Keep the previous app until installed validation passes. If restore
            # itself fails, retain recovery files instead of deleting the backup.
            if not preserve_recovery:
                shutil.rmtree(staging)
    return destination


if __name__ == "__main__":
    try:
        output = package(sys.argv[1], os.environ.get("QUILL_SIGNING_IDENTITY"))
    except (PackagingError, OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
    print("Preview ready:", output)
    print("Fresh release build packaged; source executable not re-signed. No app launched or login item installed.")
