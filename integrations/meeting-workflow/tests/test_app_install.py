"""Isolated installer transactions; no real apps, signing, or process services."""

import ctypes
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/install-app.py"
SPEC = importlib.util.spec_from_file_location("quill_installer", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
CERTIFICATE = b"synthetic-signing-certificate"
IDENTITY = hashlib.sha1(CERTIFICATE).hexdigest().upper()
OTHER_CERTIFICATE = b"another-synthetic-certificate"
OTHER_IDENTITY = hashlib.sha1(OTHER_CERTIFICATE).hexdigest().upper()


class AppInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "checkout"
        self.root.mkdir()
        self.source = self.root / ".build/Quill.app"
        self.applications = self.base / "Applications"
        self.applications.mkdir()
        self.destination = self.applications / "Quill.app"
        self.make_app(self.source, b"new-signed-executable")
        self.make_app(self.destination, b"old-signed-executable")
        self.original = installer.fingerprint(self.destination)
        self.source_original = installer.fingerprint(self.source)
        self.commands = []
        self.idle_calls = 0
        self.block_idle_call = None
        self.failure = None
        self.certificates = {}
        self.old_existed_at_installed_validation = False
        self.reports = []

    def tearDown(self):
        self.temp.cleanup()

    def make_app(self, app, content, *, identity=IDENTITY, root=None):
        executable = app / "Contents/MacOS/quill"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(content)
        executable.chmod(0o755)
        info = {"CFBundleIdentifier": installer.BUNDLE_ID, "CFBundleExecutable": "quill", "CFBundlePackageType": "APPL"}
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        marker = app / installer.MARKER
        marker.parent.mkdir()
        marker.write_text(json.dumps({"schema": installer.SCHEMA, "checkout": str(root or self.root), "signing_identity": identity}))
        (app / "Contents/_CodeSignature").mkdir()
        (app / "Contents/_CodeSignature/CodeResources").write_bytes(b"synthetic-sealed-resources")

    def runner(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == "/usr/bin/ditto":
            shutil.copytree(command[1], command[2])
            is_backup = ".quill-app-backups" in Path(command[2]).parts
            if self.failure == "backup-copy" and is_backup:
                raise subprocess.CalledProcessError(1, command)
            if self.failure == "backup-change" and is_backup:
                (Path(command[2]) / "Contents/MacOS/quill").write_bytes(b"corrupt-backup")
            if self.failure == "copy-change":
                (Path(command[2]) / "Contents/MacOS/quill").write_bytes(b"changed-copy")
            if self.failure == "source-change":
                (self.source / "Contents/MacOS/quill").write_bytes(b"changed-source")
            if self.failure == "destination-change":
                (self.destination / "Contents/MacOS/quill").write_bytes(b"changed-destination")
            if self.failure == "copy":
                raise subprocess.CalledProcessError(1, command)
        elif "--verify" in command:
            app = Path(command[-1])
            is_staged = app.parent.name.startswith(".quill-install-")
            is_installed = app == self.destination and (app / "Contents/MacOS/quill").read_bytes() == b"new-signed-executable"
            if is_installed:
                self.old_existed_at_installed_validation = bool(list(self.applications.glob(".quill-install-*/previous.app")))
            if (self.failure == "source-signature" and app == self.source
                    or self.failure == "staging" and is_staged
                    or self.failure == "installed" and is_installed):
                raise subprocess.CalledProcessError(1, command)
            if self.failure == "source-change-installed" and is_installed:
                (self.source / "Contents/MacOS/quill").write_bytes(b"concurrent-source-change")
            if self.failure == "retained-change" and is_installed:
                backup = next(self.applications.glob(".quill-app-backups/*/Quill.app"))
                (backup / "Contents/MacOS/quill").write_bytes(b"changed-retained-backup")
            if self.failure == "backup-signature" and ".quill-app-backups" in app.parts:
                raise subprocess.CalledProcessError(1, command)
        elif any(arg.startswith("--extract-certificates=") for arg in command):
            prefix = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--extract-certificates=")))
            certificate = self.certificates.get(command[-1], CERTIFICATE)
            if certificate is not None:
                Path(str(prefix) + "0").write_bytes(certificate)
        return subprocess.CompletedProcess(command, 0, stdout="")

    def idle(self, destination):
        self.assertEqual(destination, self.destination)
        self.idle_calls += 1
        if self.block_idle_call == self.idle_calls:
            raise installer.InstallError("Destination Quill is running")

    def install(self, **kwargs):
        return installer.install(self.root, self.destination, runner=self.runner, idle_check=self.idle,
                                 report=self.reports.append, **kwargs)

    def assert_previous(self):
        self.assertEqual(installer.fingerprint(self.destination), self.original)

    def assert_clean(self):
        self.assertEqual(list(self.applications.glob(".quill-install-*/")), [])

    def edit_marker(self, app, **changes):
        path = app / installer.MARKER
        value = json.loads(path.read_text())
        value.update(changes)
        path.write_text(json.dumps(value))

    def test_signed_update_preserves_all_source_bytes_and_identity(self):
        self.assertEqual(self.install(), self.destination)
        self.assertEqual(installer.fingerprint(self.destination), self.source_original)
        self.assertEqual(installer.fingerprint(self.source), self.source_original)
        self.assertTrue(self.old_existed_at_installed_validation)
        self.assertEqual(self.idle_calls, 3)
        self.assertTrue(all(command[0] in ["/usr/bin/codesign", "/usr/bin/plutil", "/usr/bin/ditto"] for command in self.commands))
        self.assertFalse(any("--sign" in command for command in self.commands))
        self.assert_clean()

    def test_release_metadata_installs_over_legacy_marker_and_keeps_recovery_compatible(self):
        self.edit_marker(self.source, build_configuration="release", version="0.1.0", build_number="2")
        info_path = self.source / "Contents/Info.plist"
        info = plistlib.loads(info_path.read_bytes())
        info.update(CFBundleShortVersionString="0.1.0", CFBundleVersion="2")
        info_path.write_bytes(plistlib.dumps(info))
        source = installer.fingerprint(self.source)
        self.install()
        self.assertEqual(installer.fingerprint(self.destination), source)
        marker = json.loads((self.destination / installer.MARKER).read_bytes())
        self.assertEqual((marker["build_configuration"], marker["version"], marker["build_number"]),
                         ("release", "0.1.0", "2"))
        backup = next(self.applications.glob(".quill-app-backups/*/Quill.app"))
        self.assertEqual(installer.fingerprint(backup), self.original)
        self.install(source_app=backup)
        self.assert_previous()
        self.assert_clean()

    def test_first_install(self):
        shutil.rmtree(self.destination)
        self.install()
        self.assertEqual(installer.fingerprint(self.destination), self.source_original)
        self.assertFalse(self.old_existed_at_installed_validation)
        self.assert_clean()
        self.assertFalse((self.applications / ".quill-app-backups").exists())

    def test_success_retains_private_verified_backup_with_complete_receipt(self):
        self.install()
        directory = next((self.applications / ".quill-app-backups").iterdir())
        backup = directory / "Quill.app"
        self.assertEqual(installer.fingerprint(backup), self.original)
        self.assertEqual(installer.validate_app(backup, self.root, self.runner), IDENTITY)
        for private in [directory, directory.parent]:
            self.assertEqual(private.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / "manifest.json").stat().st_mode & 0o777, 0o600)
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(manifest["schema"], installer.BACKUP_SCHEMA)
        self.assertEqual(manifest["status"], "installed")
        self.assertEqual(manifest["original_checkout"], str(self.root))
        self.assertEqual(manifest["signing_identity"], IDENTITY)
        self.assertEqual(manifest["previous_app_fingerprint"], [list(item) for item in self.original])
        self.assertEqual(manifest["installed_app_fingerprint"], [list(item) for item in self.source_original])
        self.assertIn("workflow source", manifest["scope"])
        self.assertTrue(any(str(backup) in line for line in self.reports))

    def test_success_never_overwrites_or_prunes_previous_backups(self):
        self.install()
        first_directory = next((self.applications / ".quill-app-backups").iterdir())
        first_receipt = (first_directory / "manifest.json").read_bytes()
        self.install()
        self.assertEqual(len(list((self.applications / ".quill-app-backups").iterdir())), 2)
        self.assertEqual(installer.fingerprint(first_directory / "Quill.app"), self.original)
        self.assertEqual((first_directory / "manifest.json").read_bytes(), first_receipt)

    def test_archived_signed_source_restores_app_and_retains_replaced_version(self):
        self.install()
        archive = next(self.applications.glob(".quill-app-backups/*/Quill.app"))
        self.install(source_app=archive)
        self.assert_previous()
        backups = list(self.applications.glob(".quill-app-backups/*/Quill.app"))
        self.assertEqual(len(backups), 2)
        self.assertEqual(installer.fingerprint(next(app for app in backups if app != archive)), self.source_original)
        self.assertEqual(installer.fingerprint(archive), self.original)
        self.assertEqual(installer.fingerprint(self.source), self.source_original)

    def test_backup_copy_signature_or_fingerprint_failure_preserves_installed_app(self):
        for failure in ["backup-copy", "backup-signature", "backup-change"]:
            with self.subTest(failure=failure):
                self.failure = failure
                with self.assertRaises((installer.InstallError, subprocess.CalledProcessError)):
                    self.install()
                self.assert_previous()
                self.assert_clean()

    def test_backup_manifest_write_failure_happens_before_replacement(self):
        with patch.object(installer, "save_backup_manifest", side_effect=OSError("Backup disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.install()
        self.assert_previous()
        self.assert_clean()

    def test_retained_backup_changed_before_completion_restores_original(self):
        self.failure = "retained-change"
        with self.assertRaisesRegex(installer.InstallError, "retained previous app changed"):
            self.install()
        self.assert_previous()
        self.assert_clean()

    def test_final_backup_receipt_failure_rolls_back_and_records_it(self):
        save = installer.save_backup_manifest
        def fail_installed_receipt(directory, manifest):
            save(directory, manifest)
            if manifest["status"] == "installed":
                raise OSError("Synthetic installed receipt sync failure")
        with patch.object(installer, "save_backup_manifest", fail_installed_receipt):
            with self.assertRaisesRegex(OSError, "receipt sync failure"):
                self.install()
        self.assert_previous()
        self.assert_clean()
        receipt = next(self.applications.glob(".quill-app-backups/*/manifest.json"))
        self.assertEqual(json.loads(receipt.read_text())["status"], "rolled_back")

    def test_prepublication_consistency_failure_leaves_previous_app(self):
        def changed_dependencies():
            self.assert_previous()
            raise installer.InstallError("Workflow dependencies changed")
        with self.assertRaisesRegex(installer.InstallError, "dependencies changed"):
            self.install(consistency_check=changed_dependencies)
        self.assert_previous()
        self.assert_clean()
        self.assertFalse(self.old_existed_at_installed_validation)

    def test_final_consistency_failure_restores_previous_without_rechecking_drift(self):
        checks = []
        def changed_dependencies():
            checks.append(installer.fingerprint(self.destination))
            if len(checks) > 1:
                raise installer.InstallError("Workflow dependencies changed")
        with self.assertRaisesRegex(installer.InstallError, "dependencies changed"):
            self.install(consistency_check=changed_dependencies)
        self.assertEqual(checks, [self.original, self.source_original])
        self.assert_previous()
        self.assert_clean()
        self.assertTrue(self.old_existed_at_installed_validation)
        receipt = next(self.applications.glob(".quill-app-backups/*/manifest.json"))
        self.assertEqual(json.loads(receipt.read_text())["status"], "rolled_back")

    def test_symlinked_or_public_backup_directory_is_refused(self):
        backup_root = self.applications / ".quill-app-backups"
        elsewhere = self.base / "other-backups"
        elsewhere.mkdir()
        backup_root.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, "symlinked"):
            self.install()
        self.assert_previous()
        backup_root.unlink()
        backup_root.mkdir(mode=0o755)
        with self.assertRaisesRegex(installer.InstallError, "must be private"):
            self.install()
        self.assert_previous()

    def test_source_cannot_contain_new_backup_target(self):
        archive = self.applications / ".quill-app-backups"
        self.make_app(archive, b"archived-signed-executable")
        with self.assertRaisesRegex(installer.InstallError, "backup directory must be separate"):
            self.install(source_app=archive)
        self.assert_previous()

    def test_archived_source_cannot_be_inside_destination(self):
        with self.assertRaisesRegex(installer.InstallError, "must be separate"):
            self.install(source_app=self.destination / "Contents/old.app")
        self.assert_previous()
        self.assertEqual(self.commands, [])

    def test_archived_source_keeps_signer_checkout_and_signature_guards(self):
        archive = self.base / "ArchivedQuill.app"
        self.make_app(archive, b"archived-signed-executable")
        self.certificates[str(archive)] = OTHER_CERTIFICATE
        with self.assertRaisesRegex(installer.InstallError, "actual signing certificate"):
            self.install(source_app=archive)
        self.assert_previous()
        self.edit_marker(archive, signing_identity=OTHER_IDENTITY)
        with self.assertRaisesRegex(installer.InstallError, "stable signing identity"):
            self.install(source_app=archive)
        self.assert_previous()
        self.edit_marker(archive, signing_identity=IDENTITY)
        self.certificates.pop(str(archive))
        self.edit_marker(archive, checkout=str(self.base / "other-checkout"))
        with self.assertRaisesRegex(installer.InstallError, "unrecognized"):
            self.install(source_app=archive)
        self.assert_previous()
        self.edit_marker(archive, checkout=str(self.root))
        def invalid_signature(command, **kwargs):
            if "--verify" in command and command[-1] == str(archive):
                raise subprocess.CalledProcessError(1, command)
            return self.runner(command, **kwargs)
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install(self.root, self.destination, source_app=archive,
                              runner=invalid_signature, idle_check=self.idle)
        self.assert_previous()

    def test_archived_source_changed_during_copy_is_refused(self):
        archive = self.base / "ArchivedQuill.app"
        self.make_app(archive, b"archived-signed-executable")
        def changing_source(command, **kwargs):
            result = self.runner(command, **kwargs)
            if command[0] == "/usr/bin/ditto" and command[1] == str(archive):
                (archive / "Contents/MacOS/quill").write_bytes(b"corrupt-archive")
            return result
        with self.assertRaisesRegex(installer.InstallError, "preview app changed"):
            installer.install(self.root, self.destination, source_app=archive,
                              runner=changing_source, idle_check=self.idle)
        self.assert_previous()
        self.assert_clean()

    def test_running_destination_refused_before_validation_lock_or_copy(self):
        self.block_idle_call = 1
        with self.assertRaisesRegex(installer.InstallError, "running"):
            self.install()
        self.assertEqual(self.commands, [])
        self.assertFalse((self.applications / ".quill-install.lock").exists())
        self.assert_previous()

    def test_destination_launched_during_copy_is_preserved(self):
        self.block_idle_call = 3
        with self.assertRaisesRegex(installer.InstallError, "running"):
            self.install()
        self.assert_previous()
        self.assert_clean()

    def test_destination_must_belong_to_this_checkout(self):
        self.edit_marker(self.destination, checkout=str(self.base / "other"))
        original = installer.fingerprint(self.destination)
        with self.assertRaisesRegex(installer.InstallError, "unrecognized"):
            self.install()
        self.assertEqual(installer.fingerprint(self.destination), original)
        self.assertFalse(any(command[0] == "/usr/bin/ditto" for command in self.commands))

    def test_source_must_belong_to_this_checkout(self):
        self.edit_marker(self.source, checkout=str(self.base / "other"))
        with self.assertRaisesRegex(installer.InstallError, "unrecognized"):
            self.install()
        self.assert_previous()

    def test_no_adhoc_or_missing_source_identity(self):
        for identity in ["-", "", "Certificate Name", 42]:
            with self.subTest(identity=identity):
                self.edit_marker(self.source, signing_identity=identity)
                with self.assertRaisesRegex(installer.InstallError, "stable certificate"):
                    self.install()
                self.assert_previous()

    def test_actual_certificate_must_match_marker(self):
        for certificate in [OTHER_CERTIFICATE, None]:
            with self.subTest(certificate=certificate):
                self.certificates[str(self.source)] = certificate
                with self.assertRaisesRegex(installer.InstallError, "actual signing certificate"):
                    self.install()
                self.assert_previous()

    def test_different_valid_destination_identity_refused(self):
        self.edit_marker(self.destination, signing_identity=OTHER_IDENTITY)
        self.certificates[str(self.destination)] = OTHER_CERTIFICATE
        original = installer.fingerprint(self.destination)
        with self.assertRaisesRegex(installer.InstallError, "stable signing identity"):
            self.install()
        self.assertEqual(installer.fingerprint(self.destination), original)

    def test_invalid_source_signature_preserves_destination(self):
        self.failure = "source-signature"
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assert_previous()
        self.assertFalse((self.applications / ".quill-install.lock").exists())

    def test_invalid_bundle_metadata_refused(self):
        path = self.source / "Contents/Info.plist"
        original = plistlib.loads(path.read_bytes())
        for field, value in [("CFBundleIdentifier", "example.other"), ("CFBundleExecutable", "other"), ("CFBundlePackageType", "BNDL")]:
            with self.subTest(field=field):
                path.write_bytes(plistlib.dumps({**original, field: value}))
                with self.assertRaisesRegex(installer.InstallError, "Unexpected Quill"):
                    self.install()
                self.assert_previous()

    def test_missing_or_nonexecutable_binary_refused(self):
        binary = self.source / "Contents/MacOS/quill"
        binary.chmod(0o644)
        with self.assertRaisesRegex(installer.InstallError, "not executable"):
            self.install()
        binary.unlink()
        with self.assertRaisesRegex(installer.InstallError, "missing"):
            self.install()
        self.assert_previous()

    def test_symlinked_destination_refused_without_following_it(self):
        other = self.applications / "Other.app"
        self.destination.rename(other)
        self.destination.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, "symlinked"):
            self.install()
        self.assertEqual(installer.fingerprint(other), self.original)
        self.assertEqual(self.commands, [])

    def test_symlinked_parent_refused(self):
        link = self.base / "LinkedApplications"
        link.symlink_to(self.applications, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, "symlinked"):
            installer.install(self.root, link / "Quill.app", runner=self.runner, idle_check=self.idle)
        self.assert_previous()

    def test_link_inside_source_refused(self):
        binary = self.source / "Contents/MacOS/quill"
        binary.unlink()
        binary.symlink_to(self.destination / "Contents/MacOS/quill")
        with self.assertRaisesRegex(installer.InstallError, "linked or special"):
            self.install()
        self.assert_previous()

    def test_install_over_preview_refused(self):
        with self.assertRaisesRegex(installer.InstallError, "separate"):
            installer.install(self.root, self.source, runner=self.runner, idle_check=self.idle)
        self.assertEqual(self.commands, [])

    def test_copy_or_staged_validation_failure_keeps_old_app(self):
        for failure in ["copy", "staging"]:
            with self.subTest(failure=failure):
                self.failure = failure
                with self.assertRaises(subprocess.CalledProcessError):
                    self.install()
                self.assert_previous()
                self.assert_clean()

    def test_changed_copy_refused(self):
        self.failure = "copy-change"
        with self.assertRaisesRegex(installer.InstallError, "staged app differs"):
            self.install()
        self.assert_previous()
        self.assert_clean()

    def test_concurrent_source_change_refused(self):
        self.failure = "source-change"
        with self.assertRaisesRegex(installer.InstallError, "preview app changed"):
            self.install()
        self.assert_previous()
        self.assert_clean()

    def test_concurrent_destination_change_is_not_overwritten(self):
        self.failure = "destination-change"
        with self.assertRaisesRegex(installer.InstallError, "installed app changed"):
            self.install()
        self.assertEqual((self.destination / "Contents/MacOS/quill").read_bytes(), b"changed-destination")
        self.assert_clean()

    def test_installed_validation_failure_restores_old_app(self):
        self.failure = "installed"
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assertTrue(self.old_existed_at_installed_validation)
        self.assert_previous()
        self.assert_clean()

    def test_first_install_failure_removes_failed_app(self):
        shutil.rmtree(self.destination)
        self.failure = "installed"
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assertFalse(self.destination.exists())
        self.assert_clean()

    def test_source_changed_during_final_validation_restores_previous(self):
        self.failure = "source-change-installed"
        with self.assertRaisesRegex(installer.InstallError, "preview app changed"):
            self.install()
        self.assert_previous()
        self.assert_clean()

    def test_publish_failure_restores_previous(self):
        rename = Path.rename
        def fail_publish(path, target):
            if path.name == "Quill.app" and path.parent.name.startswith(".quill-install-"):
                raise OSError("Synthetic publish failure")
            return rename(path, target)
        with patch.object(Path, "rename", fail_publish):
            with self.assertRaisesRegex(OSError, "publish failure"):
                self.install()
        self.assert_previous()
        self.assert_clean()

    def test_failed_restore_retains_previous_in_recovery_directory(self):
        self.failure = "installed"
        rename = Path.rename
        def fail_restore(path, target):
            if path.name == "previous.app":
                raise OSError("Synthetic restore failure")
            return rename(path, target)
        with patch.object(Path, "rename", fail_restore):
            with self.assertRaisesRegex(installer.InstallError, "Recovery files preserved"):
                self.install()
        backups = list(self.applications.glob(".quill-install-*/previous.app"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(installer.fingerprint(backups[0]), self.original)

    def test_new_app_started_during_failed_validation_is_not_removed(self):
        self.failure = "installed"
        self.block_idle_call = 4
        with self.assertRaisesRegex(installer.InstallError, "Recovery files preserved"):
            self.install()
        self.assertEqual(installer.fingerprint(self.destination), self.source_original)
        backups = list(self.applications.glob(".quill-install-*/previous.app"))
        self.assertEqual(installer.fingerprint(backups[0]), self.original)

    def test_existing_installer_lock_prevents_copy(self):
        with (self.applications / ".quill-install.lock").open("w") as lock:
            installer.fcntl.flock(lock, installer.fcntl.LOCK_EX | installer.fcntl.LOCK_NB)
            with self.assertRaisesRegex(installer.InstallError, "Another Quill installer"):
                self.install()
        self.assert_previous()
        self.assertFalse(any(command[0] == "/usr/bin/ditto" for command in self.commands))


class InstallerProcessGuardTests(unittest.TestCase):
    def library(self, paths, count_error=False):
        class Function:
            def __init__(self, function):
                self.function = function
            def __call__(self, *args):
                return self.function(*args)
        def list_pids(buffer, size):
            if count_error:
                return -1
            if buffer is None:
                return len(paths) + 1
            for index, pid in enumerate(paths):
                buffer[index] = pid
            return len(paths)
        def pid_path(pid, buffer, size):
            value = paths[pid].encode()
            ctypes.memmove(buffer, value + b"\0", len(value) + 1)
            return len(value)
        class Library:
            proc_listallpids = Function(list_pids)
            proc_pidpath = Function(pid_path)
        return Library()

    def test_exact_destination_process_is_refused(self):
        app = Path("/Applications/Quill.app")
        with patch.object(installer.ctypes, "CDLL", return_value=self.library({123: str(app / "Contents/MacOS/quill")})):
            with self.assertRaisesRegex(installer.InstallError, "destination Quill app is running"):
                installer.require_idle(app)

    def test_preview_or_unrelated_process_is_not_stopped_or_blocked(self):
        with patch.object(installer.ctypes, "CDLL", return_value=self.library({123: "/checkout/.build/Quill.app/Contents/MacOS/quill", 456: "/Applications/Other.app/Contents/MacOS/other"})):
            installer.require_idle(Path("/Applications/Quill.app"))

    def test_process_enumeration_failure_is_closed(self):
        with patch.object(installer.ctypes, "CDLL", return_value=self.library({}, count_error=True)):
            with self.assertRaisesRegex(installer.InstallError, "Cannot check"):
                installer.require_idle(Path("/Applications/Quill.app"))


if __name__ == "__main__":
    unittest.main()
