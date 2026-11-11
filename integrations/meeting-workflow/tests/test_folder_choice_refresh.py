from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workflow
from quill_workflow.folder_choice_refresh import refresh_folder_choices
from quill_workflow.local_deletion import job_lock, ProcessingBusy
from quill_workflow.pipeline import Queue, WorkflowError, atomic_json
from quill_workflow.recording_safety import RecordingSafetyHold
from quill_workflow.routing import RoutingReviews, RoutingError


class FolderChoiceRefreshTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.queue = Queue(self.root / "state")
        self.addCleanup(self.queue.close)
        self.reviews = RoutingReviews(self.queue.state)
        self.addCleanup(self.reviews.close)
        self.folders = [{"id": "acme", "name": "Acme", "drive_folder_id": "folder-acme-001"}]
        self.extra = {"id": "other", "name": "Other", "drive_folder_id": "folder-other-001"}
        self.config = {"recordings_root": str(self.root / "recordings"),
                       "route": {"enabled": True, "command": ["fixture"], "companies": self.folders,
                                 "review_required": True},
                       "archive": {"enabled": True, "command": ["fixture"]}}
        session = self.root / "recordings/meeting"
        session.mkdir(parents=True)
        for name in ("mic.caf", "system.caf"):
            (session / name).write_bytes(b"synthetic")
        atomic_json(session / "meta.json", {"started": "2026-01-01T00:00:00Z", "ended": "2026-01-01T00:01:00Z",
                                            "files": {"mic": "mic.caf", "system": "system.caf"}})
        atomic_json(session / "transcript.json", {"segments": [{"text": "A synthetic meeting."}]})
        self.job = self.queue.enqueue(session, self.config)
        self.transcript = self.queue.state / "recordings" / self.job / "transcript.json"
        source = self.transcript.read_bytes()
        self.result = self.queue.state / "results" / self.job
        receipt = {"job": self.job, "stage": "notes", "input_sha256": hashlib.sha256(source).hexdigest()}
        atomic_json(self.result / "notes-receipt.json", receipt)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='held' WHERE job=? AND stage='route'", (self.job,))
            self.queue.db.execute("UPDATE tasks SET status='succeeded',receipt=? WHERE job=? AND stage='notes'",
                                  (json.dumps(receipt), self.job))
        self.pending = self.reviews.propose(self.job, source, self.folders, {
            "schema_version": 1, "company_id": "acme", "confidence": "high", "reason": "Synthetic",
            "evidence": [0], "ambiguous": False})
        self.config["route"].update(companies=self.folders + [self.extra], review_required=False, command=["changed"])
        self.config_path = self.root / "config.json"
        atomic_json(self.config_path, self.config)

    def refresh(self):
        return refresh_folder_choices(self.queue, self.config_path, self.job, self.pending["revision"])

    def test_cli_refreshes_only_choices_and_supersedes_old_forms(self):
        before = self.queue.tasks(self.job)
        out = io.StringIO()
        with redirect_stdout(out), patch.object(workflow, "start_worker", side_effect=AssertionError):
            self.assertEqual(workflow.main(["--config", str(self.config_path), "--state", str(self.queue.state),
                                            "refresh-folder-choices", self.job, "--revision", self.pending["revision"]]), 0)
        updated = self.reviews.get(self.job)
        self.assertEqual(json.loads(out.getvalue()), {key: updated[key] for key in ("job", "status", "revision")})
        self.assertEqual(updated["status"], "pending")
        self.assertNotEqual(updated["revision"], self.pending["revision"])
        changed = {key for key in updated.keys() | self.pending.keys() if updated.get(key) != self.pending.get(key)}
        self.assertEqual(changed, {"revision", "updated_at", "companies", "company_map_sha256"})
        after = self.queue.tasks(self.job)
        for task in after:
            if task["stage"] == "route":
                settings = json.loads(task["settings"])
                self.assertEqual([c["id"] for c in settings["companies"]], ["acme", "other"])
                original = next(row for row in before if row["stage"] == "route")
                self.assertEqual(dict(settings, companies=[]), dict(json.loads(original["settings"]), companies=[]))
                task["settings"] = original["settings"]
        self.assertEqual(after, before)
        self.assertEqual(refresh_folder_choices(self.queue, self.config_path, self.job, updated["revision"]), updated)
        with self.assertRaises(RoutingError):
            self.reviews.approve(self.job, "acme", self.pending["revision"])
        with self.assertRaises(WorkflowError):
            self.refresh()
        self.assertEqual(self.reviews.approve(self.job, "other", updated["revision"])["approved_route"]["company_id"], "other")

    def test_committed_queue_change_recovers_after_interrupted_final_save(self):
        self.interrupt_before_final_review()
        interrupted = self.reviews.get(self.job)
        self.assertEqual(interrupted["status"], "refreshing")
        with self.assertRaises(RoutingError):
            self.reviews.approve(self.job, "acme", self.pending["revision"])
        with self.assertRaises(RoutingError):
            self.reviews.skip(self.job, self.pending["revision"])
        recovered = self.refresh()
        self.assertEqual(recovered["status"], "pending")
        self.assertNotIn("refresh_target_sha256", recovered)
        self.assertNotIn("refresh_target_companies", recovered)
        self.assertEqual([c["id"] for c in recovered["companies"]], ["acme", "other"])

    def interrupt_before_final_review(self):
        original_save = RoutingReviews._save

        def interrupt_final_save(store, record):
            if record["status"] == "pending":
                raise InterruptedError()
            return original_save(store, record)

        with patch.object(RoutingReviews, "_save", interrupt_final_save), self.assertRaises(InterruptedError):
            self.refresh()

    def test_recovery_after_review_commit_before_queue_update(self):
        self.queue.db.execute("CREATE TEMP TRIGGER interrupt BEFORE UPDATE OF settings ON tasks "
                              "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.refresh()
        self.assertEqual(self.reviews.get(self.job)["status"], "refreshing")
        self.queue.db.execute("DROP TRIGGER interrupt")
        self.assertEqual(self.refresh()["status"], "pending")

    def test_recovery_finishes_saved_refresh_even_after_another_folder_is_added(self):
        self.queue.db.execute("CREATE TEMP TRIGGER interrupt BEFORE UPDATE OF settings ON tasks "
                              "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.refresh()
        self.queue.db.execute("DROP TRIGGER interrupt")
        self.config["route"]["companies"].append(dict(self.extra, id="later", drive_folder_id="folder-later-001"))
        atomic_json(self.config_path, self.config)
        recovered = self.refresh()
        self.assertEqual([c["id"] for c in recovered["companies"]], ["acme", "other"])
        self.assertIsNone(recovered["approved_route"])
        self.assertEqual(recovered["status"], "pending")
        queued = next(t for t in self.queue.tasks(self.job) if t["stage"] == "route")
        self.assertEqual(json.loads(queued["settings"])["companies"], recovered["companies"])
        with self.assertRaises(RoutingError):
            self.reviews.approve(self.job, "later", recovered["revision"])

    def test_recovery_rolls_back_incompatible_destination_without_accepting_either_old_form(self):
        self.interrupt_before_final_review()
        self.config["route"]["companies"] = self.folders + [dict(self.extra, drive_folder_id="other-changed-001")]
        atomic_json(self.config_path, self.config)
        recovered = self.refresh()
        self.assertNotEqual(recovered["revision"], self.pending["revision"])
        self.assertEqual(recovered["companies"], self.pending["companies"])
        self.assertEqual(recovered["company_map_sha256"], self.pending["company_map_sha256"])
        self.assertIsNone(recovered["approved_route"])
        self.assertEqual(recovered["status"], "pending")
        queued = next(t for t in self.queue.tasks(self.job) if t["stage"] == "route")
        self.assertEqual(queued["status"], "held")
        self.assertEqual(json.loads(queued["settings"])["companies"], recovered["companies"])
        with self.assertRaises(RoutingError):
            self.reviews.approve(self.job, "acme", self.pending["revision"])
        with self.assertRaises(RoutingError):
            self.reviews.approve(self.job, "other", recovered["revision"])
        updated = refresh_folder_choices(self.queue, self.config_path, self.job, recovered["revision"])
        self.assertEqual(updated["companies"][1]["drive_folder_id"], "other-changed-001")

    def test_rejects_incompatible_map_or_conflicting_sources(self):
        self.config["route"]["companies"] = [dict(self.folders[0], drive_folder_id="folder-changed-001")]
        atomic_json(self.config_path, self.config)
        with self.assertRaises(WorkflowError):
            self.refresh()
        self.config["route"]["companies"] = self.folders + [self.extra]
        atomic_json(self.config_path, self.config)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET settings=? WHERE job=? AND stage='route'",
                                  (json.dumps(self.config["route"]), self.job))
        with self.assertRaises(WorkflowError):
            self.refresh()
        self.assertEqual(self.reviews.get(self.job), self.pending)

    def test_guard_source_hash(self):
        self.transcript.write_bytes(self.transcript.read_bytes() + b" ")
        with self.assertRaises(WorkflowError):
            self.refresh()
        self.assertEqual(self.reviews.get(self.job), self.pending)

    def test_live_folder_history_is_not_frozen_into_review_or_task(self):
        nested = {"id": "nested", "name": "Acme / Design", "drive_folder_id": "folder-design-001",
                  "drive_folder_path": [{"name": "Acme", "drive_folder_id": "folder-acme-001"},
                                        {"name": "Design", "drive_folder_id": "folder-design-001"}],
                  "previous_folder_names": ["Studio"]}
        self.config["route"]["companies"] = self.folders + [nested]
        atomic_json(self.config_path, self.config)
        refreshed = self.refresh()
        queued = next(task for task in self.queue.tasks(self.job) if task["stage"] == "route")
        self.assertEqual(json.loads(queued["settings"])["companies"], refreshed["companies"])
        self.assertNotIn("previous_folder_names", refreshed["companies"][1])
        self.assertEqual(refreshed["companies"][1]["drive_folder_path"], nested["drive_folder_path"])

    def test_guard_active_stopped_decided_or_partially_filed(self):
        with job_lock(self.queue.state, self.job), self.assertRaises(ProcessingBusy):
            self.refresh()
        for name in ("drive-meeting.json", "archive-receipt.json", "route.json"):
            (self.result / name).touch()
            with self.assertRaises(WorkflowError):
                self.refresh()
            (self.result / name).unlink()
        for stage, field, value in (("route", "status", "running"), ("archive", "attempts", 1),
                                    ("publish", "receipt", "{}")):
            self.queue.db.execute(f"UPDATE tasks SET {field}=? WHERE job=? AND stage=?", (value, self.job, stage))
            self.queue.db.commit()
            with self.assertRaises(WorkflowError):
                self.refresh()
            previous = {"status": "held", "attempts": 0, "receipt": None}[field]
            self.queue.db.execute(f"UPDATE tasks SET {field}=? WHERE job=? AND stage=?", (previous, self.job, stage))
            self.queue.db.commit()
        with self.queue.db:
            self.queue.db.execute("INSERT INTO local_deletions VALUES (?, '{}')", (self.job,))
        with self.assertRaises(RecordingSafetyHold):
            self.refresh()
        with self.queue.db:
            self.queue.db.execute("DELETE FROM local_deletions WHERE job=?", (self.job,))
        self.reviews.skip(self.job, self.pending["revision"])
        with self.assertRaises(WorkflowError):
            self.refresh()
