"""Recovered-session workflow contracts; only temporary synthetic bytes are used.

Native checks own PCM decoding and crash reconstruction. These checks start at
that boundary and never launch a model, subprocess, capture device or connector.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.drive_upload import DriveUploader
from quill_workflow.pipeline import ConnectorRunner, Queue, WorkflowError, atomic_json
from quill_workflow.recording_safety import FILENAME, RecordingSafetyHold


RECOVERY_REASON = "Recording recovered after an unexpected interruption. Review before processing."


class CrashRecoveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.session = self.root / "recordings/recovered-synthetic-session"
        self.session.mkdir(parents=True)
        self.session_id = str(uuid.uuid4())
        self.metadata = {
            "started": "2026-10-24T02:00:00Z", "ended": "2026-10-24T02:01:15Z",
            "recording_timezone": "Asia/Singapore", "recording_utc_offset_seconds": 28800,
            "duration_seconds": 55, "wall_duration_seconds": 75, "paused_duration_seconds": 20,
            "files": {"mic": "mic.caf", "system": "system.caf"},
            "start_offset_ms": {"mic": 120, "system": 0},
            "transcription_requested": True, "speaker_identification_requested": True,
            "screen_recording_requested": True, "screen_recording_started": True,
            "screen_error": "Screen video was not finalized before the interruption. Original files were preserved.",
            "capture_storage_format": "pcm_s16",
            "capture_recovery": {"status": "interrupted", "session_id": self.session_id,
                                 "snapshot_ended": "2026-10-24T02:01:14Z"},
            "mic_capture": {"coverage_complete": False, "status": "incomplete"},
            "system_capture": {"coverage_complete": False, "status": "incomplete"},
        }
        for name in ("mic.caf", "system.caf"):
            (self.session / name).write_bytes(b"Synthetic workflow bytes, not decodable audio: " + name.encode())
        atomic_json(self.session / "meta.json", self.metadata)
        self.write_review("pending")
        self.config = {"recordings_root": str(self.session.parent)}
        for stage in ("notes", "archive", "publish"):
            self.config[stage] = {"enabled": True, "command": ["synthetic-connector-never-executed"]}
        self.queue = Queue(self.root / "state")

    def tearDown(self):
        self.queue.close()
        self.temporary.cleanup()

    def write_review(self, status, phase="finished"):
        # Represents a native-app review decision; the Python worker must never
        # write or infer this approval from the presence of recovered metadata.
        atomic_json(self.session / FILENAME, {
            "schema_version": 1, "session_id": self.session_id, "owner": "manual",
            "phase": phase, "review_status": status, "warning_revision": 1,
            "started_at": "2026-10-24T02:00:00Z", "check_in_at": "2026-10-24T04:00:00Z",
            "stop_at": "2026-10-24T04:05:00Z", "reasons": ["Prior capture interruption", RECOVERY_REASON],
        })

    def write_transcript(self):
        # Represents completed native transcription after explicit approval.
        atomic_json(self.session / "transcript.json", {
            "segments": [{"speaker": "mic_speaker_1", "source": "mic", "start_ms": 120,
                          "end_ms": 2000, "text": "Synthetic recovery test."}],
            "capture_quality": {"status": "incomplete", "affected_sources": ["mic", "system"]},
        })
        (self.session / "transcript.md").write_text("Synthetic recovered transcript.\n", encoding="utf-8")

    def approved_job(self):
        self.write_review("approved")
        self.write_transcript()
        return self.queue.enqueue(self.session, self.config)

    def reopen_queue(self):
        state = self.queue.state
        self.queue.close()
        self.queue = Queue(state)

    def test_recovery_marker_never_authorizes_pending_active_or_kept_local_ingestion(self):
        for phase, status in (("finished", "pending"), ("active", "pending"), ("finished", "kept_local")):
            with self.subTest(phase=phase, status=status):
                self.write_review(status, phase)
                safety = (self.session / FILENAME).read_bytes()
                with patch("quill_workflow.pipeline.safe_member", side_effect=AssertionError("Media was inspected")):
                    with self.assertRaises(RecordingSafetyHold):
                        self.queue.enqueue(self.session, self.config)
                self.reopen_queue()
                self.queue.recover()
                self.assertEqual(self.queue.run_ready(lambda *args: self.fail("Held recovery dispatched work")), 0)
                self.assertEqual(self.queue.tasks(), [])
                self.assertFalse((self.queue.state / "recordings").exists())
                self.assertEqual((self.session / FILENAME).read_bytes(), safety)

    def test_approved_snapshot_preserves_recovery_timing_intents_and_excludes_unfinished_video(self):
        # Orphan video and checkpoint evidence remain local, even after the user
        # approves the successfully recovered audio. Only metadata-listed media
        # can enter the immutable completion snapshot.
        (self.session / "screen.mp4").write_bytes(b"Unfinalized synthetic video")
        parts = self.session / ".screen-parts-synthetic"
        parts.mkdir()
        (parts / "part-000.mp4").write_bytes(b"Preserved synthetic video segment")
        atomic_json(self.session / "in-progress.json", {"private_recovery_evidence": True})
        before = {path.relative_to(self.session): path.read_bytes()
                  for path in self.session.rglob("*") if path.is_file()}
        job = self.approved_job()
        snapshot = self.queue.state / "recordings" / job
        self.assertEqual({path.name for path in snapshot.iterdir()}, {
            "mic.caf", "system.caf", "meta.json", "transcript.json", "transcript.md", "manifest.json"})
        self.assertEqual(json.loads((snapshot / "meta.json").read_bytes()), self.metadata)
        for name in ("mic.caf", "system.caf", "meta.json"):
            self.assertEqual((snapshot / name).read_bytes(), before[Path(name)])
        for name in (Path("screen.mp4"), Path(".screen-parts-synthetic/part-000.mp4"), Path("in-progress.json")):
            self.assertEqual((self.session / name).read_bytes(), before[name])
        for task in self.queue.tasks(job):
            if task["stage"] in ("archive", "publish"):
                self.assertEqual(json.loads(task["settings"])["meeting_timezone"], "Asia/Singapore")

    def test_repeated_completion_and_worker_restart_reuse_one_job_and_receipts(self):
        job = self.approved_job()
        self.assertEqual(self.queue.enqueue(self.session, self.config), job)
        seen = []

        def receipt(stage, actual_job, settings):
            self.assertEqual(actual_job, job)
            seen.append(stage)
            return {"job": job, "stage": stage, "idempotency_key": job + ":" + stage}

        self.assertEqual(self.queue.run_ready(receipt), 3)
        self.reopen_queue()
        self.queue.recover()
        self.assertEqual(self.queue.enqueue(self.session, self.config), job)
        self.assertEqual(self.queue.run_ready(lambda *args: self.fail("Completed work repeated")), 0)
        self.assertEqual(seen, ["notes", "archive", "publish"])
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        self.assertTrue(all((task["status"], task["attempts"]) == ("succeeded", 1)
                            for task in self.queue.tasks(job)))

    def test_review_approval_does_not_enable_disabled_stages(self):
        for settings in (self.config[stage] for stage in ("notes", "archive", "publish")):
            settings["enabled"] = False
        job = self.approved_job()
        self.reopen_queue()
        self.queue.recover()
        self.assertEqual(self.queue.run_ready(lambda *args: self.fail("Approval enabled an external stage")), 0)
        self.assertTrue(all((task["status"], task["attempts"]) == ("held", 0)
                            for task in self.queue.tasks(job)))

    def test_reapplied_recovery_hold_blocks_connectors_cached_receipts_and_drive(self):
        job = self.approved_job()
        results = self.queue.state / "results" / job
        results.mkdir(parents=True)
        atomic_json(results / "archive-receipt.json", {"job": job, "stage": "archive"})
        runner = ConnectorRunner(self.queue.state, ROOT / "skills/call-summary/SKILL.md")
        runner.execute = Mock(side_effect=AssertionError("A process was launched"))
        invoke = Mock(side_effect=AssertionError("Drive was called"))
        uploader = DriveUploader(self.queue.state, invoke, "synthetic_root_123",
                                 guard=lambda: runner.require_processing(job))
        for status in ("pending", "kept_local"):
            with self.subTest(status=status):
                self.write_review(status)
                self.reopen_queue()
                self.queue.recover()
                self.assertEqual(self.queue.run_ready(lambda *args: self.fail("Recovery hold was released")), 0)
                for stage in ("notes", "archive", "publish"):
                    with self.assertRaises(RecordingSafetyHold):
                        runner(stage, job, self.config[stage])
                for operation in ("get_file_metadata", "create_folder", "upload_file", "import_document"):
                    with self.assertRaises(RecordingSafetyHold):
                        uploader._call(operation, {})
        runner.execute.assert_not_called()
        invoke.assert_not_called()

    def test_missing_or_linked_recovered_track_cannot_enter_a_snapshot(self):
        self.write_review("approved")
        mic = self.session / "mic.caf"
        mic.unlink()
        for linked in (False, True):
            with self.subTest(linked=linked):
                if linked:
                    mic.symlink_to(self.session / "system.caf")
                with self.assertRaises(WorkflowError):
                    self.queue.enqueue(self.session, self.config)
                self.assertEqual(self.queue.tasks(), [])
                self.assertFalse((self.queue.state / "recordings").exists())

    def test_recovery_fields_cannot_replace_finalized_metadata(self):
        self.write_review("approved")
        for field, value in (("ended", None), ("files", {"mic": "mic.caf"})):
            with self.subTest(field=field):
                metadata = copy.deepcopy(self.metadata)
                metadata[field] = value
                atomic_json(self.session / "meta.json", metadata)
                with self.assertRaises(RecordingSafetyHold):
                    self.queue.enqueue(self.session, self.config)
                self.assertEqual(self.queue.tasks(), [])

    def test_recovered_session_deletion_marker_blocks_later_completion(self):
        self.write_review("approved")
        atomic_json(self.session / ".quill-deleted.json", {"schema_version": 1, "job": "synthetic-deleted"})
        with self.assertRaisesRegex(WorkflowError, "deleted"):
            self.queue.enqueue(self.session, self.config)
        self.assertEqual(self.queue.tasks(), [])
        self.assertFalse((self.queue.state / "recordings").exists())


if __name__ == "__main__":
    unittest.main()
