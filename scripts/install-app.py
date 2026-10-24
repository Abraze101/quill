"""Install an already signed local Quill preview without launching or changing it."""

import argparse
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
import stat
import subprocess
import tempfile


SCHEMA = "quill-local-preview-v1"
MARKER = Path("Contents/Resources/quill-local-preview.json")
BUNDLE_ID = "com.digimata.quill"
DEFAULT_DESTINATION = Path("/Applications/Quill.app")
BACKUP_SCHEMA = "quill-app-install-backup-v1"


class InstallError(Exception):
    pass


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def require_idle(destination):
    """Read executable paths through libproc; never signal or launch a process."""
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libproc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libproc.proc_listallpids.restype = ctypes.c_int
    libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    libproc.proc_pidpath.restype = ctypes.c_int
    count = libproc.proc_listallpids(None, 0)
    if count <= 0:
        raise InstallError("Cannot check whether Quill is running; installation stopped")
    capacity = count + 256
    pids = (ctypes.c_int * capacity)()
    found = libproc.proc_listallpids(pids, ctypes.sizeof(pids))
    if found <= 0 or found >= capacity:
        raise InstallError("Process list changed while checking Quill; retry installation")
    expected = str(destination / "Contents/MacOS/quill")
    for pid in pids[:found]:
        if pid <= 0:
            continue
        path = ctypes.create_string_buffer(4096)
        if libproc.proc_pidpath(pid, path, len(path)) > 0 and os.fsdecode(path.value) == expected:
            raise InstallError(
                "The destination Quill app is running. Finish recording and quit that app "
                "before installing. No process was stopped."
            )


def ordinary_path(path):
    """Refuse aliases through symlinked ancestors, including dangling links."""
    if not path.is_absolute():
        raise InstallError("Expected an absolute application path")
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise InstallError(f"Refusing a symlinked application path: {part}")


def app_manifest(app, root):
    ordinary_path(app)
    if not app.is_dir():
        raise InstallError(f"Expected an existing ordinary Quill app: {app}")
    try:
        # This local wrapper has no frameworks or symlink-based resources.
        # Refuse links/special files before copying or trusting its marker.
        for entry in app.rglob("*"):
            mode = entry.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise InstallError(f"Refusing a linked or special application file: {entry}")
        marker = json.loads((app / MARKER).read_bytes())
        if marker.get("schema") != SCHEMA or marker.get("checkout") != str(root):
            raise InstallError(f"Refusing an unrecognized Quill app: {app}")
        identity = marker.get("signing_identity", "")
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9A-Fa-f]{40}", identity):
            raise InstallError("Installation requires Quill's stable certificate; ad hoc signatures are not accepted")
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        if (info.get("CFBundleIdentifier") != BUNDLE_ID
                or info.get("CFBundleExecutable") != "quill"
                or info.get("CFBundlePackageType") != "APPL"):
            raise InstallError("Unexpected Quill bundle identifier, executable, or application type")
        executable = app / "Contents/MacOS/quill"
        if not executable.is_file() or not os.access(executable, os.X_OK) or executable.stat().st_size == 0:
            raise InstallError("Quill's application executable is missing or not executable")
        return identity.upper()
    except (OSError, ValueError, AttributeError, TypeError) as error:
        raise InstallError(f"Cannot read Quill's signed application metadata: {app}") from error


def fingerprint(app):
    """Compare all ordinary bundle bytes and modes, without following links."""
    entries = []
    for entry in [app, *sorted(app.rglob("*"))]:
        mode = entry.lstat().st_mode
        if stat.S_ISREG(mode):
            with entry.open("rb") as source:
                digest = hashlib.sha256()
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            value = digest.hexdigest()
        elif stat.S_ISDIR(mode):
            value = "directory"
        else:
            raise InstallError(f"Application changed to a linked or special file: {entry}")
        entries.append((str(entry.relative_to(app)), stat.S_IMODE(mode), value))
    return entries


