import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.pipeline import WorkflowError
from quill_workflow.sharing import SharingReviews, digest


class SharingSuggestionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.source = {"title": "Synthetic notes", "preview": "A short summary", "owner": "owner@example.com"}
        self.reviews = SharingReviews(self.state, self.state / "config.json", lambda job: copy.deepcopy(self.source))
        self.reviews._suggestion_context = lambda job, snapshot: {"job": job, "source_hash": digest(snapshot)}
        self.event = {"suggestion_id": "a" * 64, "title": "Synthetic invite", "start": "2026-10-09T11:00:00Z",
                      "calendar_id": "primary", "event_id": "event-one", "match_reason": "Similar time",
                      "match_strength": "tentative", "recipients": [{"name": "Alex", "email": "alex@example.com"}]}
        self.calls = []
    def tearDown(self):
        self.reviews.close()
        self.temp.cleanup()
    def request(self):
        card = self.reviews.card("job")
        return self.reviews.decide("sharing_suggest", "job", card["revision"])
    def invoke(self, tool, args):
        self.calls.append((tool, args))
        return {}
    def suggest(self, context, invoke):
        invoke("list_calendars", {"max_results": 20})
        return dict(context, events=[copy.deepcopy(self.event)], warnings=[], calendars=["primary"])
    def found(self):
        request = self.request()
        return self.reviews.run_suggestions("job", request["revision"], self.invoke, self.suggest)
    def test_calendar_lookup_does_not_prepare_copy_or_choose_recipients(self):
        record = self.found()
        self.assertEqual(record["status"], "available")
        self.assertEqual(record["suggestion_status"], "ready")
        self.assertEqual(record["recipients"], [])
        self.assertNotIn("recipient_draft", record)
        self.assertNotIn("approval", record)
        self.assertEqual(record["journal"], {})
        self.assertEqual(self.calls, [("list_calendars", {"max_results": 20})])
    def test_default_calendar_transport_binds_the_meeting_document_owner(self):
        request = self.request()
        with patch("quill_workflow.codex_calendar_transport.CodexCalendarTransport") as transport:
            transport.return_value.timeout = 90
            transport.return_value.invoke.return_value = {}
            record = self.reviews.run_suggestions("job", request["revision"], suggest=self.suggest)
        self.assertEqual(record["suggestion_status"], "ready")
        self.assertEqual(transport.call_args.kwargs["account_email"], "owner@example.com")
        transport.return_value.invoke.assert_called_once_with("list_calendars", {"max_results": 20})
    def test_use_suggestions_only_prefills_editable_draft(self):
        found = self.found()
        record = self.reviews.decide("sharing_use_suggestions", "job", found["revision"], suggestion="a" * 64)
        self.assertEqual(record["recipient_draft"], "alex@example.com")
        self.assertEqual(record["status"], "available")
        self.assertEqual(record["recipients"], [])
        self.assertNotIn("approval", record)
        self.assertEqual(len(self.calls), 1)
        prepared = self.reviews.decide("sharing_prepare", "job", record["revision"], emails="alex@example.com, sam@example.com")
        self.assertEqual(prepared["recipients"], ["alex@example.com", "sam@example.com"])
        self.assertIsNone(prepared["recipient_draft"])
        self.assertNotIn("approval", prepared)
    def test_unknown_or_stale_suggestion_cannot_populate_addresses(self):
        found = self.found()
        with self.assertRaises(WorkflowError):
            self.reviews.decide("sharing_use_suggestions", "job", found["revision"], suggestion="b" * 64)
        self.source["preview"] = "Changed notes"
        with self.assertRaises(WorkflowError):
            self.reviews.decide("sharing_use_suggestions", "job", found["revision"], suggestion="a" * 64)
    def test_unsupported_calendar_operation_blocked_before_invoke(self):
        request = self.request()
        def bad(context, invoke):
            invoke("create_event", {})
        result = self.reviews.run_suggestions("job", request["revision"], self.invoke, bad)
        self.assertEqual(result["suggestion_status"], "needs_attention")
        self.assertEqual(self.calls, [])
    def test_lookup_deadline_blocks_further_calendar_calls(self):
        request = self.request()
        with patch("quill_workflow.sharing.time.monotonic", side_effect=[0, 91]):
            result = self.reviews.run_suggestions("job", request["revision"], self.invoke, self.suggest)
        self.assertEqual(result["suggestion_status"], "needs_attention")
        self.assertIn("time limit", result["suggestion_error"])
        self.assertEqual(self.calls, [])
    def test_changed_source_blocks_next_calendar_read_and_saved_result(self):
        request = self.request()
        def changed(context, invoke):
            invoke("list_calendars", {"max_results": 20})
            self.source["preview"] = "Changed"
            invoke("read_event", {"event_id": "one", "calendar_id": "primary"})
        result = self.reviews.run_suggestions("job", request["revision"], self.invoke, changed)
        self.assertEqual(result["suggestion_status"], "needs_attention")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["suggested_events"], [])
    def test_wrong_meeting_result_rejected(self):
        request = self.request()
        def wrong(context, invoke):
            return dict(context, job="another-job", events=[self.event])
        result = self.reviews.run_suggestions("job", request["revision"], self.invoke, wrong)
        self.assertEqual(result["suggestion_status"], "needs_attention")
        self.assertEqual(result["suggested_events"], [])
    def test_unavailable_calendar_is_not_claimed_as_no_match(self):
        request = self.request()
        def unavailable(context, invoke):
            return dict(context, status="unavailable", events=[], warnings=["Calendar could not be read"])
        result = self.reviews.run_suggestions("job", request["revision"], self.invoke, unavailable)
        self.assertEqual(result["suggestion_status"], "needs_attention")
        self.assertIn("unavailable", result["suggestion_error"])
    def test_lookup_crash_recovery_can_search_again(self):
        request = self.request()
        with patch("quill_workflow.sharing.time.time", return_value=request["updated"] + 100):
            card = self.reviews.card("job")
        self.assertEqual(card["status"], "available")
        self.assertEqual(card["suggestion_status"], "needs_attention")
        self.assertEqual(self.request()["suggestion_status"], "searching")
    def test_active_lookup_not_marked_crashed(self):
        request = self.request()
        with self.reviews.locked("job"), patch("quill_workflow.sharing.time.time", return_value=request["updated"] + 100):
            self.assertEqual(self.reviews.card("job")["suggestion_status"], "searching")
    def test_available_manual_prepare_invalidates_delayed_calendar_worker(self):
        request = self.request()
        prepared = self.reviews.decide("sharing_prepare", "job", request["revision"], "manual@example.com")
        self.assertEqual(prepared["suggestion_status"], "idle")
        with self.assertRaises(WorkflowError):
            self.reviews.run_suggestions("job", request["revision"], self.invoke, self.suggest)
    def test_ready_suggestion_must_be_saved_before_confirmation(self):
        found = self.found()
        found.update(status="ready", draft={"document_id": "synthetic-private-copy"}, recipients=["old@example.com"])
        self.reviews.put(found)
        record = self.reviews.decide("sharing_use_suggestions", "job", found["revision"], suggestion="a" * 64)
        self.assertEqual(record["recipients"], ["old@example.com"])
        with self.assertRaisesRegex(WorkflowError, "Save and review"):
            self.reviews.decide("sharing_confirm", "job", record["revision"])
        saved = self.reviews.decide("sharing_prepare", "job", record["revision"], "alex@example.com")
        confirmed = self.reviews.decide("sharing_confirm", "job", saved["revision"])
        self.assertEqual(confirmed["approval"]["recipients"], ["alex@example.com"])
    def test_no_calendar_lookup_after_sharing_approved(self):
        found = self.found()
        found.update(status="sharing", approval={"recipients": ["alex@example.com"]})
        self.reviews.put(found)
        with self.assertRaises(WorkflowError):
            self.reviews.decide("sharing_suggest", "job", found["revision"])

    def context_sources(self, meeting=None):
        metadata = {"started": "2026-10-09T11:00:00Z", "ended": "2026-10-09T12:00:00Z"}
        if meeting is not None:
            metadata["meeting_context"] = meeting
        notes = {"meeting_title": {"text": "Synthetic meeting", "participants": [{"name": "Mentioned Person"}]}}
        self.source["binding"] = {}
        for relative, value, key in (("recordings/job/meta.json", metadata, "metadata_sha256"),
                                     ("results/job/notes/notes.json", notes, "notes_sha256")):
            target = self.state / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps(value).encode()
            target.write_bytes(data)
            self.source["binding"][key] = hashlib.sha256(data).hexdigest()
        return SharingReviews._suggestion_context(self.reviews, "job", self.source)

    def test_context_passes_frozen_meeting_key_and_observed_names_separately(self):
        meeting = {"provider": "google-meet", "key": "abc-defg-hij", "coverage": "visible_only",
                   "participants": [{"name": "Casey Rivera", "self": False}, {"name": "Owner Name", "self": True}]}
        context = self.context_sources(meeting)
        self.assertEqual(context["meeting"], {"provider": "google-meet", "key": "abc-defg-hij"})
        self.assertEqual(context["observed_participants"], meeting["participants"])
        self.assertEqual(context["participants"], ["Mentioned Person", "Casey Rivera"])
        self.assertEqual(context["participant_coverage"], "visible_only")

    def test_historical_context_does_not_invent_participant_evidence(self):
        context = self.context_sources()
        self.assertNotIn("meeting", context)
        self.assertNotIn("observed_participants", context)

    def test_invalid_present_context_and_changed_metadata_are_not_used(self):
        with self.assertRaises(ValueError):
            self.context_sources({"provider": "google-meet", "key": "full-url", "coverage": "visible_only", "participants": []})
        self.context_sources()
        (self.state / "recordings/job/meta.json").write_text("{}")
        with self.assertRaisesRegex(WorkflowError, "changed"):
            SharingReviews._suggestion_context(self.reviews, "job", self.source)


if __name__ == "__main__":
    unittest.main()
