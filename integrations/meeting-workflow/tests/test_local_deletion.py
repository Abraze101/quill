"""Deletion tests use disposable synthetic files and a fake local Trash only."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.local_deletion import LocalMeetingDeletion, deleted_jobs, job_lock, pending_deletions, LEASE, MARKER, CAPABILITY
from quill_workflow.pipeline import Queue, ConnectorRunner, WorkflowError, atomic_json
from quill_workflow.queue_status import meetings
from quill_workflow.recording_safety import RecordingSafetyHold
from quill_workflow.review_server import create_server, _selection
from quill_workflow.routing import RoutingError, RoutingReviews
from quill_workflow.sharing import SharingReviews


class LocalDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "Recordings" / "Synthetic planning - Alex - 10-21-26"
        self.source.mkdir(parents=True)
        for name in ("mic.caf", "system.caf", "screen.mp4"):
            (self.source / name).write_bytes(b"synthetic media " + name.encode())
        atomic_json(self.source / "meta.json", {"started": "2026-10-21T00:00:00Z", "ended": "2026-10-21T00:01:00Z",
                    "transcription_requested": True, "files": {"mic": "mic.caf", "system": "system.caf", "screen": "screen.mp4"}})
        self.transcript = {"segments": [{"text": "Synthetic planning", "start_ms": 0}]}
        atomic_json(self.source / "transcript.json", self.transcript)
        (self.source / "transcript.md").write_text("Synthetic transcript")
        self.config = {"recordings_root": str(self.source.parent)}
        self.config_path = self.root / "workflow.json"
        atomic_json(self.config_path, self.config)
        self.queue = Queue(self.root / "state")
        self.job = self.queue.enqueue(self.source, self.config)
        self.results = self.queue.state / "results" / self.job
        self.results.mkdir(parents=True)
        (self.results / "notes.md").write_text("Synthetic notes")
        self.receipt = {"job": self.job, "stage": "publish", "url": "https://docs.google.com/document/d/synthetic_document/edit"}
        atomic_json(self.results / "publish-receipt.json", self.receipt)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET receipt=?,status='succeeded' WHERE job=? AND stage='publish'",
                                  (json.dumps(self.receipt), self.job))
        self.trash = self.root / "Trash"
        self.trash.mkdir()
        self.moved = []
        self.deletion = LocalMeetingDeletion(self.queue.state, self.config_path, self.fake_trash)
        reviews = RoutingReviews(self.queue.state)
        try:
            self.review = reviews.propose(self.job, json.dumps(self.transcript).encode(),
                 [{"id": "one", "name": "One", "drive_folder_id": "folder_synthetic"}],
                 {"schema_version": 1, "company_id": "one", "confidence": "high", "reason": "Synthetic evidence",
                  "evidence": [0], "ambiguous": False})
        finally:
            reviews.close()

    def tearDown(self):
        self.deletion.close()
        self.queue.close()
        self.temp.cleanup()

    def fake_trash(self, source):
        destination = self.trash / source.name
        os.rename(source, destination)
        self.moved.append(destination)
        return str(destination)

    def prepared(self):
        item = next(item for item in self.deletion.options() if item["job"] == self.job)
        self.assertTrue(item["available"], item.get("detail"))
        return self.deletion.prepare(self.job, item["revision"])

    def confirm(self, item=None):
        item = item or self.prepared()
        return self.deletion.confirm(self.job, item["revision"], item["confirmation"])

    def test_prepare_does_not_move_cancel_or_upload_anything(self):
        before = self.queue.tasks(self.job)
        self.prepared()
        self.assertTrue(self.source.is_dir())
        self.assertEqual(self.queue.tasks(self.job), before)
        self.assertEqual(self.moved, [])
        self.assertEqual(deleted_jobs(self.queue.state), set())

    def test_entire_original_and_queue_copies_go_to_trash_receipts_stay_auditable(self):
        item = self.prepared()
        record = self.confirm(item)
        self.assertEqual(record["status"], "trashed")
        self.assertFalse(self.source.exists())
        self.assertFalse(self.results.exists())
        self.assertFalse((self.queue.state / "recordings" / self.job).exists())
        self.assertEqual(len(self.moved), 3)
        original = self.moved[0] / self.source.name
        self.assertEqual((original / "mic.caf").read_bytes(), b"synthetic media mic.caf")
        self.assertTrue((original / "transcript.json").is_file())
        self.assertTrue((original / MARKER).is_file())
        self.assertTrue((self.moved[2] / self.job / "notes.md").is_file())
        self.assertEqual({task["status"] for task in self.queue.tasks(self.job)}, {"deleted"})
        receipt = next(task["receipt"] for task in self.queue.tasks(self.job) if task["stage"] == "publish")
        self.assertEqual(json.loads(receipt), self.receipt)
        self.assertEqual(record["snapshot"]["tasks"][1]["job"], self.job)
        self.assertEqual(record["receipt_journals"][self.job + "/publish-receipt.json"], self.receipt)
        self.assertEqual(deleted_jobs(self.queue.state), {self.job})
        self.assertEqual(meetings(self.queue), [])
        self.assertEqual(self.deletion.options(), [])
        self.assertFalse(self.queue.has_work())
        self.assertEqual(self.confirm(item)["status"], "trashed")
        self.assertEqual(len(self.moved), 3)

    def test_confirm_requires_preparation_exact_nonce_and_unexpired_review(self):
        item = self.deletion.options()[0]
        with self.assertRaises(WorkflowError):
            self.deletion.confirm(self.job, item["revision"], "invented")
        item = self.prepared()
        with self.assertRaises(WorkflowError):
            self.deletion.confirm(self.job, item["revision"], "wrong")
        with patch("quill_workflow.local_deletion.time.time", return_value=10**12):
            with self.assertRaises(WorkflowError):
                self.confirm(item)
        self.assertEqual(self.moved, [])

    def test_changed_finished_capture_never_moves_files(self):
        item = self.prepared()
        (self.source / "mic.caf").write_text("Changed after review")
        with self.assertRaises(WorkflowError):
            self.confirm(item)
        self.assertTrue(self.source.exists())
        self.assertFalse(deleted_jobs(self.queue.state))

    def test_processing_progress_does_not_invalidate_exact_original_confirmation(self):
        item = self.prepared()
        (self.source / "transcript.md").write_text("Transcript finished after review")
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET attempts=attempts+1 WHERE job=?", (self.job,))
        self.assertEqual(self.confirm(item)["status"], "trashed")

    def test_local_symlinks_and_queue_symlinks_are_refused(self):
        target = self.root / "precious"
        target.write_text("Keep me")
        link = self.source / "linked-notes"
        link.symlink_to(target)
        item = self.deletion.options()[0]
        self.assertFalse(item["available"])
        link.unlink()
        saved = self.results.with_name(self.job + "-saved")
        self.results.rename(saved)
        self.results.symlink_to(saved, target_is_directory=True)
        self.assertFalse(self.deletion.options()[0]["available"])
        self.assertEqual(target.read_text(), "Keep me")
        self.assertEqual(self.moved, [])

    def test_changed_root_and_foreign_job_and_paths_are_refused(self):
        for job in ("../escape", "/tmp/other", "unknown"):
            with self.assertRaises((WorkflowError, OSError)):
                self.deletion.prepare(job, "revision")
        self.config["recordings_root"] = str(self.root)
        atomic_json(self.config_path, self.config)
        self.assertFalse(self.deletion.options()[0]["available"])
        self.assertTrue(self.source.exists())

    def test_active_transcription_or_unfinished_capture_is_unavailable(self):
        (self.source / "transcript.json").unlink()
        self.assertFalse(self.deletion.options()[0]["available"])
        atomic_json(self.source / "transcript.json", self.transcript)
        meta = json.loads((self.source / "meta.json").read_text())
        meta.pop("ended")
        atomic_json(self.source / "meta.json", meta)
        self.assertFalse(self.deletion.options()[0]["available"])
        self.assertEqual(self.moved, [])

    def test_new_lease_capable_transcription_failure_can_be_deleted_when_idle(self):
        (self.source / "transcript.json").unlink()
        self.job = self.queue.enqueue(self.source, self.config)
        atomic_json(self.source / CAPABILITY, {"schema_version": 1, "shared_processing_lease": True})
        item = self.prepared()
        with (self.source / LEASE).open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.assertEqual(self.confirm(item)["status"], "cancelling")
            self.assertTrue((self.source / MARKER).exists())
            self.assertEqual(self.moved, [])
        self.deletion.reconcile()
        self.assertEqual(self.deletion._get(self.job)["status"], "trashed")

    def test_capability_marker_must_be_exact_bounded_and_not_linked(self):
        for value in ({"schema_version": True, "shared_processing_lease": True},
                      {"schema_version": 1, "shared_processing_lease": 1},
                      {"schema_version": 1, "shared_processing_lease": True, "extra": "invalid"}):
            atomic_json(self.source / CAPABILITY, value)
            self.assertFalse(self.deletion.options()[0]["available"])
        (self.source / CAPABILITY).write_text("x" * 1001)
        self.assertFalse(self.deletion.options()[0]["available"])
        (self.source / CAPABILITY).unlink()
        (self.source / CAPABILITY).symlink_to(self.source / "meta.json")
        self.assertFalse(self.deletion.options()[0]["available"])
        self.assertEqual(self.moved, [])

    def test_explicitly_disabled_transcription_can_be_deleted_without_transcript(self):
        (self.source / "transcript.json").unlink()
        meta = json.loads((self.source / "meta.json").read_text())
        meta["transcription_requested"] = False
        atomic_json(self.source / "meta.json", meta)
        self.job = self.queue.enqueue(self.source, self.config)
        self.assertEqual(self.confirm()["status"], "trashed")

    def test_unrelated_global_worker_lock_does_not_prevent_delete(self):
        with (self.queue.state / "worker.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.confirm()["status"], "trashed")

    def test_same_job_writer_defers_trash_but_durably_cancels_retries(self):
        item = self.prepared()
        with job_lock(self.queue.state, self.job):
            self.assertEqual(self.confirm(item)["status"], "cancelling")
            self.assertTrue(pending_deletions(self.queue.state))
            self.assertEqual(self.moved, [])
            self.assertEqual({task["status"] for task in self.queue.tasks(self.job)}, {"deleted"})
            with self.assertRaises(RecordingSafetyHold):
                self.queue.require_processing(self.job)
        self.deletion.reconcile()
        self.assertEqual(self.deletion._get(self.job)["status"], "trashed")

    def test_replaced_legacy_source_is_not_bound_without_manifest_verification(self):
        (self.source / "mic.caf").write_bytes(b"A different recording at the old path")
        item = self.deletion.options()[0]
        with self.assertRaises((WorkflowError, RecordingSafetyHold)):
            self.deletion.prepare(self.job, item["revision"])
        self.assertEqual(self.moved, [])

    def test_nonprivate_or_hardlinked_session_lease_is_rejected(self):
        item = self.prepared()
        lease = self.source / LEASE
        lease.chmod(0o644)
        with self.assertRaises(WorkflowError):
            self.confirm(item)
        lease.chmod(0o600)
        os.link(lease, self.root / "other-lease-link")
        with self.assertRaises(WorkflowError):
            self.confirm(item)
        self.assertEqual(self.moved, [])

    def test_orphaned_running_queue_task_is_cancelled_without_processing(self):
        item = self.prepared()
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='running' WHERE job=? AND stage='notes'", (self.job,))
        self.assertEqual(self.confirm(item)["status"], "trashed")
        self.assertEqual({task["status"] for task in self.queue.tasks(self.job)}, {"deleted"})

    def test_interrupted_move_keeps_tombstone_and_can_resume_without_duplicate_moves(self):
        item = self.prepared()
        calls = 0
        def fail_second(source):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("Synthetic unavailable Trash")
            return self.fake_trash(source)
        self.deletion.trash = fail_second
        with self.assertRaises(WorkflowError):
            self.confirm(item)
        self.assertEqual(len(self.moved), 1)
        self.assertFalse(self.queue.has_work())
        self.assertEqual(deleted_jobs(self.queue.state), {self.job})
        self.assertTrue(self.deletion.options()[0]["interrupted"])
        self.deletion.trash = self.fake_trash
        retry = self.deletion.prepare(self.job, item["revision"])
        self.assertEqual(self.confirm(retry)["status"], "trashed")
        self.assertEqual(len(self.moved), 3)

    def test_restore_does_not_reenqueue_and_stale_routing_sharing_retry_are_blocked(self):
        self.confirm()
        original = self.moved[0] / self.source.name
        os.rename(original, self.source)
        with self.assertRaises(WorkflowError):
            self.queue.enqueue(self.source, self.config)
        with self.assertRaises(RecordingSafetyHold):
            self.queue.require_processing(self.job)
        with self.assertRaises((WorkflowError, RecordingSafetyHold)):
            self.queue.release(self.job, "notes", self.config)
        reviews = RoutingReviews(self.queue.state)
        try:
            self.assertEqual(reviews.pending(), [])
            with self.assertRaises(RoutingError):
                reviews.approve(self.job, "one", self.review["revision"])
        finally:
            reviews.close()
        sharing = SharingReviews(self.queue.state, self.config_path)
        try:
            sharing.put({"job": self.job, "status": "shared", "revision": "old", "receipt": self.receipt})
            self.assertEqual(sharing.options(), [])
            with self.assertRaises(WorkflowError):
                with sharing.locked(self.job):
                    self.fail("Deleted jobs cannot enter sharing work")
        finally:
            sharing.close()
        self.queue.recover()
        self.queue.run_ready(lambda *args: self.fail("Deleted job cannot invoke connector"))
        self.assertFalse(self.queue.has_work())

    def test_duplicate_queue_jobs_for_same_original_are_cancelled_together(self):
        original_job = self.job
        (self.source / "transcript.md").write_text("A later readable transcript version")
        self.job = self.queue.enqueue(self.source, self.config)
        self.assertNotEqual(original_job, self.job)
        self.confirm()
        self.assertEqual(deleted_jobs(self.queue.state), {original_job, self.job})
        self.assertFalse((self.queue.state / "recordings" / original_job).exists())
        self.assertFalse((self.queue.state / "recordings" / self.job).exists())

    def test_prequeue_empty_finalized_meeting_is_discarded_without_enqueuing(self):
        source = self.source.parent / "Accidental empty recording"
        source.mkdir()
        atomic_json(source / "meta.json", {"started": "2026-10-21T01:00:00Z", "ended": "2026-10-21T01:00:01Z",
                                           "transcription_requested": True})
        atomic_json(source / CAPABILITY, {"schema_version": 1, "shared_processing_lease": True})
        before = self.queue.tasks()
        item = next(item for item in self.deletion.options() if item["title"] == source.name)
        self.assertTrue(item["available"])
        prepared = self.deletion.prepare(item["job"], item["revision"])
        result = self.deletion.confirm(item["job"], item["revision"], prepared["confirmation"])
        self.assertEqual(result["status"], "trashed")
        self.assertEqual(self.queue.tasks(), before)
        self.assertFalse(source.exists())
        self.assertTrue(self.source.exists())

    def test_unmarked_prequeue_transcription_is_visible_but_disabled(self):
        source = self.source.parent / "Old unfinished processing"
        source.mkdir()
        atomic_json(source / "meta.json", {"started": "2026-10-21T01:00:00Z", "ended": "2026-10-21T01:00:01Z"})
        item = next(item for item in self.deletion.options() if item["title"] == source.name)
        self.assertFalse(item["available"])
        self.assertIn("transcription", item["detail"])

    def test_late_results_and_uncertain_drive_journals_survive_cancellation(self):
        item = self.prepared()
        self.results.rename(self.root / "prior-results")
        with job_lock(self.queue.state, self.job):
            result = self.confirm(item)
            self.assertEqual(result["status"], "cancelling")
            self.results.mkdir()
            journal = {"operation": {"state": "dispatched", "remote_id": "uncertain_remote_copy"}}
            atomic_json(self.results / "drive-publish.json", journal)
        self.deletion.reconcile()
        saved = self.deletion._get(self.job)
        self.assertEqual(saved["status"], "trashed")
        self.assertEqual(saved["receipt_journals"][self.job + "/drive-publish.json"], journal)

    def test_enqueuing_cannot_race_between_durable_request_and_marker_publication(self):
        with job_lock(self.queue.state, self.job):
            self.confirm()
            (self.source / MARKER).unlink()  # Simulate the pre-publication crash window.
            with self.assertRaises(WorkflowError):
                self.queue.enqueue(self.source, self.config)
        self.deletion.reconcile()
        self.assertEqual(self.deletion._get(self.job)["status"], "trashed")

    def test_owned_long_running_connector_stops_without_touching_another_process(self):
        item = self.prepared()
        started = self.root / "started"
        late = self.root / "should-not-be-written"
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='pending' WHERE job=? AND stage='notes'", (self.job,))
        errors = []
        def work():
            queue = Queue(self.queue.state)
            runner = ConnectorRunner(self.queue.state, self.root / "unused-skill")
            def call(stage, job, settings):
                runner.execute([sys.executable, "-c", "import os,time,pathlib; pathlib.Path(" + repr(str(started)) +
                    ").write_text(str(os.getpid())); time.sleep(30); pathlib.Path(" + repr(str(late)) +
                    ").write_text('must not run')"], self.root, 40, job=job)
                return {"job": job, "stage": stage}
            try:
                queue.run_ready(call)
            except BaseException as error:
                errors.append(error)
            finally:
                queue.close()
        other = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        thread = threading.Thread(target=work)
        try:
            thread.start()
            deadline = time.monotonic() + 5
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(started.exists())
            child_pid = int(started.read_text())
            self.assertEqual(self.confirm(item)["status"], "cancelling")
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            self.assertIsNone(other.poll())
            self.assertFalse(late.exists())
            self.assertEqual({task["status"] for task in self.queue.tasks(self.job)}, {"deleted"})
            self.deletion.reconcile()
            self.assertEqual(self.deletion._get(self.job)["status"], "trashed")
        finally:
            other.terminate()
            other.wait()
            thread.join(5)

    def test_child_inherits_job_lease_until_it_really_exits(self):
        from quill_workflow.processing_lease import child_lease_fds
        item = self.prepared()
        with job_lock(self.queue.state, self.job):
            child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], pass_fds=child_lease_fds())
        try:
            self.assertEqual(self.confirm(item)["status"], "cancelling")
            self.assertEqual(self.moved, [])
        finally:
            child.terminate()
            child.wait()
        self.deletion.close()
        self.deletion = LocalMeetingDeletion(self.queue.state, self.config_path, self.fake_trash)
        self.deletion.reconcile()
        self.assertEqual(self.deletion._get(self.job)["status"], "trashed")

    def test_delete_worker_resumes_durable_request_and_waits_for_owned_lease(self):
        spec = importlib.util.spec_from_file_location("deletion_test_workflow", ROOT / "workflow.py")
        workflow = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(workflow)
        item = self.prepared()
        handle = (self.source / LEASE).open("a")
        fcntl.flock(handle, fcntl.LOCK_SH)
        try:
            self.assertEqual(self.confirm(item)["status"], "cancelling")
            factory = lambda state, config: LocalMeetingDeletion(state, config, self.fake_trash)
            with patch("quill_workflow.local_deletion.LocalMeetingDeletion", side_effect=factory), \
                    patch.object(workflow.time, "sleep", side_effect=lambda seconds: handle.close()) as wait:
                self.assertEqual(workflow.main(["--config", str(self.config_path), "--state", str(self.queue.state),
                                               "delete-work"]), 0)
                wait.assert_called_once_with(0.5)
            self.assertFalse(pending_deletions(self.queue.state))
            self.assertEqual(self.deletion._get(self.job)["status"], "trashed")
        finally:
            handle.close()
        self.assertEqual({task["status"] for task in self.queue.tasks()}, {"deleted"})
        self.assertEqual(self.deletion.options(), [])

    def test_renamed_folder_uses_saved_destination_not_old_path(self):
        info = self.source.stat()
        destination = self.source.with_name("Named meeting - Alex - 10-21-26")
        self.source.rename(destination)
        with self.queue.db:
            self.queue.db.execute("INSERT INTO local_names(job,settings,destination,device,inode,status) VALUES (?,?,?,?,?,?)",
                                  (self.job, "{}", str(destination), info.st_dev, info.st_ino, "succeeded"))
        options = self.deletion.options()
        self.assertEqual([item["job"] for item in options], [self.job])
        self.assertEqual(self.confirm()["title"], destination.name)
        self.assertFalse(destination.exists())
        self.assertTrue((self.moved[0] / destination.name).is_dir())

    def test_invalid_historical_mapping_does_not_hide_valid_prequeue_or_bypass_identity(self):
        info = self.source.stat()
        with self.queue.db:
            self.queue.db.execute("INSERT INTO local_names(job,settings,destination,device,inode,status) VALUES (?,?,?,?,?,?)",
                                  (self.job, "{}", str(self.source), info.st_dev + 1, info.st_ino, "succeeded"))
        source = self.source.parent / "Valid accidental recording"
        source.mkdir()
        atomic_json(source / "meta.json", {"started": "2026-10-21T02:00:00Z", "ended": "2026-10-21T02:00:01Z",
                                           "transcription_requested": False})
        options = self.deletion.options()
        self.assertEqual(len(options), 2)
        bad = next(item for item in options if item["job"] == self.job)
        self.assertFalse(bad["available"])
        self.assertIn("device changed", bad["detail"])
        good = next(item for item in options if item["title"] == source.name)
        self.assertTrue(good["available"])
        self.deletion.prepare(good["job"], good["revision"])
        with self.assertRaises(WorkflowError):
            self.deletion.prepare(bad["job"], bad["revision"])
        alias = next(job for job, path in self.deletion._local_sources() if path == self.source)
        with self.assertRaises(WorkflowError):
            self.deletion.prepare(alias, "invented")
        self.assertEqual(self.moved, [])
        self.assertTrue(source.is_dir())
        self.assertTrue(self.source.is_dir())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class DeleteReviewUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.actions = []
        self.item = {"job": "synthetic-only", "revision": "exact", "available": True, "title": "Synthetic meeting"}
        def on_delete(action, job, revision, confirmation=None):
            self.actions.append((action, job, revision, confirmation))
            return dict(self.item, confirmation="nonce", folder="/Synthetic/Meeting")
        self.server = create_server(Path(self.temp.name).resolve(), deletion_options=lambda: [self.item], on_delete=on_delete)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.opener = build_opener(NoRedirect)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, fields=None, origin=True):
        request = Request(self.server.review_url + ("decision" if fields is not None else "?meeting=" + _selection(self.item["job"])),
                          data=urlencode(fields).encode() if fields is not None else None)
        if origin:
            request.add_header("Origin", self.server.review_origin)
        try:
            response = self.opener.open(request)
        except HTTPError as error:
            response = error
        return response.code, response.read().decode()

    def test_delete_is_separate_two_step_native_form_and_get_has_no_effect(self):
        code, body = self.request()
        self.assertEqual(code, 200)
        self.assertIn('value="delete_prepare"', body)
        self.assertNotIn('value="delete_confirm"', body)
        self.assertEqual(self.actions, [])
        code, body = self.request({"action": "delete_prepare", "job": "synthetic-only", "revision": "exact"})
        self.assertEqual(code, 200)
        self.assertIn("Move entire meeting to Trash", body)
        self.assertIn('name="confirmation" value="nonce"', body)
        self.assertIn("Existing Drive files", body)
        self.assertIn("Cancel and keep", body)
        code, _ = self.request({"action": "delete_confirm", "job": "synthetic-only", "revision": "exact", "confirmation": "nonce"})
        self.assertEqual(code, 303)
        self.assertEqual(self.actions[-1], ("delete_confirm", "synthetic-only", "exact", "nonce"))

    def test_no_untrusted_origin_extra_path_or_missing_confirmation_reaches_callback(self):
        base = {"action": "delete_prepare", "job": "synthetic-only", "revision": "exact"}
        self.assertEqual(self.request(base, origin=False)[0], 403)
        self.assertEqual(self.request(dict(base, path="/unrelated"))[0], 400)
        self.assertEqual(self.request(dict(base, action="delete_confirm"))[0], 400)
        self.assertEqual(self.actions, [])

    def test_running_item_has_no_delete_action(self):
        self.item.update(available=False, detail="Recording is active")
        _, body = self.request()
        self.assertNotIn('value="delete_prepare"', body)


if __name__ == "__main__":
    unittest.main()
