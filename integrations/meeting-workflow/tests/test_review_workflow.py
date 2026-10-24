import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.pipeline import Queue, WorkflowError, atomic_json, render_notes
from quill_workflow.routing import RoutingReviews

spec = importlib.util.spec_from_file_location("review_workflow", ROOT / "workflow.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        session = self.root / "recordings/test"
        session.mkdir(parents=True)
        (session / "mic.caf").write_bytes(b"synthetic")
        (session / "system.caf").write_bytes(b"synthetic")
        atomic_json(session / "meta.json", {"started": "2026-01-01T00:00:00Z",
                    "ended": "2026-01-01T00:00:01Z", "files": {"mic": "mic.caf", "system": "system.caf"}})
        self.config = {"recordings_root": str(session.parent), "route": {
            "enabled": True, "command": ["fake-classifier"], "review_required": True,
            "companies": [{"id": "one", "name": "One", "drive_folder_id": "folder_123456"}]}}
        self.config_path = self.root / "config.json"
        atomic_json(self.config_path, self.config)
        self.queue = Queue(self.root / "state")
        self.job = self.queue.enqueue(session, self.config)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='held' WHERE job=? AND stage='route'", (self.job,))

    def tearDown(self):
        self.queue.close()
        self.temporary.cleanup()

    def statuses(self):
        return {item["stage"]: item["status"] for item in self.queue.tasks(self.job)}

    def test_choice_resumes_only_selected_route_and_repeat_can_retry_worker_launch(self):
        with patch.object(workflow, "start_worker") as start:
            record = {"job": self.job, "status": "approved"}
            workflow.resume_reviewed_job(record, self.config_path, self.queue.state)
            workflow.resume_reviewed_job(record, self.config_path, self.queue.state)
            self.assertEqual(start.call_count, 2)
        self.assertEqual(self.statuses(), {"archive": "held", "notes": "held", "route": "pending", "publish": "held"})

    def test_disabled_routing_keeps_choice_local_without_starting_worker(self):
        self.config["route"]["enabled"] = False
        atomic_json(self.config_path, self.config)
        with patch.object(workflow, "start_worker") as start:
            workflow.resume_reviewed_job({"job": self.job, "status": "approved"}, self.config_path, self.queue.state)
            start.assert_not_called()
        self.assertEqual(set(self.statuses().values()), {"held"})

    def test_failed_worker_launch_after_release_can_be_retried(self):
        record = {"job": self.job, "status": "approved"}
        with patch.object(workflow, "start_worker", side_effect=OSError("synthetic launch failure")):
            with self.assertRaises(OSError):
                workflow.resume_reviewed_job(record, self.config_path, self.queue.state)
        self.assertEqual(self.statuses()["route"], "pending")
        with patch.object(workflow, "start_worker") as start:
            workflow.resume_reviewed_job(record, self.config_path, self.queue.state)
            start.assert_called_once()

    def test_approval_before_worker_marks_held_is_reconciled_without_releasing_other_holds(self):
        reviews = RoutingReviews(self.queue.state)
        try:
            record = reviews.propose(self.job, b'{"segments":[{"text":"synthetic"}]}', self.config["route"]["companies"],
                {"schema_version": 1, "company_id": None, "confidence": "low", "reason": "Unclear",
                 "evidence": [], "ambiguous": True})
            reviews.approve(self.job, "one", record["revision"])
        finally:
            reviews.close()
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='running' WHERE job=? AND stage='route'", (self.job,))
        with patch.object(workflow, "start_worker"):
            workflow.resume_reviewed_job({"job": self.job, "status": "approved"}, self.config_path, self.queue.state)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='held',error=? WHERE job=? AND stage='route'",
                                  ("Choose this meeting's company in the routing review", self.job))
        self.assertEqual(workflow.reconcile_approved_reviews(self.queue, self.config), 1)
        self.assertEqual(self.statuses(), {"archive": "held", "notes": "held", "route": "pending", "publish": "held"})
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='held',error='Stage disabled in current configuration' WHERE stage='route'")
        self.assertEqual(workflow.reconcile_approved_reviews(self.queue, self.config), 0)

    def test_keep_local_and_standalone_pilot_do_not_release_any_queue_task(self):
        with patch.object(workflow, "start_worker") as start:
            workflow.resume_reviewed_job({"job": self.job, "status": "skipped"}, self.config_path, self.queue.state)
            workflow.resume_reviewed_job({"job": "pilot-only", "status": "approved"}, self.config_path, self.queue.state)
            start.assert_not_called()
        self.assertEqual(set(self.statuses().values()), {"held"})

    def approved_resume_fixture(self):
        transcript = self.queue.state / "recordings" / self.job / "transcript.json"
        source = {"segments": [{"start_ms": 0, "text": "Synthetic meeting for company One."}]}
        atomic_json(transcript, source)
        result = self.queue.state / "results" / self.job
        receipt = {"input_sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(), "settings_sha256": "fixture"}
        atomic_json(result / "notes-receipt.json", receipt)
        atomic_json(result / "notes/provenance.json", receipt)
        notes = {"schema_version": 1, "summary": [{"text": "Synthetic meeting.", "evidence": [0]}]}
        atomic_json(result / "notes/notes.json", notes)
        (result / "notes/source-transcript.json").write_bytes(transcript.read_bytes())
        (result / "notes/notes.md").write_text(render_notes(notes, source))
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='succeeded' WHERE job=? AND stage='notes'", (self.job,))
        for stage in ("archive", "publish"):
            self.config[stage] = {"enabled": True, "command": ["synthetic-only"]}
        atomic_json(self.config_path, self.config)
        reviews = RoutingReviews(self.queue.state)
        try:
            record = reviews.propose(self.job, transcript.read_bytes(), self.config["route"]["companies"],
                {"schema_version": 1, "company_id": "one", "confidence": "high", "reason": "Explicit company",
                 "evidence": [0], "ambiguous": False})
            return reviews.approve(self.job, "one", record["revision"])
        finally:
            reviews.close()

    def test_resume_releases_only_this_approved_meetings_enabled_waiting_work(self):
        record = self.approved_resume_fixture()
        # Unrelated legacy holds must remain byte-for-byte unchanged.
        with self.queue.db:
            self.queue.db.execute("INSERT INTO jobs VALUES ('older-job','/synthetic/older',0)")
            for stage in ("archive", "notes", "publish"):
                self.queue.db.execute("INSERT INTO tasks (job,stage,status,settings) VALUES (?,?,?,?)",
                                      ("older-job", stage, "held", '{}'))
        old = self.queue.tasks("older-job")
        self.config["publish"]["enabled"] = False
        atomic_json(self.config_path, self.config)
        self.assertEqual(workflow.resumable_stages(record, self.config_path, self.queue.state), ["route", "archive"])
        with patch.object(workflow, "start_worker") as start:
            self.assertEqual(workflow.resume_waiting_work(record, self.config_path, self.queue.state), ["route", "archive"])
            start.assert_called_once()
        self.assertEqual(self.statuses(), {"archive": "pending", "notes": "succeeded", "route": "pending", "publish": "held"})
        self.assertEqual(self.queue.tasks("older-job"), old)
        pending = self.queue.tasks(self.job)
        with patch.object(workflow, "start_worker") as start:
            self.assertEqual(workflow.resume_waiting_work(record, self.config_path, self.queue.state), ["route", "archive"])
            start.assert_called_once()
        self.assertEqual(self.queue.tasks(self.job), pending)

    def test_added_category_keeps_existing_resume_choices_and_explicit_release(self):
        record = self.approved_resume_fixture()
        frozen = next(task["settings"] for task in self.queue.tasks(self.job) if task["stage"] == "route")
        self.config["route"]["companies"].append(
            {"id": "general", "name": "General", "drive_folder_id": "general_folder_123"})
        atomic_json(self.config_path, self.config)
        self.assertEqual(workflow.resumable_stages(record, self.config_path, self.queue.state),
                         ["route", "archive", "publish"])
        self.assertEqual(self.statuses()["route"], "held")
        with patch.object(workflow, "start_worker") as start:
            self.assertEqual(workflow.resume_waiting_work(record, self.config_path, self.queue.state, ["route"]),
                             ["route"])
            start.assert_called_once()
        self.assertEqual(self.statuses(), {"archive": "held", "notes": "succeeded", "route": "pending", "publish": "held"})
        self.assertEqual(next(task["settings"] for task in self.queue.tasks(self.job) if task["stage"] == "route"), frozen)

    def test_removed_category_blocks_resume_without_releasing_any_work(self):
        record = self.approved_resume_fixture()
        before = self.queue.tasks(self.job)
        self.config["route"]["companies"] = [
            {"id": "general", "name": "General", "drive_folder_id": "general_folder_123"}]
        atomic_json(self.config_path, self.config)
        with patch.object(workflow, "start_worker") as start:
            with self.assertRaisesRegex(WorkflowError, "Company folders changed"):
                workflow.resume_waiting_work(record, self.config_path, self.queue.state)
            start.assert_not_called()
        self.assertEqual(self.queue.tasks(self.job), before)

    def test_resume_refuses_stale_review_transcript_notes_and_company_map(self):
        record = self.approved_resume_fixture()
        original = self.queue.tasks(self.job)
        with patch.object(workflow, "start_worker") as start:
            with self.assertRaises(WorkflowError):
                workflow.resume_waiting_work(dict(record, revision="stale"), self.config_path, self.queue.state)
            self.config["route"]["companies"][0]["drive_folder_id"] = "changed_folder_123"
            atomic_json(self.config_path, self.config)
            with self.assertRaises(WorkflowError):
                workflow.resume_waiting_work(record, self.config_path, self.queue.state)
            self.config["route"]["companies"] = record["companies"]
            atomic_json(self.config_path, self.config)
            receipt = self.queue.state / "results" / self.job / "notes-receipt.json"
            previous = receipt.read_bytes()
            atomic_json(receipt, {"input_sha256": "changed"})
            with self.assertRaises(WorkflowError):
                workflow.resume_waiting_work(record, self.config_path, self.queue.state)
            receipt.write_bytes(previous)
            transcript = self.queue.state / "recordings" / self.job / "transcript.json"
            transcript.write_bytes(transcript.read_bytes() + b'\n')
            with self.assertRaises(WorkflowError):
                workflow.resume_waiting_work(record, self.config_path, self.queue.state)
            start.assert_not_called()
        self.assertEqual(self.queue.tasks(self.job), original)

    def test_resume_rechecks_disabled_settings_and_never_overrides_keep_local(self):
        record = self.approved_resume_fixture()
        original = self.queue.tasks(self.job)
        self.config["route"]["enabled"] = False
        atomic_json(self.config_path, self.config)
        with patch.object(workflow, "start_worker") as start:
            self.assertEqual(workflow.resume_waiting_work(record, self.config_path, self.queue.state), [])
            self.assertEqual(workflow.resume_waiting_work(dict(record, status="skipped"), self.config_path, self.queue.state), [])
            start.assert_not_called()
        self.assertEqual(self.queue.tasks(self.job), original)

    def test_resume_retries_launch_after_release_commits_without_resetting_pending_tasks(self):
        record = self.approved_resume_fixture()
        with patch.object(workflow, "start_worker", side_effect=OSError("Synthetic launch failure")):
            with self.assertRaises(OSError):
                workflow.resume_waiting_work(record, self.config_path, self.queue.state)
        self.assertEqual(self.statuses(), {"archive": "pending", "notes": "succeeded", "route": "pending", "publish": "pending"})
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='retry',attempts=3,next_attempt=9999999999,error='Synthetic backoff' "
                                  "WHERE job=? AND stage='publish'", (self.job,))
        pending = self.queue.tasks(self.job)
        with patch.object(workflow, "start_worker") as start:
            self.assertEqual(workflow.resume_waiting_work(record, self.config_path, self.queue.state), ["route", "archive", "publish"])
            start.assert_called_once()
        self.assertEqual(self.queue.tasks(self.job), pending)

    def test_resume_validates_current_notes_and_their_saved_source(self):
        record = self.approved_resume_fixture()
        before = self.queue.tasks(self.job)
        notes = self.queue.state / "results" / self.job / "notes"
        for name, changed in (("notes.md", b"Unreviewed disconnected markdown"),
                              ("notes.json", b'{"schema_version":1,"summary":[{"text":"Missing evidence","evidence":[99]}]}'),
                              ("source-transcript.json", b'{"segments":[]}'),
                              ("provenance.json", b'{"input_sha256":"changed"}')):
            path = notes / name
            original = path.read_bytes()
            try:
                path.write_bytes(changed)
                with self.subTest(name=name), patch.object(workflow, "start_worker") as start:
                    with self.assertRaises(WorkflowError):
                        workflow.resume_waiting_work(record, self.config_path, self.queue.state)
                    start.assert_not_called()
                self.assertEqual(self.queue.tasks(self.job), before)
            finally:
                path.write_bytes(original)

    def test_resume_accepts_exact_historical_render_but_rejects_changed_legacy_text(self):
        record = self.approved_resume_fixture()
        notes = self.queue.state / "results" / self.job / "notes"
        data = json.loads((notes / "notes.json").read_text())
        source = json.loads((notes / "source-transcript.json").read_text())
        historical = render_notes(data, source, include_sources=True)
        self.assertIn("(00:00, segment 0)", historical)
        (notes / "notes.md").write_text(historical)
        self.assertEqual(workflow.resumable_stages(record, self.config_path, self.queue.state),
                         ["route", "archive", "publish"])
        (notes / "notes.md").write_text(historical.replace("Synthetic meeting.", "Changed meeting."))
        before = self.queue.tasks(self.job)
        with patch.object(workflow, "start_worker") as start:
            with self.assertRaisesRegex(WorkflowError, "need review"):
                workflow.resume_waiting_work(record, self.config_path, self.queue.state)
            start.assert_not_called()
        self.assertEqual(self.queue.tasks(self.job), before)

    def test_resume_form_cannot_expand_to_newly_enabled_stages(self):
        record = self.approved_resume_fixture()
        self.config["publish"]["enabled"] = False
        atomic_json(self.config_path, self.config)
        displayed = workflow.resumable_stages(record, self.config_path, self.queue.state)
        self.assertEqual(displayed, ["route", "archive"])
        # Another Settings change after the page was rendered enables future
        # notes uploads; the existing submitted action must not release one.
        self.config["publish"]["enabled"] = True
        atomic_json(self.config_path, self.config)
        with patch.object(workflow, "start_worker"):
            self.assertEqual(workflow.resume_waiting_work(record, self.config_path, self.queue.state, displayed), displayed)
        self.assertEqual(self.statuses()["publish"], "held")
        self.assertEqual(next(task for task in self.queue.tasks(self.job) if task["stage"] == "publish")["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
