"""Automatic lookups read calendars only; sharing remains an explicit choice."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.pipeline import Queue, WorkflowError
from quill_workflow.sharing import SharingReviews, digest


class AutomaticSuggestionsTests(unittest.TestCase):
    SINCE = 1000.0

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)
        self.config = self.state / "config.json"
        self.configure()
        self.queue = Queue(self.state)
        self.now = self.SINCE + 1
        self.clock = patch("quill_workflow.sharing.time.time", side_effect=lambda: self.now)
        self.clock.start()
        self.source = {"title": "Synthetic notes", "preview": "A reviewed next step.", "owner": "owner@example.com"}
        self.reviews = SharingReviews(self.state, self.config, lambda job: copy.deepcopy(self.source))
        self.reviews._suggestion_context = lambda job, snapshot: {"job": job, "source_hash": digest(snapshot)}
        self.calls = []
        self.add_job("new")

    def tearDown(self):
        self.clock.stop()
        self.reviews.close()
        self.queue.close()
        self.temporary.cleanup()

    def configure(self, enabled=True, since=None):
        self.config.write_text(json.dumps({"sharing": {
            "automatic_calendar_suggestions": enabled,
            "automatic_calendar_suggestions_since": self.SINCE if since is None else since}}))

    def add_job(self, job, created=None, status="succeeded"):
        with self.queue.db:
            self.queue.db.execute("INSERT INTO jobs(id,session,created) VALUES (?,?,?)",
                                  (job, str(self.state / (job + "-recording")), self.SINCE + 1 if created is None else created))
            self.queue.db.execute("INSERT INTO tasks(job,stage,status,settings) VALUES (?,?,?,?)",
                                  (job, "publish", status, "{}"))

    def request(self, job="new", since=None):
        return self.reviews.request_automatic(job, self.SINCE if since is None else since)

    def invoke(self, tool, arguments):
        self.calls.append((tool, arguments))
        return {}

    def suggest(self, context, invoke):
        invoke("list_calendars", {"max_results": 20})
        return dict(context, status="ready", events=[], warnings=[], calendars=[])

    def run_lookup(self, record, invoke=None, suggest=None):
        return self.reviews.run_suggestions(record["job"], record["revision"],
                                            invoke or self.invoke, suggest or self.suggest)

    def test_only_new_published_jobs_are_enrolled_without_any_calendar_read(self):
        self.add_job("older", created=self.SINCE - 1)
        self.add_job("unpublished", status="pending")
        for job in ("older", "unpublished", "missing"):
            self.assertIsNone(self.request(job))
            self.assertIsNone(self.reviews.get(job))
        enrolled = self.request()
        self.assertEqual(enrolled["status"], "available")
        self.assertEqual(enrolled["suggestion_status"], "searching")
        self.assertEqual(enrolled["automatic_suggestion"]["state"], "pending")
        self.assertEqual(enrolled["recipients"], [])
        self.assertNotIn("recipient_draft", enrolled)
        self.assertNotIn("approval", enrolled)
        self.assertEqual(enrolled["journal"], {})
        self.assertEqual(self.calls, [])
        self.assertTrue(self.reviews.pending_automatic(self.SINCE))

    def test_activation_boundary_is_inclusive(self):
        self.add_job("boundary", created=self.SINCE)
        self.assertIsNotNone(self.request("boundary"))

    def test_disabled_or_different_activation_cannot_enroll_or_dispatch(self):
        self.configure(enabled=False)
        self.assertIsNone(self.request())
        self.assertFalse(self.reviews.pending_automatic(self.SINCE))
        self.configure(since=self.SINCE + 10)
        self.assertIsNone(self.request())
        self.assertIsNone(self.reviews.get("new"))
        self.configure()
        self.assertIsNone(self.request(since=self.SINCE - 1))
        self.assertEqual(self.calls, [])

    def test_invalid_activation_values_never_create_reviews(self):
        for since in (True, False, 0, -1, float("nan"), float("inf"), "1000"):
            with self.subTest(since=since), self.assertRaises(WorkflowError):
                self.reviews.request_automatic("new", since)
        self.assertIsNone(self.reviews.get("new"))

    def test_dispatch_retry_is_delayed_bounded_and_invalidates_old_worker(self):
        first = self.request()
        self.assertEqual(first["automatic_suggestion"]["dispatch_attempts"], 1)
        self.assertEqual(first["automatic_suggestion"]["next_dispatch_at"], self.now + 15)
        self.reviews.automatic_launch_failed("new", first["revision"])
        self.now += 14
        self.assertIsNone(self.request())
        self.now += 1
        second = self.request()
        self.assertEqual(second["automatic_suggestion"]["dispatch_attempts"], 2)
        self.assertEqual(second["automatic_suggestion"]["next_dispatch_at"], self.now + 60)
        with self.assertRaises(WorkflowError):
            self.run_lookup(first)
        self.now += 59
        self.assertIsNone(self.request())
        self.now += 1
        third = self.request()
        self.assertEqual(third["automatic_suggestion"]["dispatch_attempts"], 3)
        self.now = third["automatic_suggestion"]["next_dispatch_at"]
        self.assertIsNone(self.request())
        self.assertEqual(self.reviews.get("new")["suggestion_status"], "needs_attention")
        self.assertEqual(self.reviews.get("new")["automatic_suggestion"]["state"], "failed")
        self.assertFalse(self.reviews.pending_automatic(self.SINCE))
        self.assertEqual(self.calls, [])

    def test_started_is_persisted_before_first_calendar_read_and_results_do_not_replay(self):
        request = self.request()
        def checked(tool, arguments):
            saved = self.reviews.get("new")
            self.assertEqual(saved["automatic_suggestion"]["state"], "started")
            self.assertNotIn("approval", saved)
            self.assertEqual(saved["recipients"], [])
            return self.invoke(tool, arguments)
        result = self.run_lookup(request, invoke=checked)
        self.assertEqual(result["suggestion_status"], "ready")
        self.assertTrue(result["suggestion_automatic"])
        self.assertEqual(len(self.calls), 1)
        self.now += 1000
        self.assertIsNone(self.request())
        self.assertFalse(self.reviews.pending_automatic(self.SINCE))
        self.assertEqual(self.reviews.get("new"), result)

    def test_failure_after_first_read_requires_manual_retry(self):
        request = self.request()
        def offline(tool, arguments):
            self.invoke(tool, arguments)
            raise RuntimeError("Synthetic calendar offline")
        result = self.run_lookup(request, invoke=offline)
        self.assertEqual(result["suggestion_status"], "needs_attention")
        self.assertEqual(result["automatic_suggestion"]["state"], "started")
        self.now += 1000
        self.assertIsNone(self.request())
        self.assertFalse(self.reviews.pending_automatic(self.SINCE))
        self.assertEqual(len(self.calls), 1)
        manual = self.reviews.decide("sharing_suggest", "new", result["revision"])
        self.assertEqual(manual["automatic_suggestion"]["state"], "cancelled")
        self.assertFalse(manual["suggestion_automatic"])

    def test_interrupted_started_worker_is_not_automatically_replayed(self):
        request = self.request()
        def interrupted(tool, arguments):
            self.invoke(tool, arguments)
            raise SystemExit("Synthetic worker interruption")
        with self.assertRaises(SystemExit):
            self.run_lookup(request, invoke=interrupted)
        self.assertEqual(self.reviews.get("new")["automatic_suggestion"]["state"], "started")
        self.now += 1000
        self.assertEqual(self.reviews.card("new")["suggestion_status"], "needs_attention")
        self.assertIsNone(self.request())
        self.assertEqual(len(self.calls), 1)

    def test_configuration_revoke_before_lookup_prevents_all_reads(self):
        request = self.request()
        self.configure(enabled=False)
        result = self.run_lookup(request)
        self.assertEqual(result["suggestion_status"], "needs_attention")
        self.assertEqual(self.calls, [])
        self.assertFalse(self.reviews.pending_automatic(self.SINCE))

    def test_configuration_change_between_reads_stops_the_next_read(self):
        request = self.request()
        def changed(context, invoke):
            invoke("list_calendars", {"max_results": 20})
            self.configure(since=self.SINCE + 10)
            invoke("read_event", {"calendar_id": "primary", "event_id": "event-one"})
            return dict(context, events=[], warnings=[], calendars=[])
        result = self.run_lookup(request, suggest=changed)
        self.assertEqual(result["suggestion_status"], "needs_attention")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("approval", result)
        self.assertEqual(result["recipients"], [])

    def test_existing_manual_private_ready_and_shared_reviews_are_never_overwritten(self):
        for status in ("available", "private", "ready", "shared"):
            job = "existing-" + status
            self.add_job(job)
            record = self.reviews._new(job, self.source)
            record.update(status=status, suggestion_status="ready", recipients=[] if status == "available" else ["chosen@example.com"])
            if status == "shared":
                record["approval"] = {"recipients": ["chosen@example.com"]}
            self.reviews.put(record)
            before = self.reviews.get(job)
            self.assertIsNone(self.request(job))
            self.assertEqual(self.reviews.get(job), before)
        self.assertEqual(self.calls, [])

    def test_manual_choices_cancel_pending_dispatch_and_preserve_recipients(self):
        for action in ("sharing_prepare", "sharing_private", "sharing_suggest"):
            job = "manual-" + action
            self.add_job(job)
            request = self.request(job)
            decision_revision = request["revision"]
            if action == "sharing_suggest":
                # A lookup owns the short dispatch window. Explicit retry is
                # offered after the pending worker fails to start in time.
                with self.assertRaises(WorkflowError):
                    self.reviews.decide(action, job, decision_revision)
                self.now = request["automatic_suggestion"]["next_dispatch_at"] + 91
                decision_revision = self.reviews.card(job)["revision"]
            kwargs = {"emails": "manual@example.com"} if action == "sharing_prepare" else {}
            chosen = self.reviews.decide(action, job, decision_revision, **kwargs)
            self.assertEqual(chosen["automatic_suggestion"]["state"], "cancelled")
            if action == "sharing_prepare":
                self.assertEqual(chosen["recipients"], ["manual@example.com"])
            with self.assertRaises(WorkflowError):
                self.run_lookup(request)
            self.now += 1000
            self.assertIsNone(self.request(job))
            self.assertEqual(self.reviews.get(job), chosen)
        self.assertEqual(self.calls, [])

    def test_stale_pending_card_exposes_failure_only_after_deadline_without_active_lock(self):
        request = self.request()
        deadline = request["automatic_suggestion"]["next_dispatch_at"] + 90
        self.now = deadline - 1
        self.assertEqual(self.reviews.card("new")["suggestion_status"], "searching")
        self.now = deadline + 1
        with self.reviews.locked("new"):
            self.assertEqual(self.reviews.card("new")["suggestion_status"], "searching")
        card = self.reviews.card("new")
        self.assertEqual(card["suggestion_status"], "needs_attention")
        self.assertFalse(self.reviews.pending_automatic(self.SINCE))
        self.assertEqual(self.calls, [])

    def test_removed_or_unpublished_jobs_stop_pending_recovery(self):
        self.request()
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='held' WHERE job='new' AND stage='publish'")
        self.assertFalse(self.reviews.pending_automatic(self.SINCE))
        self.assertIsNone(self.request())
        with self.queue.db:
            self.queue.db.execute("DELETE FROM tasks WHERE job='new'")
            self.queue.db.execute("DELETE FROM jobs WHERE id='new'")
        self.assertFalse(self.reviews.pending_automatic(self.SINCE))
        self.assertIsNone(self.request())

    def test_changed_source_stops_dispatch_retries_without_calendar_read(self):
        request = self.request()
        self.source["preview"] = "Changed after enrollment"
        self.now = request["automatic_suggestion"]["next_dispatch_at"]
        self.assertIsNone(self.request())
        self.assertEqual(self.reviews.get("new")["suggestion_status"], "needs_attention")
        self.assertFalse(self.reviews.pending_automatic(self.SINCE))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
