"""Packaging transactions in temporary fixtures; no builds, signing, or GUI."""

import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/build-preview-app.py"
SPEC = importlib.util.spec_from_file_location("preview_packager", SCRIPT)
preview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview)
CERTIFICATE = "A" * 40


def snapshot(path):
    return {str(file.relative_to(path)): file.read_bytes() for file in path.rglob("*") if file.is_file()}


class PreviewPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.binary = self.root / ".build/release/quill"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b"stale-binary")
        self.debug_binary = self.root / ".build/debug/quill"
        self.debug_binary.parent.mkdir(parents=True)
        self.debug_binary.write_bytes(b"stale-debug-binary")
        source_plist = self.root / "Sources/quill/Info.plist"
        source_plist.parent.mkdir(parents=True)
        source_plist.write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.digimata.quill",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "2",
            "NSMicrophoneUsageDescription": "Synthetic fixture",
            "NSScreenCaptureUsageDescription": "Synthetic fixture",
        }))
        icon = self.root / "scripts/app-icon/Quill.icns"
        icon.parent.mkdir(parents=True)
        icon.write_bytes(b"synthetic-icon-resource")
        self.app = self.root / ".build/Quill.app"
        executable = self.app / "Contents/MacOS/quill"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"previous-app")
        marker = self.app / preview.MARKER
        marker.parent.mkdir()
        marker.write_text(json.dumps({"schema": preview.SCHEMA, "checkout": str(self.root)}))
        self.original = snapshot(self.app)
        self.commands = []
        self.idle_calls = 0
        self.block_idle_call = None
        self.failure = None
        self.available = CERTIFICATE

    def tearDown(self):
        self.temp.cleanup()

    def idle(self, destination):
        self.assertEqual(destination, self.app)
        self.idle_calls += 1
        if self.idle_calls == self.block_idle_call:
            raise preview.PackagingError("Quill preview is running")

    def runner(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == "sh":
            if self.failure == "build":
                raise subprocess.CalledProcessError(1, command)
            if self.failure != "missing-release":
                self.binary.write_bytes(b"fresh-binary")
            if self.failure == "metadata-change-build":
                self.change_source_metadata()
        elif command[0] == "/usr/bin/security":
            return subprocess.CompletedProcess(command, 0, stdout=f' 1) {self.available} "Synthetic certificate"\n')
        elif "--sign" in command:
            if self.failure == "sign":
                raise subprocess.CalledProcessError(1, command)
            copy = Path(command[-1]) / "Contents/MacOS/quill"
            copy.write_bytes(copy.read_bytes() + b"-signed-copy")
        elif command[-1] == "doctor":
            installed = command[0] == str(self.app / "Contents/MacOS/quill")
            if self.failure == ("installed" if installed else "staging"):
                raise subprocess.CalledProcessError(1, command)
            if self.failure == "source-change" and installed:
                self.binary.write_bytes(b"concurrent-build")
            if self.failure == ("metadata-change-installed" if installed else "metadata-change-staging"):
                self.change_source_metadata()
        return subprocess.CompletedProcess(command, 0, stdout="")

    def change_source_metadata(self):
        path = self.root / "Sources/quill/Info.plist"
        info = plistlib.loads(path.read_bytes())
        info["CFBundleVersion"] = str(int(info["CFBundleVersion"]) + 1)
        path.write_bytes(plistlib.dumps(info))

    def package(self, identity="-"):
        return preview.package(self.root, identity, runner=self.runner, idle_check=self.idle)

    def assert_previous(self):
        self.assertEqual(snapshot(self.app), self.original)

    def assert_staging_clean(self):
        self.assertEqual(list((self.root / ".build").glob(".quill-preview-*")), [])

    def test_running_destination_refused_before_build_or_lock(self):
        self.block_idle_call = 1
        with self.assertRaisesRegex(preview.PackagingError, "running"):
            self.package()
        self.assertEqual(self.commands, [])
        self.assertFalse((self.root / ".build/.quill-preview.lock").exists())
        self.assertEqual(self.binary.read_bytes(), b"stale-binary")
        self.assert_previous()

    def test_always_builds_fresh_and_signs_only_copy(self):
        self.assertEqual(self.package(), self.app)
        self.assertEqual(self.commands[0][0], "sh")
        self.assertEqual(self.commands[0][-5:], ["build", "-c", "release", "--product", "quill"])
        self.assertEqual(self.binary.read_bytes(), b"fresh-binary")
        self.assertEqual((self.app / "Contents/MacOS/quill").read_bytes(), b"fresh-binary-signed-copy")
        self.assertEqual(self.idle_calls, 3)
        marker = json.loads((self.app / preview.MARKER).read_bytes())
        self.assertEqual(marker["source_binary_sha256"], preview.digest(self.binary))
        self.assertEqual(marker["signing_identity"], "-")
        self.assertEqual(marker["build_configuration"], "release")
        self.assertEqual(marker["version"], "0.1.0")
        self.assertEqual(marker["build_number"], "2")
        info = plistlib.loads((self.app / "Contents/Info.plist").read_bytes())
        self.assertEqual(info["CFBundleShortVersionString"], "0.1.0")
        self.assertEqual(info["CFBundleVersion"], "2")
        self.assertEqual(info["CFBundleIconFile"], "Quill.icns")
        self.assertEqual((self.app / "Contents/Resources/Quill.icns").read_bytes(), b"synthetic-icon-resource")
        self.assertFalse(any(command[0] == "/usr/bin/security" for command in self.commands))
        self.assertEqual(self.debug_binary.read_bytes(), b"stale-debug-binary")
        self.assert_staging_clean()

    def test_failed_release_build_never_uses_stale_release_or_debug(self):
        self.failure = "build"
        with self.assertRaises(subprocess.CalledProcessError):
            self.package()
        self.assertEqual(self.binary.read_bytes(), b"stale-binary")
        self.assertEqual(self.debug_binary.read_bytes(), b"stale-debug-binary")
        self.assertFalse(any("--sign" in command for command in self.commands))
        self.assert_previous()
        self.assert_staging_clean()

    def test_missing_release_never_falls_back_to_existing_debug_binary(self):
        self.failure = "missing-release"
        self.binary.unlink()
        with self.assertRaises(FileNotFoundError):
            self.package()
        self.assertEqual(self.debug_binary.read_bytes(), b"stale-debug-binary")
        self.assertFalse(any("--sign" in command for command in self.commands))
        self.assert_previous()
        self.assert_staging_clean()

    def test_invalid_version_metadata_stops_before_build_or_sign(self):
        source = self.root / "Sources/quill/Info.plist"
        original = plistlib.loads(source.read_bytes())
        for key, values in (("CFBundleShortVersionString", (None, 1, "", "0.1", "v0.1.0", "1.0.0-beta", "01.0.0", "1.0.0\n")),
                            ("CFBundleVersion", (None, 2, "", "0", "-1", "02", "10000", "2.0", "2\n"))):
            for value in values:
                metadata = dict(original)
                if value is None:
                    metadata.pop(key)
                else:
                    metadata[key] = value
                source.write_bytes(plistlib.dumps(metadata))
                self.commands = []
                with self.subTest(key=key, value=value), self.assertRaisesRegex(preview.PackagingError, key):
                    self.package()
                self.assertEqual(self.commands, [])
                self.assert_previous()
                self.assertFalse((self.root / ".build/.quill-preview.lock").exists())

    def test_source_version_is_authoritative_without_packager_defaults(self):
        source = self.root / "Sources/quill/Info.plist"
        metadata = plistlib.loads(source.read_bytes())
        metadata.update(CFBundleShortVersionString="0.2.3", CFBundleVersion="12")
        source.write_bytes(plistlib.dumps(metadata))
        self.package()
        info = plistlib.loads((self.app / "Contents/Info.plist").read_bytes())
        marker = json.loads((self.app / preview.MARKER).read_bytes())
        self.assertEqual((info["CFBundleShortVersionString"], info["CFBundleVersion"]), ("0.2.3", "12"))
        self.assertEqual((marker["version"], marker["build_number"]), ("0.2.3", "12"))

    def test_unreadable_or_malformed_source_metadata_stops_before_build(self):
        source = self.root / "Sources/quill/Info.plist"
        for payload in (None, b"not a plist", b'<?xml version="1.0"?><plist><dict><key>oops</key>',
                        plistlib.dumps([]), plistlib.dumps({"CFBundleIdentifier": "other.application"})):
            if payload is None:
                source.unlink()
            else:
                source.write_bytes(payload)
            self.commands = []
            with self.subTest(payload=payload), self.assertRaises(preview.PackagingError):
                self.package()
            self.assertEqual(self.commands, [])
            self.assert_previous()

    def test_app_opened_during_build_refused_before_swap(self):
        self.block_idle_call = 3
        with self.assertRaisesRegex(preview.PackagingError, "running"):
            self.package()
        self.assert_previous()
        self.assert_staging_clean()

    def test_staging_validation_failure_keeps_previous(self):
        self.failure = "staging"
        with self.assertRaises(subprocess.CalledProcessError):
            self.package()
        self.assert_previous()
        self.assert_staging_clean()

    def test_installed_validation_failure_restores_previous(self):
        self.failure = "installed"
        with self.assertRaises(subprocess.CalledProcessError):
            self.package()
        self.assert_previous()
        self.assert_staging_clean()

    def test_failed_publish_restores_previous(self):
        rename = Path.rename

        def fail_publish(path, target):
            if path.name == "Quill.app" and path.parent.name.startswith(".quill-preview-"):
                raise OSError("Synthetic publish failure")
            return rename(path, target)

        with patch.object(Path, "rename", fail_publish):
            with self.assertRaisesRegex(OSError, "publish failure"):
                self.package()
        self.assert_previous()
        self.assert_staging_clean()

    def test_source_change_during_installed_check_restores_previous(self):
        self.failure = "source-change"
        with self.assertRaisesRegex(preview.PackagingError, "Source binary changed"):
            self.package()
        self.assert_previous()
        self.assert_staging_clean()

    def test_concurrent_metadata_edit_never_publishes_a_mismatched_version(self):
        for phase in ("build", "staging", "installed"):
            self.failure = "metadata-change-" + phase
            self.commands = []
            with self.subTest(phase=phase), self.assertRaisesRegex(preview.PackagingError, "Info.plist changed"):
                self.package()
            if phase == "build":
                self.assertFalse(any("--sign" in command for command in self.commands))
            self.assert_previous()
            self.assert_staging_clean()

    def test_failed_rollback_preserves_recovery_files(self):
        self.failure = "installed"
        rename = Path.rename

        def fail_restore(path, target):
            if path.name == "previous.app":
                raise OSError("Synthetic restore failure")
            return rename(path, target)

        with patch.object(Path, "rename", fail_restore):
            with self.assertRaisesRegex(preview.PackagingError, "Recovery files preserved"):
                self.package()
        backups = list((self.root / ".build").glob(".quill-preview-*/previous.app"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(snapshot(backups[0]), self.original)

    def test_exact_existing_identity_is_used_once(self):
        self.package(CERTIFICATE.lower())
        signs = [command for command in self.commands if "--sign" in command]
        self.assertEqual(len(signs), 1)
        self.assertEqual(signs[0][signs[0].index("--sign") + 1], CERTIFICATE)
        marker = json.loads((self.app / preview.MARKER).read_bytes())
        self.assertEqual(marker["signing_identity"], CERTIFICATE)

    def test_unavailable_or_ambiguous_identity_never_builds_or_falls_back(self):
        for identity in ["B" * 40, "Synthetic certificate"]:
            with self.subTest(identity=identity):
                self.commands = []
                with self.assertRaises(preview.PackagingError):
                    self.package(identity)
                self.assertFalse(any(command[0] == "sh" or "--sign" in command for command in self.commands))
                self.assertEqual(self.binary.read_bytes(), b"stale-binary")
                self.assert_previous()

    def test_selected_identity_signing_failure_never_falls_back(self):
        self.failure = "sign"
        with self.assertRaises(subprocess.CalledProcessError):
            self.package(CERTIFICATE)
        self.assertEqual(len([command for command in self.commands if "--sign" in command]), 1)
        self.assert_previous()
        self.assert_staging_clean()

    def test_stable_identity_cannot_be_replaced_or_downgraded(self):
        marker = self.app / preview.MARKER
        value = json.loads(marker.read_text())
        value["signing_identity"] = CERTIFICATE
        marker.write_text(json.dumps(value))
        expected = snapshot(self.app)
        for identity in ["-", "B" * 40]:
            self.available = identity
            self.commands = []
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(preview.PackagingError, "replace Quill's stable"):
                    self.package(identity)
                self.assertEqual(snapshot(self.app), expected)
                self.assertFalse(any(command[0] == "sh" or "--sign" in command for command in self.commands))

    def test_unrecognized_destination_is_preserved(self):
        (self.app / preview.MARKER).unlink()
        with self.assertRaisesRegex(preview.PackagingError, "unrecognized"):
            self.package()
        self.assertEqual(self.commands, [])
        self.assertEqual((self.app / "Contents/MacOS/quill").read_bytes(), b"previous-app")


class SigningConfigurationTests(unittest.TestCase):
    def test_saved_certificate_is_used_without_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "signing.json"
            config.write_text(json.dumps({"identity": CERTIFICATE}))
            self.assertEqual(preview.configured_identity(config=config), CERTIFICATE)

    def test_missing_or_invalid_configuration_never_defaults_to_adhoc(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "signing.json"
            with self.assertRaises(preview.PackagingError):
                preview.configured_identity(config=config)
            for data in ['{}', '{', '{"identity": "-"}', '{"identity": 42}', 'null']:
                config.write_text(data)
                with self.subTest(data=data), self.assertRaises(preview.PackagingError):
                    preview.configured_identity(config=config)

    def test_explicit_identity_is_not_overridden_by_a_saved_setting(self):
        self.assertEqual(preview.configured_identity(CERTIFICATE, config=Path("/missing")), CERTIFICATE)


class PreviewProcessGuardTests(unittest.TestCase):
    def library(self, paths, count_error=False):
        class Function:
            def __init__(self, function):
                self.function = function

            def __call__(self, *args):
                return self.function(*args)

        def list_pids(buffer, size):
            if count_error:
                return -1
            if buffer is not None:
                for index, pid in enumerate(paths):
                    buffer[index] = pid
            return len(paths)

        def pid_path(pid, buffer, size):
            buffer.value = paths[pid].encode()
            return len(buffer.value)

        class Library:
            proc_listallpids = Function(list_pids)
            proc_pidpath = Function(pid_path)

        return Library()

    def test_exact_destination_process_is_refused(self):
        app = Path("/synthetic/.build/Quill.app")
        library = self.library({12: "/other/quill", 34: str(app / "Contents/MacOS/quill")})
        with patch.object(preview.ctypes, "CDLL", return_value=library):
            with self.assertRaisesRegex(preview.PackagingError, "running"):
                preview.require_idle(app)

    def test_other_checkout_process_does_not_block(self):
        library = self.library({12: "/other/Quill.app/Contents/MacOS/quill"})
        with patch.object(preview.ctypes, "CDLL", return_value=library):
            preview.require_idle(Path("/synthetic/.build/Quill.app"))

    def test_process_inventory_failure_stops_packaging(self):
        with patch.object(preview.ctypes, "CDLL", return_value=self.library({}, count_error=True)):
            with self.assertRaisesRegex(preview.PackagingError, "Cannot check"):
                preview.require_idle(Path("/synthetic/.build/Quill.app"))


if __name__ == "__main__":
    unittest.main()
