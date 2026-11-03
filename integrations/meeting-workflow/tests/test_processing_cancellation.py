"""Processing cancellation uses only synthetic local sessions and stub connectors."""
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.local_deletion import CAPABILITY, LEASE, LocalMeetingDeletion, deleted_jobs, stopped_jobs
from quill_workflow.pipeline import ConnectorRunner, Queue, WorkflowError, atomic_json
from quill_workflow.queue_status import meetings, retry_revision
from quill_workflow.recording_safety import CANCEL_MARKER, ProcessingCancelled
from quill_workflow.routing import RoutingError, RoutingReviews


class ProcessingCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.recordings = self.root / "recordings"
        self.recordings.mkdir()
        self.state = self.root / "state"
        self.config = {"recordings_root": str(self.recordings),
                       **{stage: {"enabled": True, "command": ["fixture"]} for stage in ("notes", "archive", "publish")}}
        self.config_file = self.root / "workflow.json"
        atomic_json(self.config_file, self.config)
        self.queue = Queue(self.state)
        self.addCleanup(self.queue.close)
        self.actions = LocalMeetingDeletion(self.state, self.config_file, trash=self.trash)
        self.addCleanup(self.actions.close)

    def trash(self, directory):
        destination = self.root / ("trashed-" + directory.name)
        os.rename(directory, destination)
        return str(destination)

    def source(self, name, transcript=True):
        path = self.recordings / name
        path.mkdir()
        for track in ("mic.caf", "system.caf"):
            (path / track).write_bytes((name + track).encode())
        atomic_json(path / CAPABILITY, {"schema_version": 1, "shared_processing_lease": True})
        atomic_json(path / "meta.json", {"started": "2026-10-21T00:00:00Z", "ended": "2026-10-21T00:01:00Z",
                    "transcription_requested": True, "files": {"mic": "mic.caf", "system": "system.caf"}})
        if transcript:
            atomic_json(path / "transcript.json", {"segments": [{"text": "Synthetic meeting", "start_ms": 0}]})
        return path

    def test_queued_cancel_preserves_receipts_sources_other_job_and_prevents_retry(self):
        source = self.source("First")
        next_source = self.source("Second")
        job = self.queue.enqueue(source, self.config)
        other = self.queue.enqueue(next_source, self.config)
        receipt = json.dumps({"job": job, "stage": "publish", "destination": "fixture"})
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='succeeded',receipt=? WHERE job=? AND stage='publish'", (receipt, job))
        before = {p.name: p.read_bytes() for p in source.iterdir()}
        untouched = self.queue.tasks(other)
        item = self.actions.cancellation_option(job)
        self.assertEqual(item["state"], "available")
        result = self.actions.cancel_processing(job, item["revision"])
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(self.actions.cancel_processing(job, item["revision"])["state"], "cancelled")
        self.assertEqual(before, {name: (source / name).read_bytes() for name in before})
        self.assertEqual(deleted_jobs(self.state), set())
        self.assertEqual(stopped_jobs(self.state), {job})
        self.assertEqual(untouched, self.queue.tasks(other))
        saved = {t["stage"]: t for t in self.queue.tasks(job)}
        self.assertEqual((saved["publish"]["status"], saved["publish"]["receipt"]), ("succeeded", receipt))
        self.assertEqual((saved["notes"]["status"], saved["archive"]["status"]), ("cancelled", "cancelled"))
        self.assertEqual(next(item for item in meetings(self.queue) if item["job"] == job)["detail"], "Processing cancelled")
        self.queue.recover()
        self.assertEqual(self.queue.tasks(job), list(sorted(saved.values(), key=lambda t: t["stage"])))
        for act in (lambda: self.queue.release(job, "archive", self.config),
                    lambda: self.queue.retry_failed(job, retry_revision(self.queue.tasks(job)), self.config),
                    lambda: self.queue.enqueue(source, self.config)):
            with self.assertRaises(ProcessingCancelled): act()
        (source / CANCEL_MARKER).unlink()
        atomic_json(source / "transcript.json", {"segments": [{"text":"New revision", "start_ms":0}]})
        with self.assertRaises(ProcessingCancelled):
            self.queue.enqueue(source, self.config)
        source.rename(self.root / "moved-original")
        self.assertEqual(self.actions.cancellation_option(job)["state"], "cancelled")
        self.assertTrue(any(i["job"] == job and i["state"] == "cancelled" for i in self.actions.cancellation_options()))

    def test_prequeue_cancel_waits_for_transcriber_and_can_later_trash(self):
        source = self.source("Prequeue", transcript=False)
        neighbor = self.source("Other", transcript=False)
        options = self.actions.cancellation_options()
        item = next(i for i in options if i["title"] == "Prequeue")
        self.assertEqual(item["phase"], "transcription")
        self.assertGreater(item["recorded_at"], 0)
        (source / LEASE).touch(mode=0o600)
        with (source / LEASE).open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.assertEqual(self.actions.cancel_processing(item["job"], item["revision"])["state"], "cancelling")
        self.assertEqual(self.actions.cancellation_option(item["job"])["state"], "cancelled")
        self.assertTrue((source / "mic.caf").exists())
        self.assertFalse((neighbor / CANCEL_MARKER).exists())
        self.assertEqual(json.loads((source / CANCEL_MARKER).read_text())["reason"], "user_requested")
        self.assertEqual((source / CANCEL_MARKER).stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ProcessingCancelled):
            self.queue.enqueue(source, self.config)
        delete = next(i for i in self.actions.options() if i["job"] == item["job"])
        approved = self.actions.prepare(delete["job"], delete["revision"])
        self.assertEqual(self.actions.confirm(delete["job"], delete["revision"], approved["confirmation"])["status"], "trashed")
        self.assertFalse(source.exists())
        self.assertTrue(neighbor.exists())

    def test_prequeue_cancellation_works_before_lease_capability_exists(self):
        source = self.source("No lease yet", transcript=False)
        (source / CAPABILITY).unlink()
        item = next(i for i in self.actions.cancellation_options() if i["title"] == source.name)
        self.assertEqual(item["phase"], "transcription")
        self.assertFalse(self.actions.options()[0]["available"])
        self.assertEqual(self.actions.cancel_processing(item["job"], item["revision"])["state"], "cancelled")
        self.assertTrue((source / "mic.caf").is_file())
        self.assertFalse(self.actions.options()[0]["available"], "Trash must retain its legacy writer guard")

    def test_after_dispatch_receipt_survives_but_observer_and_next_stage_do_not_start(self):
        source = self.source("Running upload")
        job = self.queue.enqueue(source, self.config)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='succeeded' WHERE job=? AND stage='notes'", (job,))
        calls, observed = [], []
        def runner(stage, claimed, settings):
            calls.append(stage)
            item = self.actions.cancellation_option(job)
            self.assertEqual(self.actions.cancel_processing(job, item["revision"])["state"], "cancelling")
            return {"stage": stage, "job": claimed, "destination":"acknowledged-by-fixture"}
        self.queue.run_ready(runner, on_completed=lambda *args: observed.append(args))
        self.assertEqual(calls, ["archive"])
        self.assertEqual(observed, [])
        states = {t["stage"]: t for t in self.queue.tasks(job)}
        self.assertEqual(states["archive"]["status"], "succeeded")
        self.assertEqual(json.loads(states["archive"]["receipt"])["destination"], "acknowledged-by-fixture")
        self.assertEqual(states["publish"]["status"], "cancelled")
        self.assertFalse(self.queue.has_work())
        self.assertEqual(self.actions.cancellation_option(job)["state"], "cancelled")

    def test_owned_notes_child_stops_and_releases_lease(self):
        source = self.source("Active notes")
        ready = self.root / "ready"
        settings = dict(self.config, notes={"enabled": True, "command":[sys.executable, "-c",
            "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(20)", str(ready)]})
        job = self.queue.enqueue(source, settings)
        failures = []
        def work():
            worker = Queue(self.state)
            try:
                worker.run_ready(ConnectorRunner(self.state, self.root / "skill"))
            except BaseException as error:
                failures.append(error)
            finally:
                worker.close()
        thread = threading.Thread(target=work)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline: time.sleep(0.02)
            self.assertTrue(ready.exists(), "The synthetic notes child did not start")
            item = self.actions.cancellation_option(job)
            self.actions.cancel_processing(job, item["revision"])
            thread.join(5)
            self.assertFalse(thread.is_alive(), "The notes connector must stop its owned child")
            self.assertFalse(failures)
            self.assertEqual(self.actions.cancellation_option(job)["state"], "cancelled")
            self.assertEqual({t["status"] for t in self.queue.tasks(job)}, {"cancelled"})
        finally:
            thread.join(25)

    def test_native_marker_detected_and_stale_or_finished_requests_refused(self):
        source = self.source("Native")
        job = self.queue.enqueue(source, self.config)
        item = self.actions.cancellation_option(job)
        with self.assertRaises(WorkflowError): self.actions.cancel_processing(job, "forged")
        self.assertFalse((source / CANCEL_MARKER).exists())
        with self.queue.db: self.queue.db.execute("UPDATE tasks SET status='succeeded' WHERE job=?", (job,))
        self.assertIsNone(self.actions.cancellation_option(job))
        with self.assertRaises(WorkflowError): self.actions.cancel_processing(job, item["revision"])
        (source / CANCEL_MARKER).touch()
        self.assertEqual(self.actions.cancellation_option(job)["state"], "cancelled")
        self.assertIn(job, stopped_jobs(self.state))
        reviews = RoutingReviews(self.state)
        self.addCleanup(reviews.close)
        with self.assertRaisesRegex(RoutingError, "stopped"):
            reviews.approve(job, "stale", "company")
        local = self.source("Native prequeue", transcript=False)
        (local / CANCEL_MARKER).touch()
        self.assertTrue(any(i["title"] == local.name and i["state"] == "cancelled" for i in self.actions.cancellation_options()))
