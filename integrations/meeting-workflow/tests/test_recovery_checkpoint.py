"""Recovery checkpoints use disposable source, apps, and runtime inventories."""

import hashlib
import fcntl
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/recovery-checkpoint.py"
SPEC = importlib.util.spec_from_file_location("quill_recovery_checkpoint", SCRIPT)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)
CERTIFICATE = b"synthetic-recovery-certificate"
IDENTITY = hashlib.sha1(CERTIFICATE).hexdigest().upper()
EXTENSION = "a" * 32


class RecoveryCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.root = self.home / "Documents/quill"
        self.root.mkdir(parents=True)
        self.source_text = b"# Existing local Quill source\n"
        (self.root / "README.md").write_bytes(self.source_text)
        self.host_source = self.root / "integrations/meeting-workflow/native-host.py"
        self.host_source.parent.mkdir(parents=True)
        self.host_source.write_bytes(b"#!/usr/bin/python3\n# Repository bridge\n")
        self.host_source.chmod(0o755)
        self.git("init", "--quiet")
        self.git("-c", "user.name=Recovery fixture", "-c", "user.email=fixture@example.invalid",
                 "add", "README.md", "integrations/meeting-workflow/native-host.py")
        self.git("-c", "user.name=Recovery fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--quiet", "-m", "Disposable recovery baseline")
        (self.root / "README.md").write_bytes(self.source_text + b"Local additions\n")
        added = self.root / "scripts/local-fixture.sh"
        added.parent.mkdir()
        added.write_bytes(b"#!/bin/sh\nexit 0\n")
        added.chmod(0o755)
        self.app = self.base / "Applications/Quill.app"
        self.make_app(self.app)
        self.config_dir = self.home / ".config/quill"
        self.config_dir.mkdir(parents=True)
        self.config = self.config_dir / "config.json"
        self.config.write_text(json.dumps({"transcription": {"enabled": True},
                                           "private_fixture": "never-copy-config-secret"}))
        self.workflow_config = self.config_dir / "workflow.json"
        self.workflow_config.write_text('{"secret":"never-copy-workflow-secret"}\n')
        self.state = self.home / ".local/state/quill/workflow"
        self.state.mkdir(parents=True)
        self.jobs = self.state / "jobs.sqlite3"
        with sqlite3.connect(self.jobs) as db:
            db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, private_text TEXT)")
            db.execute("INSERT INTO jobs VALUES (?,?)", ("fixture", "never-copy-meeting-content"))
        self.reviews = self.state / "reviews.sqlite3"
        with sqlite3.connect(self.reviews) as db:
            db.execute("CREATE TABLE routing_reviews (job TEXT PRIMARY KEY, record TEXT)")
        self.host = self.home / "Library/Application Support/Quill/BrowserBridge/native-host.py"
        self.host.parent.mkdir(parents=True, mode=0o700)
        self.host.write_bytes(self.host_source.read_bytes())
        self.host.chmod(0o700)
        self.registration = (self.home / "Library/Application Support/Google/Chrome/"
                             "NativeMessagingHosts/com.digimata.quill.meetings.json")
        self.registration.parent.mkdir(parents=True)
        self.registration.write_text(json.dumps({
            "name": "com.digimata.quill.meetings", "description": "Local Quill meeting state bridge",
            "path": str(self.host), "type": "stdio",
            "allowed_origins": ["chrome-extension://" + EXTENSION + "/"],
        }) + "\n")
        self.registration.chmod(0o600)
        self.bundle = self.base / "checkpoint"
        self.commands = []

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True).stdout

    def make_app(self, app):
        executable = app / "Contents/MacOS/quill"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"Synthetic signed Quill executable\n")
        executable.chmod(0o755)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.digimata.quill", "CFBundleExecutable": "quill",
            "CFBundlePackageType": "APPL",
        }))
        resources = app / "Contents/Resources"
        resources.mkdir()
        (resources / "quill-local-preview.json").write_text(json.dumps({
            "schema": "quill-local-preview-v1", "checkout": str(self.root),
            "source_binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "signing_identity": IDENTITY,
        }))
        signature = app / "Contents/_CodeSignature"
        signature.mkdir()
        (signature / "CodeResources").write_bytes(b"Synthetic signature resources\n")

    def runner(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == "/usr/bin/ditto":
            shutil.copytree(command[1], command[2])
        elif any(arg.startswith("--extract-certificates=") for arg in command):
            prefix = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--extract-certificates="))
            Path(prefix + "0").write_bytes(CERTIFICATE)
        elif command[0] not in ("/usr/bin/codesign", "/usr/bin/plutil"):
            self.fail("Unexpected external operation: " + repr(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def create(self):
        return recovery.create(self.root, self.app, self.bundle, self.home, runner=self.runner)

    def verify(self):
        return recovery.verify(self.bundle, runner=self.runner)

    def compatibility(self):
        return recovery.compatibility(self.bundle, self.root, self.home, runner=self.runner)

    def inventory(self, directory):
        return {str(p.relative_to(directory)): (p.read_bytes(), p.stat().st_mode & 0o777)
                for p in directory.rglob("*") if p.is_file()}

    def test_release_metadata_is_preserved_in_verified_checkpoint(self):
        marker_path = self.app / "Contents/Resources/quill-local-preview.json"
        marker = json.loads(marker_path.read_bytes())
        marker.update(build_configuration="release", version="0.1.0", build_number="2")
        marker_path.write_text(json.dumps(marker))
        info_path = self.app / "Contents/Info.plist"
        info = plistlib.loads(info_path.read_bytes())
        info.update(CFBundleShortVersionString="0.1.0", CFBundleVersion="2")
        info_path.write_bytes(plistlib.dumps(info))
        manifest = self.create()
        self.assertEqual(self.verify(), manifest)
        copied = self.bundle / "Quill.app/Contents/Resources/quill-local-preview.json"
        self.assertEqual(copied.read_bytes(), marker_path.read_bytes())

    def test_roundtrip_reconstructs_source_app_and_bridge_without_changing_live_files(self):
        before_root = self.inventory(self.root)
        before_home_runtime = {p: p.read_bytes() for p in (self.config, self.workflow_config,
                                                         self.jobs, self.reviews, self.host, self.registration)}
        before_app = self.inventory(self.app)
        head = self.git("rev-parse", "HEAD")
        status = self.git("status", "--porcelain=v1", "--untracked-files=all")
        manifest = self.create()
        self.assertEqual(self.verify(), manifest)
        staged = self.base / "staged-recovery"
        receipt = recovery.stage(self.bundle, staged, runner=self.runner)
        self.assertEqual((staged / "source/README.md").read_bytes(), (self.root / "README.md").read_bytes())
        self.assertEqual((staged / "source/scripts/local-fixture.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.inventory(staged / "Quill.app"), before_app)
        self.assertEqual((staged / "browser/native-host.py").read_bytes(), self.host.read_bytes())
        self.assertEqual((staged / "browser/chrome.json").read_bytes(), self.registration.read_bytes())
        self.assertFalse(receipt["live_files_changed"])
        self.assertEqual(receipt["original_checkout"], str(self.root))
        self.assertIn("Do not launch", receipt["instructions"])
        self.assertIn("original checkout", receipt["instructions"])
        self.assertEqual(self.inventory(self.root), before_root)
        self.assertEqual(self.inventory(self.app), before_app)
        self.assertEqual({p: p.read_bytes() for p in before_home_runtime}, before_home_runtime)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain=v1", "--untracked-files=all"), status)
        self.assertFalse(self.compatibility()["review_required"])
        self.assertTrue(all(command[0] in ["/usr/bin/ditto", "/usr/bin/codesign", "/usr/bin/plutil"]
                            for command in self.commands))

    def test_runtime_content_and_credentials_are_not_copied(self):
        (self.config_dir / "signing.json").write_text('{"private":"never-copy-signing-secret"}\n')
        (self.config_dir / "auth.json").write_text('{"token":"never-copy-auth-token"}\n')
        recordings = self.home / "Recordings"
        recordings.mkdir()
        (recordings / "existing.caf").write_bytes(b"never-copy-original-recording")
        self.create()
        forbidden = [b"never-copy-config-secret", b"never-copy-workflow-secret",
                     b"never-copy-meeting-content", b"never-copy-signing-secret",
                     b"never-copy-auth-token", b"never-copy-original-recording"]
        for path in self.bundle.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                for text in forbidden:
                    self.assertNotIn(text, data, str(path))
                self.assertNotIn(path.suffix, (".sqlite3", ".caf"))

    def test_installed_bridge_is_preserved_even_when_checkout_bridge_differs(self):
        installed = b"#!/usr/bin/python3\n# Older installed bridge, intentionally different.\n"
        self.host.write_bytes(installed)
        self.create()
        staged = self.base / "staged-recovery"
        recovery.stage(self.bundle, staged, runner=self.runner)
        self.assertEqual((staged / "browser/native-host.py").read_bytes(), installed)
        self.assertEqual((staged / "source/integrations/meeting-workflow/native-host.py").read_bytes(),
                         self.host_source.read_bytes())

    def test_corrupted_app_fails_before_staging(self):
        self.create()
        (self.bundle / "Quill.app/Contents/MacOS/quill").write_bytes(b"Corrupted executable\n")
        target = self.base / "failed-stage"
        with self.assertRaises(recovery.RecoveryError):
            recovery.stage(self.bundle, target, runner=self.runner)
        self.assertFalse(target.exists())

    def test_corrupted_source_object_fails_before_staging(self):
        self.create()
        source_object = next((self.bundle / "source/objects").iterdir())
        source_object.write_bytes(b"Corrupted source object\n")
        target = self.base / "failed-stage"
        with self.assertRaises(recovery.RecoveryError):
            recovery.stage(self.bundle, target, runner=self.runner)
        self.assertFalse(target.exists())

    def test_corrupted_bridge_and_registration_are_detected(self):
        self.create()
        for name in ("native-host.py", "chrome.json"):
            with self.subTest(name=name):
                path = self.bundle / "browser" / name
                original = path.read_bytes()
                path.write_bytes(b"corrupt\n")
                with self.assertRaises(recovery.RecoveryError):
                    self.verify()
                path.write_bytes(original)

    def test_config_drift_requires_review_without_restoring_old_settings(self):
        self.create()
        self.config.write_text('{"transcription":{"enabled":false}}\n')
        current = self.config.read_bytes()
        result = self.compatibility()
        self.assertTrue(result["review_required"])
        self.assertIn("config", json.dumps(result).lower())
        self.assertEqual(self.config.read_bytes(), current)

    def test_schema_drift_requires_review_with_same_user_version(self):
        self.create()
        with sqlite3.connect(self.jobs) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            db.execute("ALTER TABLE jobs ADD COLUMN future_value TEXT")
        current = self.jobs.read_bytes()
        result = self.compatibility()
        self.assertTrue(result["review_required"])
        self.assertIn("schema", json.dumps(result).lower())
        self.assertEqual(self.jobs.read_bytes(), current)

    def test_new_queue_rows_do_not_count_as_schema_drift_or_get_restored(self):
        self.create()
        with sqlite3.connect(self.jobs) as db:
            db.execute("INSERT INTO jobs VALUES ('newer-meeting', 'newer-private-text')")
        current = self.jobs.read_bytes()
        self.assertFalse(self.compatibility()["review_required"])
        self.assertEqual(self.jobs.read_bytes(), current)

    def sharing_database(self):
        path = self.state / "sharing.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE sharing_reviews (job TEXT PRIMARY KEY, record TEXT NOT NULL)")
            db.execute("INSERT INTO sharing_reviews VALUES (?,?)", ("fixture", "never-copy-sharing-approval"))
        return path

    def test_sharing_schema_is_checked_but_approvals_are_never_copied_or_rewound(self):
        sharing = self.sharing_database()
        self.activity_locks()
        manifest = self.create()
        self.assertIsNotNone(manifest["runtime"]["database_schemas"]["sharing.sqlite3"])
        for path in self.bundle.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"never-copy-sharing-approval", path.read_bytes())
                self.assertNotEqual(path.name, "sharing.sqlite3")
        with sqlite3.connect(sharing) as db:
            db.execute("UPDATE sharing_reviews SET record='newer-approved-readers' WHERE job='fixture'")
        before = sharing.read_bytes()
        self.assertFalse(self.compatibility()["review_required"])
        recovery.restore_app(self.bundle, self.root, self.home, runner=self.runner, idle_check=lambda app: None)
        self.assertEqual(sharing.read_bytes(), before)

    def test_sharing_schema_drift_requires_review(self):
        sharing = self.sharing_database()
        self.create()
        with sqlite3.connect(sharing) as db:
            db.execute("ALTER TABLE sharing_reviews ADD COLUMN approval_revision TEXT")
        before = sharing.read_bytes()
        report = self.compatibility()
        self.assertTrue(report["review_required"])
        self.assertIn("database_schemas", report["changed_components"])
        self.assertEqual(sharing.read_bytes(), before)

    def test_legacy_checkpoint_verifies_but_new_sharing_database_blocks_compatibility(self):
        with patch.object(recovery, "DATABASES", ("jobs.sqlite3", "reviews.sqlite3")):
            manifest = self.create()
        self.assertNotIn("sharing.sqlite3", manifest["runtime"]["database_schemas"])
        self.assertEqual(self.verify(), manifest)
        self.assertFalse(self.compatibility()["review_required"])
        sharing = self.sharing_database()
        before = sharing.read_bytes()
        report = self.compatibility()
        self.assertTrue(report["review_required"])
        self.assertIn("database_schemas", report["changed_components"])
        self.assertEqual(self.verify(), manifest)
        self.assertEqual(sharing.read_bytes(), before)

    def test_source_drift_requires_review_without_rewriting_checkout(self):
        self.create()
        path = self.root / "README.md"
        path.write_bytes(b"Newer uncommitted work\n")
        current = path.read_bytes()
        self.assertTrue(self.compatibility()["review_required"])
        self.assertEqual(path.read_bytes(), current)

    def test_stage_refuses_existing_output_and_protected_locations(self):
        self.create()
        existing = self.base / "existing"
        existing.mkdir()
        sentinel = existing / "sentinel"
        sentinel.write_bytes(b"Preserve existing folder\n")
        linked = self.base / "linked-output"
        linked.symlink_to(existing, target_is_directory=True)
        for target in (existing, linked, self.bundle / "inside", self.root / "restored-source",
                       self.config_dir / "restored", self.state / "restored"):
            with self.subTest(target=target):
                with self.assertRaises(recovery.RecoveryError):
                    recovery.stage(self.bundle, target, runner=self.runner)
        self.assertEqual(sentinel.read_bytes(), b"Preserve existing folder\n")
        self.assertFalse((self.root / "restored-source").exists())
        self.assertFalse((self.config_dir / "restored").exists())
        self.assertFalse((self.state / "restored").exists())

    def test_create_refuses_existing_destination(self):
        self.bundle.mkdir()
        sentinel = self.bundle / "sentinel"
        sentinel.write_bytes(b"Existing checkpoint\n")
        with self.assertRaises(recovery.RecoveryError):
            self.create()
        self.assertEqual(sentinel.read_bytes(), b"Existing checkpoint\n")

    def test_linked_bundle_payload_is_never_followed(self):
        self.create()
        host = self.bundle / "browser/native-host.py"
        host.unlink()
        host.symlink_to(self.host)
        current = self.host.read_bytes()
        with self.assertRaises(recovery.RecoveryError):
            self.verify()
        self.assertEqual(self.host.read_bytes(), current)

    def test_create_refuses_widened_origin_or_wrong_registered_host(self):
        original = json.loads(self.registration.read_bytes())
        for changes in ({"path": str(self.host_source)},
                        {"allowed_origins": original["allowed_origins"] + ["chrome-extension://" + "b" * 32 + "/"]}):
            with self.subTest(changes=changes):
                self.registration.write_text(json.dumps({**original, **changes}))
                with self.assertRaisesRegex(recovery.RecoveryError, "registration"):
                    self.create()
                self.assertFalse(self.bundle.exists())

    def test_live_changes_during_capture_are_not_published(self):
        for changed in (self.root / "README.md", self.config, self.app / "Contents/MacOS/quill"):
            with self.subTest(changed=changed):
                before = changed.read_bytes()
                def mutate_during_copy(command, **kwargs):
                    result = self.runner(command, **kwargs)
                    if command[0] == "/usr/bin/ditto" and command[1] == str(self.app):
                        changed.write_bytes(before + b"\n")
                    return result
                with self.assertRaisesRegex(recovery.RecoveryError, "changed during"):
                    recovery.create(self.root, self.app, self.bundle, self.home, runner=mutate_during_copy)
                self.assertFalse(self.bundle.exists())
                self.assertEqual(changed.read_bytes(), before + b"\n")
                changed.write_bytes(before)

    def test_bundle_changes_during_stage_are_not_published(self):
        self.create()
        staged = self.base / "staged-recovery"
        def mutate_during_copy(command, **kwargs):
            result = self.runner(command, **kwargs)
            if command[0] == "/usr/bin/ditto":
                (self.bundle / "browser/native-host.py").write_bytes(b"changed mid-stage\n")
            return result
        with self.assertRaisesRegex(recovery.RecoveryError, "checksum|differs"):
            recovery.stage(self.bundle, staged, runner=mutate_during_copy)
        self.assertFalse(staged.exists())

    def activity_locks(self):
        paths = [self.home / ".local/state/quill/run.lock",
                 self.state / "worker.lock", self.state / "supervisor.lock"]
        for path in paths:
            path.touch(mode=0o600)
        return paths

    def test_restore_matching_software_preserves_current_state_and_does_not_launch(self):
        self.activity_locks()
        original_app = self.inventory(self.app)
        self.create()
        # The fake signer treats this as a newer valid signed app. Everything
        # outside the app still matches the checkpoint, so app-only recovery is safe.
        executable = self.app / "Contents/MacOS/quill"
        executable.write_bytes(b"Newer signed app to roll back\n")
        before_runtime = {p: p.read_bytes() for p in (self.config, self.workflow_config,
                                                   self.jobs, self.reviews, self.host, self.registration)}
        before_source = self.inventory(self.root)
        idle_checked = []
        result = recovery.restore_app(self.bundle, self.root, self.home, runner=self.runner,
                                      idle_check=lambda app: idle_checked.append(app))
        self.assertEqual(self.inventory(self.app), original_app)
        self.assertEqual(self.inventory(self.root), before_source)
        self.assertEqual({p: p.read_bytes() for p in before_runtime}, before_runtime)
        self.assertTrue(idle_checked)
        self.assertEqual(set(idle_checked), {self.app})
        self.assertFalse(result["app_launched"])
        self.assertTrue(result["runtime_preserved"])
        self.assertTrue(result["matching_source_and_bridge_verified"])
        backups = list((self.app.parent / ".quill-app-backups").glob("*/Quill.app/Contents/MacOS/quill"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"Newer signed app to roll back\n")

    def test_restore_refuses_live_app_worker_or_supervisor_without_installing(self):
        locks = self.activity_locks()
        self.create()
        original = self.inventory(self.app)
        for lock_path in locks:
            with self.subTest(lock=lock_path):
                self.commands.clear()
                with lock_path.open("rb") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaisesRegex(recovery.RecoveryError, "running"):
                        recovery.restore_app(self.bundle, self.root, self.home, runner=self.runner,
                                             idle_check=lambda app: self.fail("Installer must not run"))
                self.assertEqual(self.inventory(self.app), original)
                self.assertFalse(any(command[0] == "/usr/bin/ditto" for command in self.commands))
        self.assertFalse((self.app.parent / ".quill-app-backups").exists())

    def test_restore_missing_activity_guard_refuses_before_install(self):
        self.create()
        original = self.inventory(self.app)
        with self.assertRaisesRegex(recovery.RecoveryError, "lock is missing"):
            recovery.restore_app(self.bundle, self.root, self.home, runner=self.runner,
                                 idle_check=lambda app: self.fail("Installer must not run"))
        self.assertEqual(self.inventory(self.app), original)

    def test_restore_refuses_active_sharing_worker_or_legacy_job_lock(self):
        self.activity_locks()
        self.create()
        original = self.inventory(self.app)
        for name, operation in (("sharing-work.lock", fcntl.LOCK_SH),
                                ("sharing-" + "a" * 64 + ".lock", fcntl.LOCK_EX)):
            with self.subTest(name=name):
                path = self.state / name
                path.touch(mode=0o600)
                self.commands.clear()
                with path.open("rb") as handle:
                    fcntl.flock(handle, operation | fcntl.LOCK_NB)
                    with self.assertRaisesRegex(recovery.RecoveryError, "sharing is running"):
                        recovery.restore_app(self.bundle, self.root, self.home, runner=self.runner,
                                             idle_check=lambda app: self.fail("Installer must not run"))
                self.assertEqual(self.inventory(self.app), original)
                self.assertFalse(any(command[0] == "/usr/bin/ditto" for command in self.commands))

    def test_restore_excludes_new_sharing_actions_until_app_replacement_finishes(self):
        self.activity_locks()
        self.create()
        lock = self.state / "sharing-work.lock"
        self.assertFalse(lock.exists())
        observations = []
        def inspect_global_guard(command, **kwargs):
            if command[0] == "/usr/bin/ditto" and command[1] == str(self.bundle / "Quill.app"):
                with lock.open("rb") as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                observations.append(True)
            return self.runner(command, **kwargs)
        recovery.restore_app(self.bundle, self.root, self.home, runner=inspect_global_guard,
                             idle_check=lambda app: None)
        self.assertTrue(observations)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        with lock.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)

    def test_restore_refuses_active_deletion_worker_or_confirmation_without_installing(self):
        self.activity_locks()
        self.create()
        original = self.inventory(self.app)
        for name in ("deletion-worker.lock", "local-deletion-control.lock"):
            with self.subTest(name=name):
                lock = self.state / name
                lock.touch(mode=0o600)
                self.commands.clear()
                with lock.open("rb") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaisesRegex(recovery.RecoveryError, "deletion is running"):
                        recovery.restore_app(self.bundle, self.root, self.home, runner=self.runner,
                                             idle_check=lambda app: self.fail("Installer must not run"))
                self.assertEqual(self.inventory(self.app), original)
                self.assertFalse(any(command[0] == "/usr/bin/ditto" for command in self.commands))
        self.assertFalse((self.app.parent / ".quill-app-backups").exists())

    def test_restore_excludes_new_deletion_work_and_confirmation_through_replacement(self):
        self.activity_locks()
        self.create()
        locks = [self.state / name for name in ("deletion-worker.lock", "local-deletion-control.lock")]
        self.assertTrue(all(not lock.exists() for lock in locks))
        observations = []
        def inspect_deletion_guards(command, **kwargs):
            if command[0] == "/usr/bin/ditto" and command[1] == str(self.bundle / "Quill.app"):
                for lock in locks:
                    with lock.open("rb") as handle:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observations.append(True)
            return self.runner(command, **kwargs)
        recovery.restore_app(self.bundle, self.root, self.home, runner=inspect_deletion_guards,
                             idle_check=lambda app: None)
        self.assertTrue(observations)
        for lock in locks:
            self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
            with lock.open("rb") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_restore_rejects_linked_or_nonprivate_deletion_guards(self):
        self.activity_locks()
        self.create()
        original = self.inventory(self.app)
        other = self.base / "not-a-deletion-lock"
        other.touch(mode=0o600)
        for name in ("deletion-worker.lock", "local-deletion-control.lock"):
            lock = self.state / name
            for kind in ("symlink", "nonprivate"):
                with self.subTest(name=name, kind=kind):
                    if lock.exists() or lock.is_symlink():
                        lock.unlink()
                    if kind == "symlink":
                        lock.symlink_to(other)
                    else:
                        lock.touch(mode=0o600)
                        lock.chmod(0o644)
                    self.commands.clear()
                    with self.assertRaisesRegex(recovery.RecoveryError, "symlinked|private, ordinary"):
                        recovery.restore_app(self.bundle, self.root, self.home, runner=self.runner,
                                             idle_check=lambda app: self.fail("Installer must not run"))
                    self.assertEqual(self.inventory(self.app), original)
                    self.assertFalse(any(command[0] == "/usr/bin/ditto" for command in self.commands))
                    lock.unlink()

    def test_restore_drift_refuses_before_app_replacement(self):
        self.activity_locks()
        self.create()
        source = self.root / "README.md"
        source.write_bytes(b"Unreviewed newer source\n")
        original_app = self.inventory(self.app)
        self.commands.clear()
        with self.assertRaisesRegex(recovery.RecoveryError, "review.*source"):
            recovery.restore_app(self.bundle, self.root, self.home, runner=self.runner,
                                 idle_check=lambda app: self.fail("Installer must not run"))
        self.assertEqual(self.inventory(self.app), original_app)
        self.assertEqual(source.read_bytes(), b"Unreviewed newer source\n")
        self.assertFalse(any(command[0] == "/usr/bin/ditto" for command in self.commands))
        self.assertFalse((self.app.parent / ".quill-app-backups").exists())

    def test_restore_rechecks_source_after_copy_before_replacement(self):
        self.activity_locks()
        self.create()
        (self.app / "Contents/MacOS/quill").write_bytes(b"Current valid app\n")
        before_app = self.inventory(self.app)
        source = self.root / "README.md"
        def mutate_source(command, **kwargs):
            result = self.runner(command, **kwargs)
            if command[0] == "/usr/bin/ditto" and command[1] == str(self.bundle / "Quill.app"):
                source.write_bytes(b"Concurrent source edit\n")
            return result
        with self.assertRaises(recovery.RecoveryError):
            recovery.restore_app(self.bundle, self.root, self.home, runner=mutate_source,
                                 idle_check=lambda app: None)
        self.assertEqual(self.inventory(self.app), before_app)
        self.assertEqual(source.read_bytes(), b"Concurrent source edit\n")

    def test_restore_rolls_back_app_if_config_changes_during_final_validation(self):
        self.activity_locks()
        self.create()
        executable = self.app / "Contents/MacOS/quill"
        checkpoint_binary = executable.read_bytes()
        executable.write_bytes(b"Current valid app\n")
        before_app = self.inventory(self.app)
        def mutate_config(command, **kwargs):
            result = self.runner(command, **kwargs)
            if ("--verify" in command and command[-1] == str(self.app)
                    and executable.read_bytes() == checkpoint_binary):
                self.config.write_text('{"concurrent_preference_change":true}\n')
            return result
        with self.assertRaises(recovery.RecoveryError):
            recovery.restore_app(self.bundle, self.root, self.home, runner=mutate_config,
                                 idle_check=lambda app: None)
        self.assertEqual(self.inventory(self.app), before_app)
        self.assertEqual(self.config.read_text(), '{"concurrent_preference_change":true}\n')


if __name__ == "__main__":
    unittest.main()
