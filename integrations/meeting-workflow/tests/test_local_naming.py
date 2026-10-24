"""Local naming checks use temporary synthetic files, never a live recording."""
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from date_fixtures import process_timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quill_workflow.drive_upload import _folder_name
from quill_workflow.local_naming import name_pending, name_session, request_naming
from quill_workflow import local_naming
from quill_workflow.pipeline import Queue, WorkflowError, atomic_json, render_notes
from quill_workflow.recording_safety import RecordingSafetyHold

spec = importlib.util.spec_from_file_location("local_naming_workflow", ROOT / "workflow.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


class LocalNamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.recordings = self.root / "recordings"
        self.session = self.recordings / "2026.09.14-0930"
        self.session.mkdir(parents=True)
        self.started = "2026-09-14T16:30:00Z"
        for filename in ("mic.caf", "system.caf", "screen.mp4"):
            (self.session / filename).write_bytes(("synthetic bytes: " + filename).encode())
        atomic_json(self.session / "meta.json", {
            "started": self.started, "ended": "2026-09-14T16:31:00Z", "duration_seconds": 60,
            "files": {"mic": "mic.caf", "system": "system.caf", "screen": "screen.mp4"},
            "transcription_requested": True,
        })
        atomic_json(self.session / "transcript.json", {
            "segments": [{"start_ms": 0, "end_ms": 3000, "speaker": "me",
                          "text": "Alex here. We will revise the portal dashboard."}]})
        (self.session / "transcript.md").write_text("Synthetic portal discussion.\n")
        (self.session / "transcribe.log").write_text("Synthetic completed transcription.\n")
        self.config = {"recordings_root": str(self.recordings), "local_naming": {"enabled": True}}
        self.queue = Queue(self.root / "state")
        self.title = "Portal dashboard review - Alex"

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def file_bytes(self, directory):
        return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}

    def row(self, job):
        row = self.queue.db.execute("SELECT * FROM local_names WHERE job=?", (job,)).fetchone()
        return dict(row) if row else None

    def prepare(self, config=None, title=True):
        job = self.queue.enqueue(self.session, self.config if config is None else config)
        source = self.queue.state / "recordings" / job / "transcript.json"
        result = self.queue.state / "results" / job
        notes_dir = result / "notes"
        notes_dir.mkdir(parents=True)
        notes = {"schema_version": 1,
                 "summary": [{"text": "Revise the portal dashboard.", "evidence": [0]}]}
        if title:
            notes["meeting_title"] = {"text": "Portal dashboard review", "evidence": [0],
                                      "participants": [{"name": "Alex", "evidence": [0]}]}
        settings = json.loads(next(task for task in self.queue.tasks(job)
                                   if task["stage"] == "notes")["settings"])
        receipt = {"job": job, "stage": "notes", "idempotency_key": job + ":notes",
                   "settings_sha256": hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest(),
                   "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        atomic_json(notes_dir / "notes.json", notes)
        atomic_json(notes_dir / "provenance.json", receipt)
        atomic_json(result / "notes-receipt.json", receipt)
        (notes_dir / "source-transcript.json").write_bytes(source.read_bytes())
        (notes_dir / "notes.md").write_text(render_notes(notes, json.loads(source.read_bytes())))
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='succeeded',receipt=? WHERE job=? AND stage='notes'",
                                  (json.dumps(receipt), job))
        return job

    def expected(self, job):
        return self.recordings / _folder_name(datetime.fromisoformat(self.started.replace("Z", "+00:00")),
                                             job, self.title).replace("/", "-")

    def test_short_local_date_has_no_time_or_path_separators(self):
        job = self.prepare()
        destination = name_session(self.queue, job)
        date = datetime.fromisoformat(self.started.replace("Z", "+00:00")).astimezone()
        self.assertEqual(destination.name, f"{self.title} - {date.month}-{date.day}-{date:%y}")
        self.assertEqual(destination.parent, self.recordings)

    def test_saved_recording_date_survives_travel_before_naming_and_refresh(self):
        metadata = json.loads((self.session / "meta.json").read_text())
        metadata.update(recording_timezone="Asia/Singapore", recording_utc_offset_seconds=28800)
        atomic_json(self.session / "meta.json", metadata)
        # This start is September 15 in Singapore, September 14 in Los Angeles.
        with process_timezone("America/Los_Angeles"):
            job = self.prepare()
            destination = name_session(self.queue, job)
            self.assertEqual(destination.name, self.title + " - 9-15-26")
        original = self.file_bytes(destination)
        for zone in ("Pacific/Honolulu", "Asia/Tokyo"):
            with self.subTest(zone=zone), process_timezone(zone):
                self.assertEqual(name_session(self.queue, job, refresh=True), destination)
                self.assertEqual(self.queue.enqueue(destination, self.config), job)
                self.assertEqual(self.file_bytes(destination), original)
        self.assertEqual(json.loads(self.row(job)["settings"])["meeting_timezone"], "Asia/Singapore")

    def test_enabling_local_naming_later_uses_recorded_timezone(self):
        metadata = json.loads((self.session / "meta.json").read_text())
        metadata["recording_timezone"] = "Asia/Singapore"
        atomic_json(self.session / "meta.json", metadata)
        job = self.prepare(dict(self.config, local_naming={"enabled": False}))
        with process_timezone("America/Los_Angeles"), self.queue.db:
            request_naming(self.queue, job, self.config)
        self.assertEqual(json.loads(self.row(job)["settings"])["meeting_timezone"], "Asia/Singapore")
        self.assertEqual(name_session(self.queue, job).name, self.title + " - 9-15-26")

    def test_refresh_date_format_preserves_all_prior_hook_paths(self):
        job = self.prepare()
        old_name = "2026-09-14 09-30-00 PDT - " + self.title
        with patch("quill_workflow.drive_upload._folder_name", return_value=old_name):
            old = name_session(self.queue, job)
        before = self.file_bytes(old)
        current = name_session(self.queue, job, refresh=True)
        self.assertEqual(current, self.expected(job))
        self.assertFalse(old.exists())
        self.assertEqual(self.file_bytes(current), before)
        for path in (self.session, old, current):
            self.assertEqual(self.queue.enqueue(path, self.config), job)

    def test_interrupted_date_format_refresh_recovers_from_previous_named_path(self):
        job = self.prepare()
        old_name = "2026-09-14 09-30-00 PDT - " + self.title
        with patch("quill_workflow.drive_upload._folder_name", return_value=old_name):
            old = name_session(self.queue, job)
        with patch.object(local_naming, "_rename_exclusive", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                name_session(self.queue, job, refresh=True)
        self.assertTrue(old.is_dir())
        self.assertEqual(self.row(job)["status"], "planned")
        self.queue.close()
        self.queue = Queue(self.root / "state")
        current = name_session(self.queue, job)
        self.assertEqual(current, self.expected(job))
        for path in (self.session, old, current):
            self.assertEqual(self.queue.enqueue(path, self.config), job)

    def test_new_jobs_are_registered_only_when_naming_is_enabled(self):
        job = self.queue.enqueue(self.session, dict(self.config, local_naming={"enabled": False}))
        self.assertIsNone(self.row(job))
        self.assertEqual(self.queue.enqueue(self.session, self.config), job)
        self.assertIsNone(self.row(job), "Enabling naming must not silently opt old jobs in")
        with self.queue.db:
            request_naming(self.queue, job, self.config)
        self.assertIsNotNone(self.row(job))

    def test_renames_folder_to_drive_label_and_preserves_every_member(self):
        job = self.prepare()
        before = self.file_bytes(self.session)
        identity = self.session.stat()
        tasks = self.queue.tasks(job)
        snapshot = self.file_bytes(self.queue.state / "recordings" / job)
        destination = name_session(self.queue, job)
        self.assertEqual(destination, self.expected(job))
        self.assertTrue(destination.is_dir())
        self.assertFalse(self.session.exists())
        self.assertEqual(self.file_bytes(destination), before)
        self.assertEqual((destination.stat().st_dev, destination.stat().st_ino),
                         (identity.st_dev, identity.st_ino))
        self.assertEqual(self.queue.tasks(job), tasks)
        self.assertEqual(self.file_bytes(self.queue.state / "recordings" / job), snapshot)
        self.assertEqual(self.queue.db.execute("SELECT session FROM jobs WHERE id=?", (job,)).fetchone()[0],
                         str(self.session))

    def test_original_and_named_paths_both_deduplicate_after_reopen(self):
        job = self.prepare()
        destination = name_session(self.queue, job)
        self.queue.close()
        self.queue = Queue(self.root / "state")
        self.assertEqual(self.queue.enqueue(self.session, self.config), job)
        self.assertEqual(self.queue.enqueue(destination, self.config), job)
        self.assertEqual(name_session(self.queue, job), destination)
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_parent_path_alias_does_not_lose_original_hook_identity(self):
        alias = self.root / "recordings-alias"
        alias.symlink_to(self.recordings, target_is_directory=True)
        job = self.prepare()
        name_session(self.queue, job)
        self.assertEqual(self.queue.enqueue(alias / self.session.name, self.config), job)

    def test_interruption_after_rename_recovers_saved_destination_and_inode(self):
        job = self.prepare()
        expected = self.expected(job)
        identity = self.session.stat()
        original_rename = local_naming._rename_exclusive

        def interrupted(source, destination):
            original_rename(source, destination)
            raise OSError("synthetic interruption after filesystem rename")

        with patch.object(local_naming, "_rename_exclusive", side_effect=interrupted):
            with self.assertRaises(OSError):
                name_session(self.queue, job)
        self.assertFalse(self.session.exists())
        self.assertTrue(expected.is_dir())
        self.assertEqual(self.row(job)["status"], "planned")
        self.queue.close()
        self.queue = Queue(self.root / "state")
        with patch.object(local_naming, "_rename_exclusive", side_effect=AssertionError("Already renamed")):
            self.assertEqual(name_session(self.queue, job), expected)
        self.assertEqual(self.row(job)["status"], "succeeded")
        self.assertEqual(expected.stat().st_ino, identity.st_ino)
        self.assertEqual(self.queue.enqueue(self.session, self.config), job)

    def test_destination_created_during_rename_is_not_replaced(self):
        job = self.prepare()
        original_rename = local_naming._rename_exclusive

        def collision(source, destination):
            destination.mkdir()
            (destination / "keep.txt").write_text("unrelated racing folder")
            original_rename(source, destination)

        with patch.object(local_naming, "_rename_exclusive", side_effect=collision):
            with self.assertRaises(OSError):
                name_session(self.queue, job)
        self.assertTrue(self.session.is_dir())
        self.assertEqual((self.expected(job) / "keep.txt").read_text(), "unrelated racing folder")
        with self.assertRaisesRegex(RecordingSafetyHold, 'Original recording location needs review'):
            name_session(self.queue, job)
        self.assertTrue(self.session.is_dir())
        self.assertEqual((self.expected(job) / "keep.txt").read_text(), "unrelated racing folder")

    def test_unrelated_name_collision_gets_stable_suffix_without_overwrite(self):
        job = self.prepare()
        occupied = self.expected(job)
        occupied.mkdir()
        (occupied / "keep.txt").write_text("unrelated content")
        destination = name_session(self.queue, job)
        self.assertEqual(destination, self.recordings / (occupied.name + " - " + job[:8]))
        self.assertEqual((occupied / "keep.txt").read_text(), "unrelated content")
        self.assertEqual(name_session(self.queue, job), destination)

    def test_both_collision_paths_are_never_overwritten(self):
        job = self.prepare()
        occupied = self.expected(job)
        fallback = self.recordings / (occupied.name + " - " + job[:8])
        for directory in (occupied, fallback):
            directory.mkdir()
            (directory / "keep.txt").write_text("unrelated content")
        with self.assertRaises(WorkflowError):
            name_session(self.queue, job)
        self.assertTrue(self.session.is_dir())
        self.assertEqual((fallback / "keep.txt").read_text(), "unrelated content")

    def test_historical_notes_without_title_are_skipped(self):
        job = self.prepare(title=False)
        self.assertIsNone(name_session(self.queue, job))
        self.assertEqual(self.row(job)["status"], "skipped")
        self.assertTrue(self.session.is_dir())

    def test_held_or_running_notes_cannot_rename_a_recording(self):
        job = self.prepare()
        for status in ("held", "running", "pending", "retry", "failed"):
            with self.subTest(status=status):
                with self.queue.db:
                    self.queue.db.execute("UPDATE tasks SET status=? WHERE job=? AND stage='notes'", (status, job))
                with self.assertRaises(WorkflowError):
                    name_session(self.queue, job)
                self.assertTrue(self.session.is_dir())

    def test_notes_provenance_and_exact_source_are_required(self):
        job = self.prepare()
        result = self.queue.state / "results" / job
        paths = (result / "notes-receipt.json", result / "notes/provenance.json",
                 result / "notes/source-transcript.json")
        for path in paths:
            original = path.read_bytes()
            with self.subTest(path=path.name):
                changed = json.loads(original)
                if path.name == "source-transcript.json":
                    changed["segments"][0]["text"] = "Unrelated transcript."
                else:
                    changed["input_sha256"] = "not-the-source"
                atomic_json(path, changed)
                with self.assertRaises(WorkflowError):
                    name_session(self.queue, job)
                self.assertTrue(self.session.is_dir())
            path.write_bytes(original)

    def test_unevidenced_participant_is_not_used_in_local_name(self):
        job = self.prepare()
        path = self.queue.state / "results" / job / "notes/notes.json"
        value = json.loads(path.read_bytes())
        value["meeting_title"]["participants"][0]["name"] = "Invented participant"
        atomic_json(path, value)
        with self.assertRaises(WorkflowError):
            name_session(self.queue, job)
        self.assertTrue(self.session.is_dir())

    def test_changed_media_or_unfinished_metadata_are_rejected(self):
        job = self.prepare()
        for filename in ("mic.caf", "meta.json"):
            path = self.session / filename
            original = path.read_bytes()
            with self.subTest(filename=filename):
                if filename == "meta.json":
                    value = json.loads(original)
                    del value["ended"]
                    atomic_json(path, value)
                else:
                    path.write_bytes(b"replacement synthetic media")
                with self.assertRaises(WorkflowError):
                    name_session(self.queue, job)
                self.assertTrue(self.session.is_dir())
            path.write_bytes(original)

    def test_missing_original_is_not_restored_from_recovery_snapshot(self):
        job = self.prepare()
        moved = self.root / "synthetic-trash"
        self.session.rename(moved)
        with self.assertRaises(WorkflowError):
            name_session(self.queue, job)
        self.assertFalse(self.session.exists())
        self.assertFalse(self.expected(job).exists())
        self.assertTrue(moved.is_dir())

    def test_linked_original_or_track_is_rejected(self):
        job = self.prepare()
        path = self.session / "mic.caf"
        original = path.read_bytes()
        path.unlink()
        path.symlink_to(self.session / "system.caf")
        with self.assertRaises(WorkflowError):
            name_session(self.queue, job)
        path.unlink()
        path.write_bytes(original)
        moved = self.root / "moved"
        self.session.rename(moved)
        self.session.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(WorkflowError):
            name_session(self.queue, job)
        self.assertTrue(self.session.is_symlink())

    def test_old_path_recreated_after_rename_is_not_mistaken_for_saved_job(self):
        job = self.prepare()
        destination = name_session(self.queue, job)
        shutil.copytree(destination, self.session)
        with self.assertRaises(WorkflowError):
            self.queue.enqueue(self.session, self.config)
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_distinct_new_recording_can_reuse_original_timestamp(self):
        job = self.prepare()
        destination = name_session(self.queue, job)
        shutil.copytree(destination, self.session)
        (self.session / "mic.caf").write_bytes(b"separate new synthetic recording")
        metadata = json.loads((self.session / "meta.json").read_bytes())
        metadata["started"] = "2026-09-14T16:30:45Z"
        metadata["ended"] = "2026-09-14T16:31:45Z"
        atomic_json(self.session / "meta.json", metadata)
        later = self.queue.enqueue(self.session, self.config)
        self.assertNotEqual(later, job)
        self.assertEqual(self.queue.enqueue(self.session, self.config), later)
        self.assertEqual(self.queue.enqueue(destination, self.config), job)
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)

    def test_removed_named_recording_is_not_recreated_or_requeued(self):
        job = self.prepare()
        destination = name_session(self.queue, job)
        destination.rename(self.root / "synthetic-trash")
        for path in (self.session, destination):
            with self.subTest(path=path.name):
                with self.assertRaises(WorkflowError):
                    self.queue.enqueue(path, self.config)
        with self.assertRaises(WorkflowError):
            name_session(self.queue, job)
        self.assertFalse(self.session.exists())
        self.assertFalse(destination.exists())
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_replaced_named_directory_does_not_resolve_as_original_job(self):
        job = self.prepare()
        destination = name_session(self.queue, job)
        held = self.root / "preserved-original"
        destination.rename(held)
        shutil.copytree(held, destination)
        with self.assertRaises(WorkflowError):
            self.queue.enqueue(destination, self.config)
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_pending_error_does_not_change_stage_status_and_can_be_retried(self):
        job = self.prepare()
        tasks = self.queue.tasks(job)
        path = self.session / "mic.caf"
        original = path.read_bytes()
        path.write_bytes(b"modified fixture")
        name_pending(self.queue, self.config)
        self.assertTrue(self.row(job)["error"])
        self.assertEqual(self.queue.tasks(job), tasks)
        path.write_bytes(original)
        name_pending(self.queue, self.config)
        self.assertTrue(self.expected(job).is_dir())
        self.assertFalse(self.row(job)["error"])
        self.assertEqual(self.queue.tasks(job), tasks)

    def test_disabling_naming_pauses_registered_jobs(self):
        job = self.prepare()
        name_pending(self.queue, dict(self.config, local_naming={"enabled": False}))
        self.assertTrue(self.session.is_dir())
        name_pending(self.queue, self.config)
        self.assertTrue(self.expected(job).is_dir())

    def test_old_successful_job_needs_explicit_registration(self):
        job = self.prepare(dict(self.config, local_naming={"enabled": False}))
        name_pending(self.queue, self.config)
        self.assertTrue(self.session.is_dir())
        self.assertIsNone(self.row(job))
        with self.queue.db:
            request_naming(self.queue, job, self.config)
        name_pending(self.queue, self.config)
        self.assertTrue(self.expected(job).is_dir())

    def test_worker_names_registered_completed_meeting_without_running_connectors(self):
        job = self.prepare()
        tasks = self.queue.tasks(job)
        config_path = self.root / "workflow-config.json"
        atomic_json(config_path, self.config)
        with patch.object(workflow.ConnectorRunner, "__call__", side_effect=AssertionError("Unexpected connector")):
            result = workflow.main(["--state", str(self.queue.state), "--config", str(config_path),
                                    "work", "--once"])
        self.assertEqual(result, 0)
        self.assertFalse(self.session.exists())
        self.assertTrue(self.expected(job).is_dir())
        self.assertEqual(self.queue.tasks(job), tasks)
        self.assertEqual(self.row(job)["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
