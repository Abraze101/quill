"""Exercise packaging and the installed executable using synthetic temp homes."""
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = "a" * 32
ORIGIN = "chrome-extension://" + EXTENSION + "/"


class BrowserHostInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.source = self.home / "Documents/test-checkout/integrations/meeting-workflow"
        self.source.mkdir(parents=True)
        for name in ("native-host.py", "install-browser-host.py"):
            shutil.copyfile(ROOT / name, self.source / name)
        self.environment = {**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        self.environment.pop("PYTHONPATH", None)
        self.support = self.home / "Library/Application Support"
        self.host = self.support / "Quill/BrowserBridge/native-host.py"
        self.manifest = self.support / "Google/Chrome/NativeMessagingHosts/com.digimata.quill.meetings.json"
        self.backup = self.manifest.with_name(self.manifest.name + ".repo-backup")
        self.signals = self.home / ".local/state/quill/meeting-signals"

    def tearDown(self):
        self.temp.cleanup()

    def install(self, extension=EXTENSION, browser="chrome"):
        return subprocess.run([sys.executable, "-B", str(self.source / "install-browser-host.py"),
                               "--browser", browser, "--extension-id", extension],
                              env=self.environment, capture_output=True, timeout=10)

    def expected(self, host=None):
        return {"name": "com.digimata.quill.meetings", "description": "Local Quill meeting state bridge",
                "path": str(host or self.host), "type": "stdio", "allowed_origins": [ORIGIN]}

    def write_legacy(self, changes=None):
        manifest = self.expected(self.source / "native-host.py")
        manifest.update(changes or {})
        payload = json.dumps(manifest, indent=4).encode() + b"\n"
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_bytes(payload)
        return payload

    def run_host(self, origin=ORIGIN):
        message = {"source": "synthetic-tab", "meeting": "synthetic-call", "provider": "google-meet",
                   "state": "joined", "requestID": "delivery-1", "url": "not-persisted"}
        payload = json.dumps(message).encode()
        return subprocess.run([str(self.host), origin], env=self.environment, cwd=self.host.parent,
                              input=struct.pack("=I", len(payload)) + payload,
                              capture_output=True, timeout=10)

    def test_installed_executable_works_after_source_checkout_is_unavailable(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.manifest.read_bytes()), self.expected())
        self.assertFalse(self.host.is_symlink())
        self.assertEqual(self.host.read_bytes(), (self.source / "native-host.py").read_bytes())
        self.assertNotIn(b"quill_workflow", self.host.read_bytes())
        self.source.rename(self.root / "source-unavailable")
        result = self.run_host()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        length = struct.unpack("=I", result.stdout[:4])[0]
        self.assertEqual(len(result.stdout), 4 + length)
        self.assertEqual(json.loads(result.stdout[4:]), {"accepted": True, "requestID": "delivery-1",
                                                     "recordingStatus": {"state": "unavailable"}})
        event = json.loads(next(self.signals.glob("*.json")).read_bytes())
        self.assertEqual(event["state"], "joined")
        self.assertNotIn("url", event)
        for file in (self.manifest, *self.signals.glob("*.json")):
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.host.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.host.parent.stat().st_mode), 0o700)

    def test_legacy_migration_backs_up_original_bytes_once_and_updates_are_idempotent(self):
        original = self.write_legacy()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backup.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o600)
        before = {path: path.stat().st_mtime_ns for path in (self.host, self.manifest, self.backup)}
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in before})
        source = self.source / "native-host.py"
        source.write_bytes(source.read_bytes() + b"\n# Synthetic packaged update.\n")
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.host.read_bytes(), source.read_bytes())
        self.assertEqual(self.backup.read_bytes(), original)
        self.assertEqual(self.run_host().returncode, 0)
        self.assertFalse(list(self.host.parent.glob(".quill-install-*")))

    def test_unrelated_registration_is_preserved_without_installing_host(self):
        for changes in ({"path": "/unrelated/native-host.py"}, {"type": "other"},
                        {"name": "unrelated.host"}, {"allowed_origins": ["chrome-extension://" + "b" * 32 + "/"]},
                        {"allowed_origins": [ORIGIN, "chrome-extension://" + "b" * 32 + "/"]},
                        {"unrelated": True}):
            with self.subTest(changes=changes):
                original = self.write_legacy(changes)
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.manifest.read_bytes(), original)
                self.assertFalse(self.host.exists())
                self.assertFalse(self.backup.exists())

    def test_selected_origin_cannot_be_changed_by_reinstall(self):
        self.assertEqual(self.install().returncode, 0)
        original = self.manifest.read_bytes()
        self.assertNotEqual(self.install(extension="b" * 32).returncode, 0)
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertNotEqual(self.install(extension="invalid-extension").returncode, 0)
        self.assertEqual(self.manifest.read_bytes(), original)

    def test_installed_host_rejects_malformed_origins_without_signals(self):
        self.assertEqual(self.install().returncode, 0)
        for origin in ("https://meet.google.com/", "chrome-extension://" + "z" * 32 + "/",
                       "chrome-extension://" + EXTENSION, ORIGIN + "extra"):
            with self.subTest(origin=origin):
                result = self.run_host(origin)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertFalse(self.signals.exists())

    def test_edge_install_uses_only_its_exact_browser_registration(self):
        result = self.install(browser="edge")
        self.assertEqual(result.returncode, 0, result.stderr)
        edge = self.support / "Microsoft Edge/NativeMessagingHosts/com.digimata.quill.meetings.json"
        self.assertEqual(json.loads(edge.read_bytes()), self.expected())
        self.assertFalse(self.manifest.exists())

    def test_conflicting_backup_and_linked_manifest_are_never_overwritten(self):
        original = self.write_legacy()
        self.backup.write_text('{"unrelated": true}\n')
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertFalse(self.host.exists())
        self.manifest.unlink()
        target = self.root / "unrelated.json"
        target.write_bytes(original)
        self.manifest.symlink_to(target)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertTrue(self.manifest.is_symlink())
        self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
