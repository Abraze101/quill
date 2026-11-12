import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.pipeline import (ConnectorRunner, NeedsConfiguration, Queue,
                                     WorkflowError, atomic_json, route_settings)
from quill_workflow.routing import RoutingReviews
from quill_workflow.review_library import early_folder_choice
from quill_workflow.review_server import create_server, _selection
from quill_workflow.queue_status import meetings, retry_revision


class PipelineRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = self.root / "recordings/test"
        self.session.mkdir(parents=True)
        for name in ("mic.caf", "system.caf"):
            (self.session / name).write_bytes(b"synthetic fixture")
        atomic_json(self.session / "meta.json", {"started": "start", "ended": "end",
                    "files": {"mic": "mic.caf", "system": "system.caf"}})
        self.transcript = {"segments": [{"start_ms": 0, "text": "Acme agreed to send the agenda."}]}
        atomic_json(self.session / "transcript.json", self.transcript)
        self.companies = [{"id": "acme", "name": "Acme", "drive_folder_id": "company_folder_123"}]
        self.config = {"recordings_root": str(self.session.parent),
            "notes": {"enabled": True, "command": ["notes", "{output}"]},
            "route": {"enabled": True, "companies": self.companies,
                      "command": ["route", "{input}", "{companies}", "{output}"]},
            "archive": {"enabled": True, "command": ["archive", "{output}", "{job}", "{idempotency_key}",
                                                        "{route}", "{drive_folder_id}"]},
            "publish": {"enabled": True, "command": ["publish", "{output}", "{job}", "{idempotency_key}",
                                                        "{route}", "{drive_folder_id}"]}}
        self.queue = Queue(self.root / "state")
        self.reviews = RoutingReviews(self.queue.state)
        self.runner = ConnectorRunner(self.queue.state, ROOT / "skills/call-summary/SKILL.md")
        self.calls = []
        self.suggestion = {"schema_version": 1, "company_id": "acme", "confidence": "high",
                           "reason": "Explicit ownership", "evidence": [0], "ambiguous": False}
        self.runner.execute = self.execute

    def tearDown(self):
        self.reviews.close()
        self.queue.close()
        self.temp.cleanup()

    def execute(self, argv, cwd, timeout):
        self.calls.append(argv[0])
        if argv[0] == "notes":
            atomic_json(argv[1], {"schema_version": 1,
                        "summary": [{"text": "The agenda was agreed.", "evidence": [0]}]})
        elif argv[0] == "route":
            self.assertEqual(json.loads(Path(argv[2]).read_text())[0]["drive_folder_id"], "company_folder_123")
            atomic_json(argv[3], self.suggestion)
        else:
            selected = json.loads(Path(argv[4]).read_text())
            self.assertEqual(selected["drive_folder_id"], argv[5])
            self.assertEqual(argv[5], "company_folder_123")
            atomic_json(argv[1], {"job": argv[2], "stage": argv[0], "idempotency_key": argv[3],
                                 "destination": "verified-fixture/" + argv[5]})

    def enqueue(self):
        return self.queue.enqueue(self.session, self.config)

    def states(self, job):
        return {task["stage"]: task["status"] for task in self.queue.tasks(job)}

    def approve(self, job):
        review = self.reviews.get(job)
        self.reviews.approve(job, "acme", review["revision"])
        self.queue.release(job, "route", self.config)

    def test_legacy_jobs_keep_three_stages(self):
        legacy = {"recordings_root": str(self.session.parent)}
        job = self.queue.enqueue(self.session, legacy)
        self.assertEqual(set(self.states(job)), {"archive", "notes", "publish"})
        with self.assertRaisesRegex(WorkflowError, "cannot add stages"):
            self.queue.release(job, "route", self.config)

    def test_manual_folder_before_transcript_survives_notes_and_files_once(self):
        (self.session / "transcript.json").unlink()
        self.config["archive"]["enabled"] = False
        job = self.queue.enqueue(self.session, self.config, recording_ready=True)
        early = early_folder_choice(self.queue.state, job)
        self.assertIsNone(self.reviews.get(job), "Opening an early picker must be read-only")
        server = create_server(self.queue.state, processing_options=lambda selected=None: [{
            "job": job, "state": "running", "detail": "Transcribing the recording"}])
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request("GET", server.review_path + "?meeting=" + _selection(job))
            page = connection.getresponse().read().decode()
            self.assertIn("Transcribing the recording", page)
            self.assertIn(early["revision"], page)
            fields = {"action": "approve", "job": job, "company_id": "acme", "revision": early["revision"]}
            connection.request("POST", server.review_path + "decision", urlencode(fields),
                               {"Origin": server.review_origin, "Content-Type": "application/x-www-form-urlencoded"})
            response = connection.getresponse()
            self.assertEqual(response.status, 303)
            response.read()
        finally:
            connection.close()
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        approved = self.reviews.get(job)
        self.assertIsNone(approved["transcript_sha256"])
        self.assertEqual(self.reviews.approve(job, "acme", early["revision"]), approved)
        atomic_json(self.session / "transcript.json", self.transcript)
        self.assertEqual(self.queue.enqueue(self.session, self.config, handoff=True), job)
        self.queue.run_ready(self.runner)
        self.assertEqual(self.calls, ["notes", "publish"])
        self.assertEqual(self.states(job), {"archive": "held", "publish": "succeeded", "route": "succeeded", "notes": "succeeded"})
        self.assertTrue(self.reviews.get(job)["transcript_sha256"])
        self.assertEqual(self.queue.run_ready(self.runner), 0)

    def test_manual_choice_wins_if_model_writes_a_suggestion_or_fails_mid_decision(self):
        job = self.enqueue()
        early = early_folder_choice(self.queue.state, job)
        def choose_in_model(argv, cwd, timeout):
            if argv[0] == "route":
                self.reviews.skip(job, early["revision"])
                raise WorkflowError("The suggestion process failed after the decision")
            return self.execute(argv, cwd, timeout)
        self.runner.execute = choose_in_model
        self.queue.run_ready(self.runner)
        self.assertEqual(self.calls, ["notes"])
        self.assertEqual(self.reviews.get(job)["status"], "skipped")
        receipt = json.loads((self.queue.state / "results" / job / "notes-receipt.json").read_text())
        self.assertEqual(self.reviews.get(job)["transcript_sha256"], receipt["input_sha256"])
        self.assertIsNone(self.reviews.get(job)["approved_route"])

    def test_early_form_remains_valid_when_suggestion_arrives_first(self):
        job = self.enqueue()
        early = early_folder_choice(self.queue.state, job)
        self.queue.run_ready(self.runner)
        self.assertEqual(self.reviews.get(job)["status"], "pending")
        decision = self.reviews.approve(job, "acme", early["revision"])
        self.assertEqual(decision["decision_source"], "user")
        self.assertEqual(self.reviews.approve(job, "acme", early["revision"]), decision)

    def test_optional_preparation_failure_does_not_mask_allocation_or_local_notes(self):
        job = self.enqueue()
        with self.queue.db:
            self.queue.db.execute("INSERT INTO tasks(job,stage,status,settings,error) "
                                  "VALUES (?,'prepare_media','failed','{}','Temporary upload stopped')", (job,))
        self.queue.run_ready(self.runner)
        status = meetings(self.queue, job)[0]
        self.assertEqual(status["state"], "waiting_for_company")
        self.assertFalse(status["retryable"])
        self.reviews.skip(job, self.reviews.get(job)["revision"])
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='failed',receipt=NULL,error='Notes failed' "
                                  "WHERE job=? AND stage='notes'", (job,))
        status = meetings(self.queue, job)[0]
        self.assertEqual(status["detail"], "Notes failed")
        self.assertTrue(status["retryable"])
        self.assertEqual(self.queue.retry_failed(job, status["revision"], self.config), ["notes"])

    def test_review_holds_external_work_without_worker_spin_or_reclassification(self):
        job = self.enqueue()
        self.assertEqual(self.queue.run_ready(self.runner), 2)
        self.assertEqual(self.calls, ["notes", "route"])
        self.assertEqual(self.states(job), {"notes": "succeeded", "route": "held",
                                          "archive": "pending", "publish": "pending"})
        self.assertFalse(self.queue.has_work())
        status = json.loads((self.queue.state / "status.json").read_text())
        self.assertEqual(status["pending_reviews"], 1)
        self.queue.release(job, "route", self.config)
        self.queue.run_ready(self.runner)
        self.assertEqual(self.calls, ["notes", "route"])
        self.assertFalse(self.queue.has_work())
        self.approve(job)
        self.queue.run_ready(self.runner)
        self.assertEqual(self.calls, ["notes", "route", "archive", "publish"])
        self.assertTrue(all(state == "succeeded" for state in self.states(job).values()))
        self.assertEqual(json.loads((self.queue.state / "status.json").read_text())["pending_reviews"], 0)

    def test_notes_dependency_blocks_routing_and_destinations(self):
        self.config["notes"]["enabled"] = False
        job = self.enqueue()
        self.assertFalse(self.queue.has_work())
        self.assertEqual(self.queue.run_ready(self.runner), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.states(job)["route"], "pending")

    def test_release_cannot_drop_route_dependency_or_add_stage_to_legacy(self):
        self.config["archive"]["enabled"] = False
        job = self.enqueue()
        modified = {"archive": dict(self.config["archive"], enabled=True, requires_route=False)}
        self.queue.release(job, "archive", modified)
        task = next(task for task in self.queue.tasks(job) if task["stage"] == "archive")
        self.assertTrue(json.loads(task["settings"])["requires_route"])
        self.queue.run_ready(self.runner, enabled={"archive": True})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.states(job)["archive"], "held")
        self.assertFalse(self.queue.has_work())

    def test_disabling_routing_blocks_destinations_even_after_route_succeeds(self):
        self.config["archive"]["enabled"] = self.config["publish"]["enabled"] = False
        job = self.enqueue()
        self.queue.run_ready(self.runner)
        self.approve(job)
        self.queue.run_ready(self.runner)
        self.assertEqual(self.states(job)["route"], "succeeded")
        self.config["archive"]["enabled"] = True
        self.queue.release(job, "archive", self.config)
        self.queue.run_ready(self.runner, enabled={"archive": True, "notes": True})
        self.assertEqual(self.states(job)["archive"], "held")
        self.assertEqual(self.calls, ["notes", "route"])

    def test_changed_company_map_rejects_release_and_preserves_frozen_map(self):
        job = self.enqueue()
        self.queue.run_ready(self.runner)
        original = self.queue.tasks(job)
        self.config["route"]["companies"] = [dict(self.companies[0], drive_folder_id="different_folder_456")]
        with self.assertRaisesRegex(NeedsConfiguration, "new routing review"):
            self.queue.release(job, "route", self.config)
        self.assertEqual(self.queue.tasks(job), original)

    def test_added_category_preserves_old_review_and_original_destination(self):
        job = self.enqueue()
        self.queue.run_ready(self.runner)
        original = self.reviews.get(job)
        frozen = next(task["settings"] for task in self.queue.tasks(job) if task["stage"] == "route")
        self.config["route"]["companies"] = self.companies + [
            {"id": "general", "name": "General", "drive_folder_id": "general_folder_123"}]
        self.queue.release(job, "route", self.config)
        self.queue.run_ready(self.runner)
        self.assertEqual(self.reviews.get(job), original)
        self.assertEqual(self.calls, ["notes", "route"])
        self.assertEqual(next(task["settings"] for task in self.queue.tasks(job) if task["stage"] == "route"), frozen)
        self.approve(job)
        self.queue.run_ready(self.runner)
        self.assertTrue(all(state == "succeeded" for state in self.states(job).values()))
        self.assertEqual(self.calls, ["notes", "route", "archive", "publish"])
        self.assertEqual(self.reviews.get(job)["approved_route"]["company_id"], "acme")

    def test_added_category_allows_retry_without_rewriting_frozen_settings_or_receipts(self):
        job = self.enqueue()
        self.queue.run_ready(self.runner)
        self.approve(job)
        self.queue.run_ready(self.runner)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='failed',receipt=NULL,error='Offline' "
                                  "WHERE job=? AND stage='archive'", (job,))
        before = {task["stage"]: task for task in self.queue.tasks(job)}
        review = self.reviews.get(job)
        self.config["route"]["companies"] = self.companies + [
            {"id": "general", "name": "General", "drive_folder_id": "general_folder_123"}]
        self.assertEqual(self.queue.retry_failed(job, retry_revision(list(before.values())), self.config), ["archive"])
        after = {task["stage"]: task for task in self.queue.tasks(job)}
        for stage in ("notes", "route", "publish"):
            self.assertEqual(after[stage], before[stage])
        self.assertEqual(after["archive"]["settings"], before["archive"]["settings"])
        self.assertEqual(after["archive"]["status"], "pending")
        self.assertEqual(self.reviews.get(job), review)

    def test_removed_category_blocks_release_and_retry_without_mutations(self):
        job = self.enqueue()
        self.queue.run_ready(self.runner)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='failed',error='Offline' "
                                  "WHERE job=? AND stage='archive'", (job,))
        before = self.queue.tasks(job)
        self.config["route"]["companies"] = [
            {"id": "general", "name": "General", "drive_folder_id": "general_folder_123"}]
        with self.assertRaisesRegex(NeedsConfiguration, "new routing review"):
            self.queue.release(job, "route", self.config)
        with self.assertRaisesRegex(NeedsConfiguration, "Company routing changed"):
            self.queue.retry_failed(job, retry_revision(before), self.config)
        self.assertEqual(self.queue.tasks(job), before)

    def test_stale_transcript_rejects_even_cached_external_receipt(self):
        job = self.enqueue()
        self.queue.run_ready(self.runner)
        self.approve(job)
        self.queue.run_ready(self.runner)
        archive = next(task for task in self.queue.tasks(job) if task["stage"] == "archive")
        before = list(self.calls)
        atomic_json(self.queue.state / "recordings" / job / "transcript.json",
                    {"segments": [{"start_ms": 0, "text": "Different organizational context."}]})
        with self.assertRaisesRegex(NeedsConfiguration, "Transcript or company folders changed"):
            self.runner("archive", job, json.loads(archive["settings"]))
        self.assertEqual(self.calls, before)

    def test_frozen_map_tampering_rejects_cached_receipt(self):
        job = self.enqueue()
        self.queue.run_ready(self.runner)
        self.approve(job)
        self.queue.run_ready(self.runner)
        route_task = next(task for task in self.queue.tasks(job) if task["stage"] == "route")
        settings = json.loads(route_task["settings"])
        settings["companies"][0]["drive_folder_id"] = "different_folder_456"
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET settings=? WHERE job=? AND stage='route'",
                                  (json.dumps(settings), job))
        with self.assertRaisesRegex(NeedsConfiguration, "Transcript or company folders changed"):
            self.runner("archive", job, self.config["archive"])
        self.assertEqual(self.calls, ["notes", "route", "archive", "publish"])

    def test_missing_route_cannot_bypass_a_pinned_destination_guard(self):
        job = self.enqueue()
        self.queue.run_ready(self.runner)
        self.approve(job)
        self.queue.run_ready(self.runner)
        with self.queue.db:
            self.queue.db.execute("DELETE FROM tasks WHERE job=? AND stage='route'", (job,))
        with self.assertRaisesRegex(NeedsConfiguration, "required company routing review"):
            self.runner("archive", job, dict(self.config["archive"], requires_route=False))
        self.assertEqual(self.calls, ["notes", "route", "archive", "publish"])

    def test_unknown_and_skipped_reviews_never_reach_connectors(self):
        self.config["route"]["review_required"] = False
        self.suggestion.update(company_id=None, confidence="low", ambiguous=True, evidence=[],
                               reason="Insufficient organizational context")
        job = self.enqueue()
        self.queue.run_ready(self.runner)
        review = self.reviews.get(job)
        self.assertEqual(review["status"], "pending")
        self.reviews.skip(job, review["revision"])
        self.queue.release(job, "route", self.config)
        self.queue.run_ready(self.runner)
        self.assertEqual(self.calls, ["notes", "route"])
        self.assertFalse(self.queue.has_work())

    def test_explicit_automatic_policy_routes_only_supported_high_confidence(self):
        self.config["route"]["review_required"] = False
        job = self.enqueue()
        self.queue.run_ready(self.runner)
        self.assertEqual(self.calls, ["notes", "route", "archive", "publish"])
        self.assertEqual(self.reviews.get(job)["decision_source"], "automatic")

    def test_invalid_company_map_fails_before_enqueue(self):
        self.config["route"]["companies"] = []
        with self.assertRaises(NeedsConfiguration):
            self.enqueue()
        self.assertEqual(self.queue.tasks(), [])

    def explicit_input(self):
        text = "This meeting is about Acme's own recruitment plan."
        self.transcript["segments"][0]["text"] = text
        atomic_json(self.session / "transcript.json", self.transcript)
        self.suggestion.update(schema_version=2,
            explicit_basis={"kind": "ownership", "segment_index": 0, "quote": text})

    def test_explicit_company_policy_files_new_jobs_without_folder_review(self):
        self.explicit_input()
        self.config["route"].update(review_required=True, auto_file_explicit_companies=True)
        job = self.enqueue()
        self.queue.run_ready(self.runner)
        self.assertEqual(self.calls, ["notes", "route", "archive", "publish"])
        review = self.reviews.get(job)
        self.assertEqual(review["decision_source"], "automatic_explicit")
        self.assertTrue(all(state == "succeeded" for state in self.states(job).values()))
        self.assertEqual(set(self.states(job)), {"notes", "route", "archive", "publish"})

    def test_explicit_policy_is_frozen_and_never_added_to_a_released_old_job(self):
        self.explicit_input()
        job = self.enqueue()
        self.config["route"]["auto_file_explicit_companies"] = True
        self.queue.run_ready(self.runner)
        self.assertEqual(self.states(job)["route"], "held")
        original = self.reviews.get(job)
        self.queue.release(job, "route", self.config)
        settings = next(json.loads(task["settings"]) for task in self.queue.tasks(job) if task["stage"] == "route")
        self.assertFalse(settings["auto_file_explicit_companies"])
        self.queue.run_ready(self.runner)
        self.assertEqual(self.reviews.get(job), original)
        self.assertEqual(self.calls, ["notes", "route"])

    def test_old_frozen_policy_without_field_does_not_inherit_the_live_setting(self):
        self.explicit_input()
        job = self.enqueue()
        with self.queue.db:
            row = self.queue.db.execute("SELECT settings FROM tasks WHERE job=? AND stage='route'", (job,)).fetchone()
            settings = json.loads(row[0])
            settings.pop("auto_file_explicit_companies", None)
            self.queue.db.execute("UPDATE tasks SET settings=? WHERE job=? AND stage='route'", (json.dumps(settings), job))
        self.config["route"]["auto_file_explicit_companies"] = True
        self.queue.run_ready(self.runner)
        self.assertEqual(self.states(job)["route"], "held")
        self.assertEqual(self.reviews.get(job)["status"], "pending")
        self.assertEqual(self.calls, ["notes", "route"])

    def test_explicit_policy_validates_boolean_and_defaults_to_off(self):
        self.assertFalse(route_settings(self.config["route"])["auto_file_explicit_companies"])
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaisesRegex(WorkflowError, "must be a boolean"):
                route_settings(dict(self.config["route"], auto_file_explicit_companies=value))

    def test_registered_rename_is_live_only_for_old_and_new_jobs(self):
        folder = "nested_folder_123"
        child = {"id": "acme-sales", "name": "Acme / Sales", "drive_folder_id": folder,
                 "drive_folder_path": [{"name": "Acme", "drive_folder_id": "company_folder_123"},
                                       {"name": "Sales", "drive_folder_id": folder}]}
        self.companies.append(child)
        old_job = self.queue.enqueue(self.session, self.config)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='held' WHERE job=? AND stage='route'", (old_job,))
        child["name"] = "Acme / Client Sales"
        child["drive_folder_path"][-1]["name"] = "Client Sales"
        self.config["route"]["previous_drive_folder_names"] = {folder: ["Sales"]}
        self.queue.release(old_job, "route", self.config)
        self.session = self.session.rename(self.session.with_name("second"))
        new_job = self.queue.enqueue(self.session, self.config)
        for job, name in ((old_job, "Acme / Sales"), (new_job, "Acme / Client Sales")):
            task = next(t for t in self.queue.tasks(job) if t["stage"] == "route")
            settings = json.loads(task["settings"])
            self.assertNotIn("previous_drive_folder_names", settings)
            saved = next(c for c in route_settings(settings)["companies"] if c["id"] == child["id"])
            self.assertEqual(saved["name"], name)
            self.assertNotIn("previous_folder_names", saved)
        self.config["route"]["previous_drive_folder_names"] = {"unknown_folder_id": ["Sales"]}
        with self.assertRaisesRegex(NeedsConfiguration, "configured Drive folder ID"):
            route_settings(self.config["route"])


if __name__ == "__main__":
    unittest.main()
