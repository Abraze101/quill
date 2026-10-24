"""A meeting gets one primary action without weakening saved approvals."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.review_server import _review_entries, _reviews_page, create_server
from quill_workflow.routing import RoutingReviews


class ReviewStateResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.processing, self.sharing, self.deletion = [], [], []
        self.resume, self.calls = {}, []
        self.server = create_server(
            self.state,
            on_decision=lambda *args: self.calls.append(args),
            resume_options=lambda record: self.resume.get(record["job"], []),
            on_resume=lambda *args: self.calls.append(args),
            processing_options=lambda: self.processing,
            on_retry=lambda *args: self.calls.append(args),
            sharing_options=lambda: self.sharing,
            on_sharing=lambda *args, **kwargs: self.calls.append((args, kwargs)),
            deletion_options=lambda: self.deletion,
            on_delete=lambda *args, **kwargs: self.calls.append((args, kwargs)),
        )

    def tearDown(self):
        self.server.server_close()
        self.temporary.cleanup()

    def notes(self, text="# Saved meeting\n\nPrivate notes remain readable.", job="meeting"):
        path = self.state / "results" / job / "notes" / "notes.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return text

    def routing(self, approved=False):
        reviews = RoutingReviews(self.state)
        try:
            record = reviews.propose(
                "meeting", b'{"segments":[{"text":"This is a company meeting."}]}',
                [{"id": "company", "name": "Company", "drive_folder_id": "folder-company-123"}],
                {"schema_version": 1, "company_id": "company", "confidence": "high",
                 "reason": "Company named directly.", "evidence": [0], "ambiguous": False},
            )
            if approved:
                record = reviews.approve("meeting", "company", record["revision"])
            return record
        finally:
            reviews.close()

    def processing_item(self, state, retryable=False):
        return {"job": "meeting", "revision": "processing-revision", "title": "Saved meeting",
                "state": state, "stage": "archive", "detail": "The previous upload needs verification.",
                "retryable": retryable}

    def sharing_item(self, status="available"):
        return {"job": "meeting", "revision": "sharing-revision", "title": "Saved meeting",
                "status": status, "preview": "# Shareable notes\n\nExact notes-only excerpt.",
                "draft_url": "https://docs.google.com/document/d/example-copy/edit",
                "recipients": ["alex@example.com"], "cancellable": status != "shared",
                "recipient_draft": None}

    def entry(self):
        entries = _review_entries(self.server)
        self.assertEqual(len(entries), 1)
        self.assertEqual(self.calls, [])
        entry = entries[0]
        self.assertNotIn("panel-header", entry["body"])
        self.assertNotIn("<h2", entry["body"])
        return entry

    def test_failed_upload_and_resumable_work_have_one_status_and_retry_action(self):
        self.routing(approved=True)
        self.resume["meeting"] = ["archive", "publish"]
        self.processing = [self.processing_item("needs_attention", retryable=True)]
        saved = self.notes()
        entry = self.entry()
        self.assertEqual(entry["status"], "Needs attention")
        self.assertTrue(entry["attention"])
        self.assertIn("previous upload needs verification", entry["detail"])
        self.assertEqual(entry["body"].count('value="retry"'), 1)
        self.assertNotIn('value="resume"', entry["body"])
        self.assertEqual(entry["notes"], saved)
        self.assertEqual(entry["job"], "meeting")
        self.assertEqual((entry["folder_id"], entry["folder_name"]), ("company", "Company"))

    def test_active_work_never_exposes_resume_even_when_adapter_lists_pending_stages(self):
        self.routing(approved=True)
        self.resume["meeting"] = ["archive", "publish"]
        for state, label in (("running", "Processing"), ("retrying", "Waiting to retry"), ("queued", "Waiting")):
            with self.subTest(state=state):
                self.processing = [self.processing_item(state, retryable=True)]
                entry = self.entry()
                self.assertEqual(entry["status"], label)
                self.assertEqual(entry["body"], "")

    def test_active_work_keeps_concurrent_warning_but_offers_no_second_action(self):
        self.processing = [self.processing_item("running"), self.processing_item("needs_attention", True)]
        self.processing[0]["detail"] = "Uploading the notes."
        entry = self.entry()
        self.assertEqual(entry["status"], "Processing")
        self.assertIn("Uploading the notes.", entry["detail"])
        self.assertIn("previous upload needs verification", entry["detail"])
        self.assertTrue(entry["attention"])
        self.assertEqual(entry["body"], "")

    def test_unknown_processing_state_does_not_become_ready_to_resume(self):
        self.routing(approved=True)
        self.resume["meeting"] = ["publish"]
        self.processing = [self.processing_item("unknown")]
        entry = self.entry()
        self.assertEqual(entry["status"], "Check status")
        self.assertTrue(entry["attention"])
        self.assertEqual(entry["body"], "")

    def test_pending_folder_actions_wait_until_active_or_failed_processing_is_resolved(self):
        self.routing()
        for state in ("running", "retrying", "queued", "needs_attention", "unknown"):
            with self.subTest(state=state):
                self.processing = [self.processing_item(state)]
                entry = self.entry()
                self.assertNotIn('value="approve"', entry["body"])
                self.assertNotIn('value="skip"', entry["body"])

    def test_matching_folder_wait_has_one_approval_form_and_no_duplicate_notes(self):
        record = self.routing()
        self.processing = [self.processing_item("waiting_for_company")]
        saved = self.notes()
        entry = self.entry()
        self.assertEqual(entry["status"], "Choose a folder")
        self.assertEqual(entry["body"].count("<form"), 1)
        self.assertEqual(entry["body"].count('value="approve"'), 1)
        self.assertEqual(entry["body"].count('value="skip"'), 1)
        self.assertIn(record["revision"], entry["body"])
        self.assertNotIn("Private notes remain readable", entry["body"])
        self.assertEqual(entry["notes"], saved)
        self.assertNotIn("folder_id", entry)
        self.assertEqual(entry["companies"], record["companies"])
        self.assertTrue(entry["body"].startswith('<details><summary>Choose folder</summary>'))
        self.assertNotIn("<details open", entry["body"])

    def test_resumable_alone_has_only_exact_saved_resume_action(self):
        record = self.routing(approved=True)
        self.resume["meeting"] = ["archive", "publish"]
        saved = self.notes()
        entry = self.entry()
        self.assertEqual(entry["status"], "Ready to resume")
        self.assertEqual(entry["body"].count('value="resume"'), 1)
        self.assertIn('name="stages" value="archive,publish"', entry["body"])
        self.assertIn(record["revision"], entry["body"])
        self.assertEqual(entry["notes"], saved)

    def test_saved_approval_can_resume_after_route_release_callback_was_interrupted(self):
        record = self.routing(approved=True)
        self.resume["meeting"] = ["route", "archive", "publish"]
        self.processing = [self.processing_item("waiting_for_company")]
        self.processing[0]["detail"] = "Choose this meeting's company folder"
        entry = self.entry()
        self.assertEqual(entry["status"], "Ready to resume")
        self.assertEqual(entry["body"].count('value="resume"'), 1)
        self.assertIn('name="stages" value="route,archive,publish"', entry["body"])
        self.assertIn(record["revision"], entry["body"])
        self.assertNotIn('value="approve"', entry["body"])
        self.assertNotIn('value="retry"', entry["body"])
        self.assertNotIn("unavailable", entry["detail"])

        self.resume.clear()
        orphan = self.entry()
        self.assertEqual(orphan["status"], "Check status")
        self.assertEqual(orphan["body"], "")

    def test_completed_sharing_keeps_saved_notes_visible_and_uses_state_heading(self):
        self.sharing = [self.sharing_item("shared")]
        saved = self.notes()
        entry = self.entry()
        self.assertEqual(entry["status"], "Notes shared")
        self.assertEqual(entry["notes"], saved)
        self.assertEqual(entry["body"], "")
        self.assertFalse(entry["attention"])
        self.assertIn("Sharing details", entry["sharing_body"])
        self.assertNotIn("Share meeting notes?", entry["sharing_body"])
        self.assertNotIn('value="sharing_confirm"', entry["sharing_body"])

    def test_sharing_preview_is_fallback_for_reading_and_exact_source_for_confirmation(self):
        self.sharing = [self.sharing_item("ready")]
        entry = self.entry()
        self.assertEqual(entry["notes"], self.sharing[0]["preview"])
        saved = self.notes("# Private notes\n\nINTERNAL CONTEXT NOT SHARED")
        entry = self.entry()
        self.assertEqual(entry["notes"], saved)
        self.assertIn("Exact notes-only excerpt.", entry["sharing_body"])
        self.assertNotIn("INTERNAL CONTEXT NOT SHARED", entry["sharing_body"])
        self.assertIn('value="sharing_confirm"', entry["sharing_body"])
        self.assertIn('name="revision" value="sharing-revision"', entry["sharing_body"])
        self.assertNotIn("review-layout", entry["sharing_body"])
        self.sharing[0]["draft_url"] = "https://outside.example/copy"
        self.assertNotIn('value="sharing_confirm"', self.entry()["sharing_body"])
        self.sharing[0]["draft_url"] = "https://docs.google.com/document/d/example-copy/edit"
        self.sharing[0]["recipient_draft"] = "taylor@example.com"
        self.assertNotIn('value="sharing_confirm"', self.entry()["sharing_body"])

    def test_optional_sharing_is_not_attention_but_confirmation_and_failures_are(self):
        for status, attention in (("available", False), ("private", False), ("shared", False), ("ready", True), ("needs_attention", True)):
            with self.subTest(status=status):
                self.sharing = [self.sharing_item(status)]
                self.assertEqual(self.entry()["attention"], attention)

    def test_local_deletion_inventory_does_not_claim_a_cloud_or_sharing_result(self):
        self.deletion = [{"job": "meeting", "revision": "deletion-revision", "title": "Historical recording", "available": True}]
        entry = self.entry()
        self.assertEqual(entry["status"], "On this Mac")
        self.assertIn("not established", entry["detail"])
        self.assertNotIn("Saved in Drive", str(entry))
        self.assertNotIn("Notes shared", str(entry))
        self.assertEqual(entry["sharing_body"], "")
        self.assertEqual(entry["sharing_status"], "unknown")
        self.assertEqual(entry["sharing_label"], "No sharing confirmation available")
        self.assertIn('value="delete_prepare"', entry["deletion_body"])

    def test_metadata_does_not_infer_a_local_folder_or_sharing_confirmation(self):
        self.deletion = [{"job": "meeting", "revision": "deletion-revision", "title": "Saved meeting", "available": True}]
        self.assertNotIn("local_folder", self.entry())
        self.deletion[0]["folder"] = "/temporary/demo/meeting"
        self.assertEqual(self.entry()["local_folder"], "/temporary/demo/meeting")
        for status, label in (
            ("available", "Not shared through Quill"),
            ("ready", "Private copy awaiting approval"),
            ("sharing", "Sharing started; confirmation pending"),
            ("shared", "View-only sharing confirmed"),
            ("private", "Kept private; no sharing scheduled"),
            ("needs_attention", "Sharing result needs review"),
        ):
            with self.subTest(status=status):
                self.sharing = [self.sharing_item(status)]
                entry = self.entry()
                self.assertEqual(entry["sharing_status"], status)
                self.assertEqual(entry["sharing_label"], label)

    def test_removal_overrides_stale_workflow_and_sharing_actions(self):
        self.routing(approved=True)
        self.resume["meeting"] = ["publish"]
        self.processing = [self.processing_item("needs_attention", True)]
        self.sharing = [self.sharing_item("ready")]
        for flag, label in (("cancelling", "Stopping processing…"), ("interrupted", "Removal needs attention")):
            with self.subTest(flag=flag):
                self.deletion = [{"job": "meeting", "revision": "deletion-revision", "title": "Saved meeting", "available": True, flag: True}]
                entry = self.entry()
                self.assertEqual(entry["status"], label)
                self.assertEqual(entry["body"], "")
                self.assertEqual(entry["sharing_body"], "")
                self.assertNotIn('value="retry"', entry["deletion_body"])
                if flag == "interrupted":
                    self.assertIn('value="delete_prepare"', entry["deletion_body"])

    def test_workspace_receives_one_normalized_meeting_without_prebuilt_headers(self):
        self.routing(approved=True)
        self.resume["meeting"] = ["publish"]
        self.processing = [self.processing_item("needs_attention", True)]
        with patch("quill_workflow.review_server.workspace", return_value="rendered") as render:
            self.assertEqual(_reviews_page(self.server), "rendered")
        entries = render.call_args.args[0]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "Needs attention")
        self.assertNotIn("Ready to resume", str(entries[0]))
        self.assertNotIn("panel-header", str(entries[0]))
        self.assertEqual(self.calls, [])

    def test_library_omits_cleanup_stubs_but_keeps_content_and_confirmed_saved_copies(self):
        empty = {"title": None, "display_date": None, "recorded_at": None,
                 "participants": [], "transcript_text": None, "recording_files": [],
                 "local_folder": "/missing/old-meeting", "storage_status": "not_confirmed",
                 "storage": {"document": {"status": "unknown"}, "media": {"status": "unknown"}}}
        library = {"cleanup-" + str(index): dict(empty) for index in range(18)}
        library.update({
            "titled": dict(empty, title="Saved notes"),
            "dated": dict(empty, display_date="10/20/26"),
            "transcript": dict(empty, transcript_text="Alex Morgan: Review the page."),
            "recording": dict(empty, recording_files=[{"kind": "mic", "label": "Microphone audio", "name": "mic.caf"}]),
            "filed": dict(empty, storage_status="saved_in_drive",
                          storage={"document": {"status": "confirmed", "file_ids": ["saved-file"]}}),
            "partly-filed": dict(empty, storage_status="upload_uncertain",
                                 storage={"document": {"status": "confirmed", "file_ids": ["saved-file"]},
                                          "media": {"status": "uncertain"}}),
        })
        with patch("quill_workflow.review_server.meeting_library", return_value=library), \
                patch("quill_workflow.review_server.workspace", return_value="rendered") as render:
            self.assertEqual(_reviews_page(self.server), "rendered")
        entries = render.call_args.args[0]
        self.assertEqual({entry["job"] for entry in entries},
                         {"titled", "dated", "transcript", "recording", "filed", "partly-filed"})
        self.assertEqual(self.calls, [])

    def test_empty_library_metadata_never_removes_normalized_workflow_entries(self):
        self.routing()
        self.processing = [dict(self.processing_item("running"), job="processing")]
        self.deletion = [{"job": "deletion", "revision": "deletion-revision", "title": "Removal pending",
                          "available": True, "interrupted": True}]
        empty = {"title": None, "display_date": None, "transcript_text": None, "recording_files": [],
                 "storage_status": "not_confirmed", "storage": {"document": {"status": "unknown"}}}
        library = {job: dict(empty) for job in ("meeting", "processing", "deletion")}
        with patch("quill_workflow.review_server.meeting_library", return_value=library), \
                patch("quill_workflow.review_server.workspace", return_value="rendered") as render:
            self.assertEqual(_reviews_page(self.server), "rendered")
        entries = {entry["job"]: entry for entry in render.call_args.args[0]}
        self.assertEqual(set(entries), set(library))
        self.assertEqual(entries["meeting"]["status"], "Choose a folder")
        self.assertEqual(entries["processing"]["status"], "Processing")
        self.assertEqual(entries["deletion"]["status"], "Removal needs attention")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
