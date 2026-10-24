#!/usr/bin/env python3
"""Checkpoint and stage Quill software recovery without rewinding meeting data.

Only restore-app installs anything, after exact compatibility and idle checks.
No command launches, stops processes, registers a host, rewinds meeting data, or
writes to the live checkout. Source/bridge restoration is separately reviewed.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


overlay = module("quill_recovery_overlay", "source-overlay.py")
installer = module("quill_recovery_installer", "install-app.py")
SCHEMA = "quill-recovery-checkpoint-v1"
CONFIG_FILES = ("config.json", "workflow.json", "signing.json", "drive-folders.json")
DATABASES = ("jobs.sqlite3", "reviews.sqlite3", "sharing.sqlite3")
HOST = Path("Library/Application Support/Quill/BrowserBridge/native-host.py")
REGISTRATIONS = {
    "chrome": Path("Library/Application Support/Google/Chrome/NativeMessagingHosts/com.digimata.quill.meetings.json"),
    "edge": Path("Library/Application Support/Microsoft Edge/NativeMessagingHosts/com.digimata.quill.meetings.json"),
}


class RecoveryError(Exception):
    pass


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def ordinary(path):
    path = Path(os.path.abspath(path))
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise RecoveryError("Refusing a symlinked recovery path: " + str(part))
    return path


def file_record(path):
    path = ordinary(path)
    if not path.exists():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RecoveryError("Expected an ordinary, unlinked file: " + str(path))
    return {"sha256": digest(path.read_bytes()), "size": info.st_size,
            "mode": stat.S_IMODE(info.st_mode)}


def inventory(directory):
    directory = ordinary(directory)
    result = {}
    for entry in sorted(directory.rglob("*")):
        mode = entry.lstat().st_mode
        if stat.S_ISDIR(mode):
            ordinary(entry)
        elif stat.S_ISREG(mode):
            result[str(entry.relative_to(directory))] = file_record(entry)
        else:
            raise RecoveryError("Recovery payload contains a linked or special file")
    return result


def runtime_inventory(home):
    """Hashes only: never copy credentials, preferences, queue rows or receipts."""
    home = ordinary(home)
    config = home / ".config/quill"
    files = {name: file_record(config / name) for name in CONFIG_FILES}
    schemas = {}
    for name in DATABASES:
        path = ordinary(home / ".local/state/quill/workflow" / name)
        if not path.exists():
            schemas[name] = None
            continue
        if not path.is_file():
            raise RecoveryError("Expected an ordinary workflow database")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            rows = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                                      "WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
            schemas[name] = {"user_version": connection.execute("PRAGMA user_version").fetchone()[0],
                             "ddl_sha256": digest(encode(rows))}
        finally:
            connection.close()
    return {"configuration": files, "database_schemas": schemas}


def bridge_inventory(home):
    home = ordinary(home)
    host_path = home / HOST
    host = file_record(host_path)
    result = {"native-host.py": host}
    for browser, relative in REGISTRATIONS.items():
        path = home / relative
        record = file_record(path)
        result[browser + ".json"] = record
        if record:
            data = json.loads(path.read_bytes())
            origins = data.get("allowed_origins", [])
            if (data.get("name") != "com.digimata.quill.meetings"
                    or data.get("type") != "stdio" or data.get("path") != str(host_path)
                    or not isinstance(origins, list) or len(origins) != 1
                    or not isinstance(origins[0], str)
                    or not re.fullmatch(r"chrome-extension://[a-p]{32}/", origins[0])):
                raise RecoveryError("Unexpected browser host registration; review it before checkpointing")
            if host is None or not host["mode"] & 0o111:
                raise RecoveryError("Registered browser host is missing or not executable")
    return result


def fresh_output(destination, forbidden):
    destination = ordinary(destination)
    if destination.exists():
        raise RecoveryError("Recovery output already exists; choose an unused directory")
    if not destination.parent.is_dir():
        raise RecoveryError("Recovery output parent must already exist")
    for protected in forbidden:
        protected = ordinary(protected)
        if destination == protected or protected in destination.parents or destination in protected.parents:
            raise RecoveryError("Recovery output must be separate from live files and the checkpoint")
    return destination


def live_locations(root, app, home):
    return [root, app, home / ".config/quill", home / ".local/state/quill",
            home / HOST.parent, *[home / p.parent for p in REGISTRATIONS.values()]]


def source_inventory(root):
    commit = overlay.commit_id(root, "HEAD")
    base, _ = overlay.tree(root, commit)
    files, _, _, _ = overlay.inventory(root, base)
    return {"base_commit": commit, "files": files}


def write_manifest(directory, manifest):
    data = encode(manifest)
    for name, payload in (("manifest.json", data), ("manifest.sha256", (digest(data) + "\n").encode())):
        with (directory / name).open("xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())


def create(root, app, destination, home, *, runner=installer.run):
    root, app, home = ordinary(root), ordinary(app), ordinary(home)
    destination = fresh_output(destination, live_locations(root, app, home))
    identity = installer.validate_app(app, root, runner)
    app_before = installer.fingerprint(app)
    runtime_before = runtime_inventory(home)
    bridge_before = bridge_inventory(home)
    with tempfile.TemporaryDirectory(prefix=".quill-checkpoint-", dir=destination.parent) as temporary:
        temporary = Path(temporary)
        exported = overlay.export(root, temporary / "source")
        runner(["/usr/bin/ditto", str(app), str(temporary / "Quill.app")])
        if (installer.validate_app(temporary / "Quill.app", root, runner) != identity
                or installer.fingerprint(temporary / "Quill.app") != app_before):
            raise RecoveryError("Checkpoint app does not match the installed signed app")
        browser = temporary / "browser"
        browser.mkdir(mode=0o700)
        for name, record in bridge_before.items():
            if record is not None:
                source = home / (HOST if name == "native-host.py" else REGISTRATIONS[name[:-5]])
                shutil.copy2(source, browser / name)
                if file_record(browser / name) != record:
                    raise RecoveryError("Browser bridge changed during checkpointing")
        live_source = source_inventory(root)
        if (live_source != {"base_commit": exported["base_commit"], "files": exported["working_files"]}
                or installer.fingerprint(app) != app_before
                or runtime_inventory(home) != runtime_before
                or bridge_inventory(home) != bridge_before):
            raise RecoveryError("Quill software or configuration changed during checkpointing; retry")
        manifest = {"schema": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
                    "checkout": str(root), "installed_app": str(app), "home": str(home),
                    "signing_identity": identity, "runtime": runtime_before, "browser": bridge_before,
                    "payload": inventory(temporary),
                    "scope": "Contemporaneous installed app, source and browser bridge; not a build-provenance attestation",
                    "preserve_live": ["recordings", "configuration", "credentials", "Keychain", "queue", "approvals", "upload receipts"]}
        write_manifest(temporary, manifest)
        verify(temporary, runner=runner)
        overlay.publish(temporary, destination)
    return manifest


def verify(bundle, *, runner=installer.run):
    bundle = ordinary(bundle)
    if not bundle.is_dir():
        raise RecoveryError("Checkpoint directory is missing")
    # Check paths before reading anything through a possible substituted link.
    actual = inventory(bundle)
    data = (bundle / "manifest.json").read_bytes()
    if digest(data) != (bundle / "manifest.sha256").read_text().strip():
        raise RecoveryError("Checkpoint manifest checksum mismatch")
    manifest = json.loads(data)
    if manifest.get("schema") != SCHEMA:
        raise RecoveryError("Unsupported recovery checkpoint format")
    payload = {k: v for k, v in actual.items() if k not in ("manifest.json", "manifest.sha256")}
    if payload != manifest["payload"]:
        raise RecoveryError("Checkpoint payload checksum or file mode mismatch")
    root = Path(manifest["checkout"])
    if installer.validate_app(bundle / "Quill.app", root, runner) != manifest["signing_identity"]:
        raise RecoveryError("Checkpoint signing identity mismatch")
    source, _ = overlay.load_bundle(bundle / "source")
    with tempfile.TemporaryDirectory(prefix="quill-source-restore-check-") as temporary:
        overlay.replay(bundle / "source", Path(temporary) / "source")
    expected_browser = {k: v for k, v in manifest["browser"].items() if v is not None}
    if inventory(bundle / "browser") != expected_browser:
        raise RecoveryError("Checkpoint browser snapshot mismatch")
    return manifest


def compatibility(bundle, root, home, *, runner=installer.run):
    manifest = verify(bundle, runner=runner)
    root, home = ordinary(root), ordinary(home)
    source, _ = overlay.load_bundle(Path(bundle) / "source")
    current_source = source_inventory(root)
    current_runtime = runtime_inventory(home)
    current_bridge = bridge_inventory(home)
    changes = []
    if str(root) != manifest["checkout"] or str(home) != manifest["home"]:
        changes.append("installation_paths")
    if current_source != {"base_commit": source["base_commit"], "files": source["working_files"]}:
        changes.append("source")
    if current_bridge != manifest["browser"]:
        changes.append("browser_bridge")
    for part in ("configuration", "database_schemas"):
        expected = manifest["runtime"][part]
        if part == "database_schemas":
            # Older checkpoints predate explicit sharing approvals. Their
            # omission means absent, never permission to ignore a live DB.
            expected = dict(expected)
            for name in DATABASES:
                expected.setdefault(name, None)
        if current_runtime[part] != expected:
            changes.append(part)
    return {"review_required": bool(changes), "changed_components": changes,
            "installs_or_restores": False, "meeting_data_preserved": True}


def stage(bundle, destination, *, runner=installer.run):
    bundle = ordinary(bundle)
    manifest = verify(bundle, runner=runner)
    root, app, home = (Path(manifest[k]) for k in ("checkout", "installed_app", "home"))
    destination = fresh_output(destination, [bundle, *live_locations(root, app, home)])
    with tempfile.TemporaryDirectory(prefix=".quill-restore-stage-", dir=destination.parent) as temporary:
        temporary = Path(temporary)
        source = overlay.replay(bundle / "source", temporary / "source")
        runner(["/usr/bin/ditto", str(bundle / "Quill.app"), str(temporary / "Quill.app")])
        shutil.copytree(bundle / "browser", temporary / "browser")
        if (installer.validate_app(temporary / "Quill.app", root, runner) != manifest["signing_identity"]
                or installer.fingerprint(temporary / "Quill.app") != installer.fingerprint(bundle / "Quill.app")
                or inventory(temporary / "browser") != inventory(bundle / "browser")):
            raise RecoveryError("Staged recovery differs from the verified checkpoint")
        # Catch bundle mutation during staging before publishing an output.
        if verify(bundle, runner=runner) != manifest:
            raise RecoveryError("Checkpoint changed during staging")
        receipt = {"schema": SCHEMA, "checkpoint": str(bundle), "source_files": len(source["files"]),
                   "signed_app_verified": True, "browser_snapshot_verified": True,
                   "live_files_changed": False, "original_checkout": str(root),
                   "instructions": "Do not launch this staged app: its hooks still target the original checkout. Review compatibility, quit Quill and its workers, restore reviewed source/bridge together, then install the archived app. Preserve current configuration and all meeting state."}
        (temporary / "RESTORE.json").write_bytes(encode(receipt))
        overlay.publish(temporary, destination)
    return receipt


def restore_app(bundle, root, home, *, runner=installer.run, idle_check=installer.require_idle):
    """Restore only when the matching source, bridge and runtime still exist.

    Hold Quill's existing instance/processing locks throughout the installation;
    never stop an active recording or worker to make a rollback possible.
    """
    root, home = ordinary(root), ordinary(home)
    locks = [home / ".local/state/quill/run.lock",
             home / ".local/state/quill/workflow/worker.lock",
             home / ".local/state/quill/workflow/supervisor.lock"]
    with ExitStack() as stack:
        for path in locks:
            ordinary(path)
            if file_record(path) is None:
                raise RecoveryError("Required Quill activity lock is missing; review this installation first")
            handle = stack.enter_context(path.open("rb"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RecoveryError("Quill or meeting processing is running; finish and quit before restoring") from error
        state = ordinary(home / ".local/state/quill/workflow")
        # Confirmed deletion can outlive the app and the main worker. Match its
        # daemon-then-control order and retain both locks through replacement:
        # neither a Trash move nor a new confirmation may race a rollback.
        # Older installations have no such files, so create these guards safely.
        for name in ("deletion-worker.lock", "local-deletion-control.lock"):
            path = ordinary(state / name)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            handle = stack.enter_context(os.fdopen(descriptor, "a"))
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise RecoveryError("Expected a private, ordinary local deletion activity lock")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RecoveryError("Local meeting deletion is running; finish before restoring") from error
        # Sharing runs independently after Quill may have quit. Its workers
        # and approval actions hold this common lock shared; rollback holds it
        # exclusively so a newly starting worker cannot race replacement.
        sharing_lock = ordinary(state / "sharing-work.lock")
        descriptor = os.open(sharing_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        handle = stack.enter_context(os.fdopen(descriptor, "a"))
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RecoveryError("Expected an ordinary sharing activity lock")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RecoveryError("Notes sharing is running; finish before restoring") from error
        # Also honor an already running worker from the first implementation,
        # which held only its per-meeting lock. The global lock prevents new
        # current workers while these existing legacy locks are inspected.
        for path in sorted(state.glob("sharing-*.lock")):
            if path == sharing_lock:
                continue
            if file_record(path) is None:
                raise RecoveryError("A sharing activity lock disappeared during restore")
            handle = stack.enter_context(path.open("rb"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RecoveryError("Notes sharing is running; finish before restoring") from error
        manifest = verify(bundle, runner=runner)
        def require_matching_software():
            report = compatibility(bundle, root, home, runner=runner)
            if report["review_required"]:
                raise RecoveryError("Rollback needs review of changed components: " + ", ".join(report["changed_components"]) +
                                    ". Stage the checkpoint for comparison before restoring.")
        require_matching_software()
        destination = Path(manifest["installed_app"])
        installer.install(root, destination, source_app=Path(bundle) / "Quill.app",
                          runner=runner, idle_check=idle_check,
                          consistency_check=require_matching_software)
        return {"restored_app": str(destination), "matching_source_and_bridge_verified": True,
                "runtime_preserved": True, "app_launched": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("create")
    make.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    make.add_argument("--app", type=Path, default=Path("/Applications/Quill.app"))
    make.add_argument("--output", type=Path, required=True)
    check = sub.add_parser("verify")
    check.add_argument("--checkpoint", type=Path, required=True)
    restore = sub.add_parser("stage")
    restore.add_argument("--checkpoint", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    compare = sub.add_parser("check-current")
    compare.add_argument("--checkpoint", type=Path, required=True)
    compare.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    apply = sub.add_parser("restore-app", help="Restore the signed app only when its matching source and runtime are unchanged")
    apply.add_argument("--checkpoint", type=Path, required=True)
    apply.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "create":
            result = create(args.repo, args.app, args.output, Path.home())
            print(json.dumps({"checkpoint": str(args.output), "verified": True,
                              "created_at": result["created_at"], "live_files_changed": False}))
        elif args.command == "verify":
            result = verify(args.checkpoint)
            print(json.dumps({"verified": True, "created_at": result["created_at"],
                              "checkout": result["checkout"], "signing_identity": result["signing_identity"]}))
        elif args.command == "stage":
            print(json.dumps(stage(args.checkpoint, args.output), indent=2))
        elif args.command == "restore-app":
            print(json.dumps(restore_app(args.checkpoint, args.repo, Path.home()), indent=2))
        else:
            result = compatibility(args.checkpoint, args.repo, Path.home())
            print(json.dumps(result, indent=2))
            if result["review_required"]:
                raise SystemExit(2)
    except (RecoveryError, installer.InstallError, overlay.OverlayError, OSError, subprocess.CalledProcessError,
            ValueError, KeyError, TypeError, sqlite3.Error) as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()
