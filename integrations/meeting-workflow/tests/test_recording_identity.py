"""Synthetic filesystem identity changes; never touch actual recording state."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.pipeline import Queue, atomic_json
from quill_workflow.recording_identity import volume_identity
from quill_workflow.recording_safety import FILENAME, RecordingSafetyHold
from quill_workflow.local_naming import session_path


class RecordingIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.session = self.root / "recordings/synthetic"
        self.session.mkdir(parents=True)
        for name in ("mic.caf", "system.caf"):
            (self.session / name).write_bytes(b"synthetic fixture, not captured media")
        atomic_json(self.session / "meta.json", {
            "started": "2026-01-01T10:00:00Z", "ended": "2026-01-01T10:01:00Z",
            "files": {"mic": "mic.caf", "system": "system.caf"}})
        self.safety = dict(schema_version=1, session_id=str(uuid.uuid4()), owner="manual",
                           phase="finished", review_status="approved", reasons=[],
                           started_at="2026-01-01T10:00:00Z", check_in_at="2026-01-01T12:00:00Z",
                           stop_at="2026-01-01T12:05:00Z", warning_revision=1)
        atomic_json(self.session / FILENAME, self.safety)
        self.config = {"recordings_root": str(self.session.parent), "local_naming": {"enabled": True}}
        self.volume = patch("quill_workflow.recording_identity.volume_identity",
                            return_value="macos-volume:11111111-2222-3333-4444-555555555555")
        self.probe = self.volume.start()
        self.addCleanup(self.volume.stop)
        self.queue = Queue(self.root / "state")
        self.addCleanup(lambda: self.queue.close())

    def enqueue(self):
        self.job = self.queue.enqueue(self.session, self.config)
        return self.job

    def saved(self):
        row = self.queue.db.execute("SELECT identity FROM recording_identities WHERE job=?", (self.job,)).fetchone()
        return json.loads(row[0]) if row else None

    def stored_device(self):
        return self.queue.db.execute("SELECT device FROM recording_safety_bindings WHERE job=?", (self.job,)).fetchone()[0]

    def simulate_device_renumber(self):
        # The real temporary filesystem is untouched. Its old stored device
        # number represents the previous mount, with all other identity fixed.
        self.old_device = self.session.stat().st_dev + 1000
        with self.queue.db:
            for table in ("recording_safety_bindings", "local_names"):
                self.queue.db.execute("UPDATE " + table + " SET device=? WHERE job=?", (self.old_device, self.job))

    def test_new_identity_records_volume_session_inode_and_manifest(self):
        self.enqueue()
        saved = self.saved()
        self.assertEqual(saved["session_id"], self.safety["session_id"])
        self.assertEqual(saved["inode"], self.session.stat().st_ino)
        self.assertEqual(saved["volume"], self.probe.return_value)
        self.assertEqual(len(saved["manifest_sha256"]), 64)
        self.assertEqual((self.session / FILENAME).read_text(), json.dumps(self.safety, indent=2) + "\n")

    def test_verified_device_renumber_survives_queue_restart_and_updates_both_bindings(self):
        self.enqueue()
        original_tasks = self.queue.tasks(self.job)
        self.simulate_device_renumber()
        self.queue.close()
        self.queue = Queue(self.root / "state")
        self.assertEqual(self.queue.require_processing(self.job), self.safety)
        self.assertEqual(self.stored_device(), self.session.stat().st_dev)
        self.assertEqual(self.queue.db.execute("SELECT device FROM local_names WHERE job=?", (self.job,)).fetchone()[0],
                         self.session.stat().st_dev)
        self.assertEqual(self.queue.tasks(self.job), original_tasks)
        with patch("quill_workflow.recording_identity._verify_files", side_effect=AssertionError("Repeated media hashing")):
            self.queue.require_processing(self.job)

    def test_named_destination_and_original_hook_keep_same_job_after_device_change(self):
        self.enqueue()
        self.simulate_device_renumber()
        old_path = self.session
        self.session = self.session.with_name("Synthetic title - 1-1-26")
        old_path.rename(self.session)
        with self.queue.db:
            self.queue.db.execute("UPDATE local_names SET destination=?,status='succeeded' WHERE job=?",
                                  (str(self.session), self.job))
        self.assertEqual(session_path(self.queue, self.job), self.session)
        self.assertEqual(self.queue.enqueue(old_path, self.config), self.job)
        self.assertEqual(self.queue.enqueue(self.session, self.config), self.job)
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_saved_alias_recovers_interrupted_rename_after_device_change(self):
        self.enqueue()
        self.simulate_device_renumber()
        alias = self.session.with_name("Previous title")
        self.session.rename(alias)
        with self.queue.db:
            self.queue.db.execute("INSERT INTO local_name_aliases VALUES (?,?)", (self.job, str(alias)))
        self.assertEqual(session_path(self.queue, self.job), alias)
        self.assertEqual(self.queue.require_processing(self.job), self.safety)

    def test_legacy_binding_upgrades_only_while_original_device_matches(self):
        self.enqueue()
        with self.queue.db:
            self.queue.db.execute("DELETE FROM recording_identities WHERE job=?", (self.job,))
        self.queue.require_processing(self.job)
        self.assertIsNotNone(self.saved())
        self.simulate_device_renumber()
        self.assertEqual(self.queue.require_processing(self.job), self.safety)

    def test_already_mismatched_legacy_binding_never_bootstraps_from_current_folder(self):
        self.enqueue()
        with self.queue.db:
            self.queue.db.execute("DELETE FROM recording_identities WHERE job=?", (self.job,))
        self.simulate_device_renumber()
        with self.assertRaisesRegex(RecordingSafetyHold, "without a saved volume identity"):
            self.queue.require_processing(self.job)
        self.assertIsNone(self.saved())
        self.assertEqual(self.stored_device(), self.old_device)

    def test_missing_volume_identity_preserves_normal_processing_but_fails_closed_on_change(self):
        self.probe.return_value = None
        self.enqueue()
        self.assertIsNone(self.saved())
        self.assertEqual(self.queue.require_processing(self.job), self.safety)
        self.simulate_device_renumber()
        with self.assertRaises(RecordingSafetyHold):
            self.queue.require_processing(self.job)

    def test_different_volume_cannot_reuse_same_inode_session_and_content(self):
        self.enqueue()
        self.simulate_device_renumber()
        self.probe.return_value = "macos-volume:99999999-2222-3333-4444-555555555555"
        with self.assertRaisesRegex(RecordingSafetyHold, "volume or folder changed"):
            self.queue.require_processing(self.job)
        self.assertEqual(self.stored_device(), self.old_device)

    def test_different_volume_cannot_reuse_unchanged_device_and_inode_numbers(self):
        self.enqueue()
        device = self.stored_device()
        self.probe.return_value = "macos-volume:99999999-2222-3333-4444-555555555555"
        with self.assertRaisesRegex(RecordingSafetyHold, "volume or folder changed"):
            self.queue.require_processing(self.job)
        self.assertEqual(self.stored_device(), device)

    def test_birthtime_guard_applies_even_when_device_and_inode_are_unchanged(self):
        self.enqueue()
        saved = self.saved()
        saved["birthtime"] = -1
        with self.queue.db:
            self.queue.db.execute("UPDATE recording_identities SET identity=? WHERE job=?",
                                  (json.dumps(saved), self.job))
        with self.assertRaisesRegex(RecordingSafetyHold, "volume or folder changed"):
            self.queue.require_processing(self.job)

    def test_named_folder_resolution_does_not_skip_same_device_volume_guard(self):
        self.enqueue()
        old_path = self.session
        self.session = self.session.with_name("Synthetic named destination")
        old_path.rename(self.session)
        with self.queue.db:
            self.queue.db.execute("UPDATE local_names SET destination=?,status='succeeded' WHERE job=?",
                                  (str(self.session), self.job))
        self.probe.return_value = "macos-volume:99999999-2222-3333-4444-555555555555"
        with self.assertRaisesRegex(RecordingSafetyHold, "volume or folder changed"):
            self.queue.require_processing(self.job)

    def test_replacement_folder_with_identical_content_and_journal_is_rejected(self):
        self.enqueue()
        self.simulate_device_renumber()
        original = self.session.with_name("original")
        self.session.rename(original)
        shutil.copytree(original, self.session)
        with self.assertRaisesRegex(RecordingSafetyHold, "replaced"):
            self.queue.require_processing(self.job)
        self.assertEqual(self.stored_device(), self.old_device)

    def test_edited_recording_cannot_be_rebound(self):
        self.enqueue()
        self.simulate_device_renumber()
        member = self.session / "mic.caf"
        member.write_bytes(b"X" * member.stat().st_size)
        with self.assertRaisesRegex(RecordingSafetyHold, "changed since completion"):
            self.queue.require_processing(self.job)
        self.assertEqual(self.stored_device(), self.old_device)

    def test_changed_saved_manifest_cannot_authorize_edited_source(self):
        self.enqueue()
        self.simulate_device_renumber()
        manifest_path = self.queue.state / "recordings" / self.job / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["mic.caf"]["sha256"] = "0" * 64
        atomic_json(manifest_path, manifest)
        with self.assertRaisesRegex(RecordingSafetyHold, "manifest changed"):
            self.queue.require_processing(self.job)
        self.assertEqual(self.stored_device(), self.old_device)

    def test_missing_manifest_becomes_a_recording_hold(self):
        self.enqueue()
        self.simulate_device_renumber()
        (self.queue.state / "recordings" / self.job / "manifest.json").unlink()
        with self.assertRaises(RecordingSafetyHold):
            self.queue.require_processing(self.job)

    def test_source_member_symlink_is_rejected_even_with_matching_bytes(self):
        self.enqueue()
        self.simulate_device_renumber()
        member = self.session / "mic.caf"
        outside = self.root / "same-bytes.caf"
        member.rename(outside)
        member.symlink_to(outside)
        with self.assertRaises(RecordingSafetyHold):
            self.queue.require_processing(self.job)
        self.assertEqual(self.stored_device(), self.old_device)

    def test_directory_symlink_is_rejected(self):
        self.enqueue()
        self.simulate_device_renumber()
        original = self.session.with_name("original")
        self.session.rename(original)
        self.session.symlink_to(original, target_is_directory=True)
        with self.assertRaises(RecordingSafetyHold):
            self.queue.require_processing(self.job)

    def test_changed_session_uuid_or_revoked_approval_never_rebinds(self):
        self.enqueue()
        self.simulate_device_renumber()
        for changes in ({"session_id": str(uuid.uuid4())}, {"review_status": "pending"},
                        {"review_status": "kept_local"}, {"phase": "active", "review_status": "pending"}):
            atomic_json(self.session / FILENAME, dict(self.safety, **changes))
            with self.subTest(changes=changes), self.assertRaises(RecordingSafetyHold):
                self.queue.require_processing(self.job)
            self.assertEqual(self.stored_device(), self.old_device)

    def test_birthtime_mismatch_and_corrupt_saved_identity_fail_closed(self):
        self.enqueue()
        self.simulate_device_renumber()
        original = self.saved()
        for saved in (dict(original, birthtime=-1), dict(original, version=True), [], None, "bad"):
            with self.queue.db:
                self.queue.db.execute("UPDATE recording_identities SET identity=? WHERE job=?",
                                      (json.dumps(saved), self.job))
            with self.subTest(saved=saved), self.assertRaises(RecordingSafetyHold):
                self.queue.require_processing(self.job)
            self.assertEqual(self.stored_device(), self.old_device)


class VolumeIdentityTests(unittest.TestCase):
    def test_macos_probe_uses_filesystem_uuid_bytes(self):
        value = "A6CEB834-056E-434D-AB62-60650D10D298"
        with patch("quill_workflow.recording_identity.sys.platform", "darwin"), patch(
                "quill_workflow.recording_identity._volume_uuid_bytes", return_value=uuid.UUID(value).bytes) as probe:
            self.assertEqual(volume_identity(Path("/synthetic/recording")), "macos-volume:" + value.lower())
            probe.assert_called_once_with(Path("/synthetic/recording"))

    def test_probe_failure_invalid_or_null_uuid_is_not_an_identity(self):
        with patch("quill_workflow.recording_identity.sys.platform", "darwin"), patch(
                "quill_workflow.recording_identity._volume_uuid_bytes") as probe:
            for data in (b"disk3s5", b"\0" * 16, b"", None):
                probe.return_value = data
                with self.subTest(data=data):
                    self.assertIsNone(volume_identity(Path("/synthetic")))
            probe.side_effect = OSError("Unsupported filesystem")
            self.assertIsNone(volume_identity(Path("/synthetic")))

    @unittest.skipUnless(sys.platform == "darwin", "Darwin getattrlist contract")
    def test_real_macos_volume_probe_on_synthetic_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            identity = volume_identity(Path(folder))
            self.assertIsNotNone(identity)
            self.assertEqual(identity, volume_identity(Path(folder).parent))


if __name__ == "__main__":
    unittest.main()