def validate_app(app, root, runner):
    identity = app_manifest(app, root)
    runner(["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(app)], capture_output=True)
    runner(["/usr/bin/plutil", "-lint", str(app / "Contents/Info.plist")], capture_output=True)
    # The signed marker names the intended identity. Check the actual leaf
    # certificate too; a valid signature from a different signer is insufficient.
    with tempfile.TemporaryDirectory(prefix="quill-install-certificate-") as directory:
        prefix = Path(directory) / "certificate"
        runner(["/usr/bin/codesign", "--display", "--extract-certificates=" + str(prefix), str(app)], capture_output=True)
        certificate = Path(str(prefix) + "0")
        if not certificate.is_file() or hashlib.sha1(certificate.read_bytes()).hexdigest().upper() != identity:
            raise InstallError("Quill's actual signing certificate does not match its saved identity")
    return identity


def existing_destination(destination, root, identity, runner):
    ordinary_path(destination)
    if not destination.exists():
        return None
    actual = validate_app(destination, root, runner)
    if actual != identity:
        raise InstallError("Refusing to replace Quill's stable signing identity; review an identity migration separately")
    return fingerprint(destination)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_backup_manifest(directory, manifest):
    # Replace the receipt only after its full contents reach disk. The directory
    # belongs to this transaction, so an interrupted write cannot affect another.
    temporary = directory / "manifest.tmp"
    with temporary.open("x", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump(manifest, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(directory / "manifest.json")
    sync_directory(directory)


def retain_previous(destination, source, root, identity, before_destination, before_source, runner):
    """Prepare a verified durable copy before replacing the installed app."""
    backup_root = destination.parent / ".quill-app-backups"
    ordinary_path(backup_root)
    if backup_root == source or source in backup_root.parents:
        raise InstallError("The backup directory must be separate from the source app")
    backup_root.mkdir(mode=0o700, exist_ok=True)
    mode = backup_root.lstat()
    if (not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid()
            or stat.S_IMODE(mode.st_mode) & 0o077):
        raise InstallError("Quill's app backup directory must be private and owned by the current user")
    directory = Path(tempfile.mkdtemp(prefix="install-", dir=backup_root))
    backup = directory / "Quill.app"
    if source == backup or source in backup.parents or backup in source.parents:
        raise InstallError("The backup must be separate from the source app")
    manifest = {
        "schema": BACKUP_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "preparing",
        "original_checkout": str(root),
        "signing_identity": identity,
        "source_app": str(source),
        "destination_app": str(destination),
        "previous_app": "Quill.app",
        "previous_app_fingerprint": before_destination,
        "installed_app_fingerprint": before_source,
        "scope": "Signed app bundle only; workflow source, configuration, queues, and recordings are not restored.",
    }
    save_backup_manifest(directory, manifest)
    runner(["/usr/bin/ditto", str(destination), str(backup)])
    if validate_app(backup, root, runner) != identity or fingerprint(backup) != before_destination:
        raise InstallError(f"Previous app backup validation failed. The installed app was not replaced. Backup: {directory}")
    # Flush the saved bundle before allowing replacement, including its parent
    # entries. A successful install never deletes or prunes these backups.
    for entry in backup.rglob("*"):
        if entry.is_file():
            with entry.open("rb") as saved:
                os.fsync(saved.fileno())
    for entry in sorted([backup, *[p for p in backup.rglob("*") if p.is_dir()]], key=lambda p: len(p.parts), reverse=True):
        sync_directory(entry)
    manifest["status"] = "prepared"
    save_backup_manifest(directory, manifest)
    sync_directory(backup_root)
    sync_directory(destination.parent)
    return directory, manifest


def install(root, destination=DEFAULT_DESTINATION, *, source_app=None, runner=run,
            idle_check=require_idle, report=print, consistency_check=lambda: None):
    root = Path(root).resolve(strict=True)
    source = Path(os.path.abspath(source_app)) if source_app is not None else root / ".build/Quill.app"
    destination = Path(os.path.abspath(destination))
    ordinary_path(destination)
    ordinary_path(source)
    if destination == source or source in destination.parents or destination in source.parents:
        raise InstallError("The installation must be separate from the preview app")
    if destination.name != "Quill.app" or not destination.parent.is_dir():
        raise InstallError("Choose Quill.app inside an existing Applications directory")
    # Check the running target before creating locks or copying anything.
    idle_check(destination)
    identity = validate_app(source, root, runner)
    before_source = fingerprint(source)
    before_destination = existing_destination(destination, root, identity, runner)
    lock_fd = os.open(destination.parent / ".quill-install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError("Another Quill installer is running") from error
        idle_check(destination)
        if existing_destination(destination, root, identity, runner) != before_destination:
            raise InstallError("The installed app changed during preparation; retry installation")
        staging = Path(tempfile.mkdtemp(prefix=".quill-install-", dir=destination.parent))
        preserve_recovery = False
        backup_directory = None
        try:
            staged = staging / "Quill.app"
            # ditto preserves macOS bundle metadata and the original signature.
            runner(["/usr/bin/ditto", str(source), str(staged)])
            if validate_app(staged, root, runner) != identity or fingerprint(staged) != before_source:
                raise InstallError("The staged app differs from the signed source")
            if fingerprint(source) != before_source:
                raise InstallError("The preview app changed while it was being copied; retry installation")
            if before_destination is not None:
                if existing_destination(destination, root, identity, runner) != before_destination:
                    raise InstallError("The installed app changed before backup; retry installation")
                backup_directory, backup_manifest = retain_previous(
                    destination, source, root, identity, before_destination, before_source, runner
                )
            idle_check(destination)
            if existing_destination(destination, root, identity, runner) != before_destination:
                raise InstallError("The installed app changed before replacement; retry installation")
            if fingerprint(source) != before_source:
                raise InstallError("The preview app changed before replacement; retry installation")
            previous = staging / "previous.app"
            published = False
            try:
                # A caller restoring compatible software can recheck live
                # dependencies here after the potentially lengthy copy.
                consistency_check()
                if before_destination is not None:
                    destination.rename(previous)
                staged.rename(destination)
                published = True
                if validate_app(destination, root, runner) != identity or fingerprint(destination) != before_source:
                    raise InstallError("Installed app validation did not match the signed source")
                if fingerprint(source) != before_source:
                    raise InstallError("The preview app changed during installed validation")
                if backup_directory is not None:
                    backup_app = backup_directory / "Quill.app"
                    if validate_app(backup_app, root, runner) != identity or fingerprint(backup_app) != before_destination:
                        raise InstallError("The retained previous app changed during installation")
                consistency_check()
                if backup_directory is not None:
                    backup_manifest["status"] = "installed"
                    save_backup_manifest(backup_directory, backup_manifest)
            except BaseException:
                try:
                    if published:
                        # Never remove a newly launched app during rollback.
                        idle_check(destination)
                        if fingerprint(destination) != before_source:
                            raise InstallError("Installed app changed during rollback")
                        destination.rename(staging / "failed.app")
                    if previous.exists():
                        previous.rename(destination)
                except BaseException as error:
                    preserve_recovery = True
                    raise InstallError(f"Could not restore the previous app. Recovery files preserved at {staging}") from error
                if backup_directory is not None:
                    try:
                        backup_manifest["status"] = "rolled_back"
                        save_backup_manifest(backup_directory, backup_manifest)
                    except BaseException as error:
                        preserve_recovery = True
                        raise InstallError(
                            f"The previous app was restored, but its backup receipt could not be updated. "
                            f"Recovery files preserved at {staging}; backup at {backup_directory}"
                        ) from error
                raise
        finally:
            # The old app is retained until the installed signature, metadata,
            # and byte-for-byte copy have all passed validation.
            if not preserve_recovery:
                shutil.rmtree(staging)
        if backup_directory is not None:
            report(f"Previous signed app retained: {backup_directory / 'Quill.app'}")
            report(f"App backup receipt: {backup_directory / 'manifest.json'}")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--source-app", type=Path, help="Install an archived signed app from this checkout; restores the app bundle only")
    args = parser.parse_args()
    try:
        output = install(args.checkout, args.destination, source_app=args.source_app)
    except (InstallError, OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
    print("Installed:", output)
    print("Original signing identity preserved. No app launched, configuration changed, or login item enabled.")
    if args.source_app is not None:
        print("App-only restore: workflow source, configuration, queues, and recordings were not restored.")


if __name__ == "__main__":
    main()
