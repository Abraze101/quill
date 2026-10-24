"""Deterministic Drive filing checks; no model, network, or actual recordings."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.drive_upload import (
    COMBINED_TEXT_FORMAT, DOCUMENT_MIME, DriveUploader, FOLDER_MIME, MAX_FILE_BYTES,
    UploadNeedsReview, UploadRetryable, _folder_name, inventory, native_document_text, file_identity,
)
from quill_workflow.pipeline import RecoverableUploadError, WorkflowError, atomic_json
from quill_workflow.google_drive_auth import DriveAuthNeedsConfiguration, DriveAuthRetryable
from quill_workflow.codex_drive_transport import DriveOperationError, DriveProtocolError, DriveTimeoutError


@contextmanager
def local_timezone(name):
    try:
        with patch.dict(os.environ, {"TZ": name}):
            time.tzset()
            yield
    finally:
        time.tzset()


class SimulatedTransportFailure(RuntimeError):
    def __init__(self, *, outcome_unknown=True):
        super().__init__("Simulated connection interruption")
        self.outcome_unknown = outcome_unknown


class FakeDrive:
    ROOT = "meetings_root_123"
    COMPANY = "company_folder_123"
    OWNER = "owner@example.test"

    def __init__(self):
        self.items = {}
        self.calls = []
        self.counter = 0
        self.before = None
        self.after_write = None
        self.search_override = None
        self.list_override = None
        self.document_text = ["Meeting notes", "Full transcript", "Synthetic complete transcript."]
        self.add(self.ROOT, "Meetings", "root", FOLDER_MIME)
        self.add(self.COMPANY, "Acme", self.ROOT, FOLDER_MIME)

    def add(self, identity, title, parent, mime, size=None):
        self.items[identity] = {
            "id": identity, "title": title, "mime_type": mime,
            "parent_ids": [parent], "size": str(size) if size is not None else None,
            "url": "https://drive.google.com/drive/folders/" + identity,
            "shared": False, "current_user_can_share": True, "drive_id": None,
            "permissions": [{"type": "user", "role": "owner", "emailAddress": self.OWNER}],
            "source_visibility_status": "not_shared",
        }
        return identity

    @property
    def writes(self):
        return [(tool, args) for tool, args in self.calls if tool in {"create_folder", "upload_file", "import_document"}]

    @property
    def uploads(self):
        return [args for tool, args in self.writes if tool == "upload_file"]

    @staticmethod
    def search_target(arguments):
        assert arguments["best_effort_fetch"] is False
        assert arguments["topn"] == 100
        pattern = r"trashed = false and '((?:\\.|[^'])*)' in parents and name = '((?:\\.|[^'])*)'"
        match = re.match(pattern, arguments["special_filter_query_str"])
        assert match, arguments
        parent, name = (re.sub(r"\\(.)", r"\1", value) for value in match.groups())
        tail = arguments["special_filter_query_str"][match.end():]
        folder = bool(tail)
        if folder:
            assert tail == " and mimeType = '" + FOLDER_MIME + "'"
            assert arguments["item_type"] == "folder"
        return parent, name, folder

    def __call__(self, tool, arguments):
        arguments = copy.deepcopy(arguments)
        self.calls.append((tool, arguments))
        if self.before:
            self.before(tool, arguments)
        if tool == "get_file_metadata":
            return copy.deepcopy(self.items[arguments["fileId"]])
        if tool == "get_document":
            return {"documentId": arguments["document_id"], "tabs": [{"tabId": "t.0", "body": {"content": [
                {"paragraph": {"elements": [{"textRun": {"content": text + "\n"}}]}}
                for text in self.document_text]}}]}
        if tool == "search":
            parent, name, folder = self.search_target(arguments)
            if self.search_override:
                result = self.search_override(parent, name, folder, arguments)
                if result is not None:
                    return copy.deepcopy(result)
            return {"results": [{"id": item["id"]} for item in self.items.values()
                    if item["parent_ids"] == [parent] and item["title"] == name
                    and not item.get("trashed") and (item["mime_type"] == FOLDER_MIME if folder else
                                                    item["mime_type"] not in {"application/json", "audio/x-caf", "video/mp4"})],
                    "next_page_token": None}
        if tool == "list_folder":
            self.assert_listing_arguments(arguments)
            parent = arguments["url"].rsplit("/", 1)[1]
            if self.list_override:
                result = self.list_override(parent, arguments)
                if result is not None:
                    return copy.deepcopy(result)
            return {"files": [copy.deepcopy(item) for item in self.items.values()
                              if item["parent_ids"] == [parent] and not item.get("trashed")][:100]}
        self.counter += 1
        identity = "created_item_" + str(self.counter).zfill(8)
        if tool == "create_folder":
            self.add(identity, arguments["name"], arguments["parent_folder"], FOLDER_MIME)
        elif tool == "upload_file":
            path = Path(arguments["file_uri"])
            self.add(identity, arguments["file_name"], arguments["parent_folder_id"],
                     arguments["mime_type"], path.stat().st_size)
        elif tool == "import_document":
            self.add(identity, arguments["title"], arguments["parent_folder_id"], DOCUMENT_MIME)
        else:
            raise AssertionError("Unexpected operation: " + tool)
        if self.after_write:
            self.after_write(tool, arguments, identity)
        return {"success": True, "id": identity, "title": self.items[identity]["title"],
                "parent_id": self.items[identity]["parent_ids"][0],
                "mime_type": self.items[identity]["mime_type"], "url": self.items[identity]["url"]}

    @staticmethod
    def assert_listing_arguments(arguments):
        assert set(arguments) == {"url", "top_k"}
        assert arguments["top_k"] == 100
        assert re.fullmatch(r"https://drive\.google\.com/drive/folders/[A-Za-z0-9_-]+", arguments["url"])


class DriveUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "recording"
        self.source.mkdir()
        self.started = "2026-01-02T10:30:00Z"
        self.job = "synthetic-meeting-123"
        self.folder_name = "2026-01-02 10-30-00 UTC - " + self.job
        self.route = {"company_id": "acme", "company_name": "Acme", "drive_folder_id": FakeDrive.COMPANY}
        self.meta = {"started": self.started, "ended": "2026-01-02T10:31:00Z",
                     "files": {"mic": "mic.caf", "system": "system.caf"}}
        atomic_json(self.source / "meta.json", self.meta)
        (self.source / "mic.caf").write_bytes(b"synthetic microphone bytes")
        (self.source / "system.caf").write_bytes(b"synthetic system audio bytes")
        atomic_json(self.source / "transcript.json", {"segments": [{"text": "Synthetic transcript."}]})
        (self.source / "transcript.md").write_text("Synthetic transcript.")
        self.notes = self.root / "notes"
        self.notes.mkdir()
        (self.notes / "notes.md").write_text("A synthetic note.")
        atomic_json(self.notes / "notes.json", {"summary": []})
        self.drive = FakeDrive()
        self.uploader = DriveUploader(self.root / "state", self.drive, FakeDrive.ROOT)

    def tearDown(self):
        self.temp.cleanup()

    def upload(self, stage="archive", meeting_title=None):
        return self.uploader.upload(self.job, stage, self.notes if stage == "publish" else self.source,
                                    self.route, self.started, meeting_title=meeting_title)

    def test_recording_timezone_keeps_archive_publish_and_retries_on_one_date(self):
        self.started = "2026-09-28T05:12:41Z"
        title = "Portal planning - Alex"

        def upload(stage):
            return self.uploader.upload(self.job, stage, self.notes if stage == "publish" else self.source,
                self.route, self.started, meeting_title=title, meeting_timezone="Asia/Singapore")

        with local_timezone("America/Los_Angeles"):
            first = upload("archive")
        self.assertEqual(first["meeting_folder_name"], title + " - 9/28/26")
        with local_timezone("Pacific/Honolulu"):
            published = upload("publish")
            writes = copy.deepcopy(self.drive.writes)
            retried = upload("archive")
        self.assertEqual(published["meeting_folder_id"], first["meeting_folder_id"])
        self.assertEqual(retried["meeting_folder_id"], first["meeting_folder_id"])
        self.assertEqual(self.drive.writes, writes)
        with self.assertRaisesRegex(UploadNeedsReview, "inputs or destination changed"):
            self.uploader.upload(self.job, "archive", self.source, self.route, self.started,
                                 meeting_title=title, meeting_timezone="America/Los_Angeles")
        self.assertEqual(self.drive.writes, writes)

    def test_recorded_timezone_applies_to_untitled_new_meetings(self):
        self.started = "2026-09-28T05:12:41Z"
        with local_timezone("America/Los_Angeles"):
            result = self.uploader.upload(self.job, "archive", self.source, self.route, self.started,
                                          meeting_timezone="Asia/Singapore")
        self.assertEqual(result["meeting_folder_name"], "Meeting - 9/28/26 - " + self.job)

    def test_invalid_saved_timezone_does_not_reach_drive(self):
        with self.assertRaisesRegex(UploadNeedsReview, "timezone"):
            self.uploader.upload(self.job, "archive", self.source, self.route, self.started,
                                 meeting_title="Portal planning", meeting_timezone="Unknown/Place")
        self.assertEqual(self.drive.calls, [])
        self.assertFalse(self.uploader.state.exists())

    def combined(self, stage="publish", **kwargs):
        document = self.notes / "documents" / "meeting.docx"
        document.parent.mkdir(exist_ok=True)
        if not document.exists():
            document.write_bytes(b"Synthetic immutable DOCX bytes")
        options = dict(text_format=COMBINED_TEXT_FORMAT, document_path=document,
                       document_title="Portal planning - Alex — Notes and transcript",
                       document_text=["Meeting notes", "Full transcript", "Synthetic complete transcript."])
        options.update(kwargs)
        return self.uploader.upload(self.job, stage, self.notes if stage == "publish" else self.source,
                                    self.route, self.started, meeting_title="Portal planning - Alex", **options)

    def test_combined_format_archives_only_media_and_imports_one_native_doc(self):
        archive = self.combined("archive")
        published = self.combined()
        self.assertEqual([item["name"] for item in archive["files"]], ["mic.caf", "system.caf"])
        self.assertEqual([args["file_name"] for args in self.drive.uploads], ["mic.caf", "system.caf"])
        imports = [args for tool, args in self.drive.writes if tool == "import_document"]
        self.assertEqual(len(imports), 1)
        self.assertEqual(imports[0]["upload_mode"], "native_google_docs")
        self.assertEqual(len(published["files"]), 1)
        self.assertEqual(published["files"][0]["mime_type"], DOCUMENT_MIME)
        self.assertEqual(published["verification"], "metadata_and_document_text")
        self.assertEqual(archive["meeting_folder_id"], published["meeting_folder_id"])
        before = copy.deepcopy(self.drive.writes)
        self.combined()
        self.assertEqual(before, self.drive.writes)

    def test_combined_format_does_not_reinterpret_legacy_upload_journal(self):
        self.upload()
        before = copy.deepcopy(self.drive.writes)
        with self.assertRaisesRegex(UploadNeedsReview, "inputs or destination changed"):
            self.combined("archive")
        self.assertEqual(before, self.drive.writes)

    def test_native_import_missing_content_holds_with_id_and_retry_verifies_without_import(self):
        self.drive.document_text = ["Meeting notes", "Full transcript"]
        with self.assertRaisesRegex(UploadNeedsReview, "complete notes and transcript"):
            self.combined()
        before = copy.deepcopy(self.drive.writes)
        self.drive.document_text.append("Synthetic complete transcript.")
        result = self.combined()
        self.assertEqual(result["verification"], "metadata_and_document_text")
        self.assertEqual(before, self.drive.writes)

    def test_uncertain_import_requires_saved_id_and_never_adopts_name_match(self):
        def lost_response(tool, arguments, identity):
            if tool == "import_document":
                raise SimulatedTransportFailure()
        self.drive.after_write = lost_response
        with self.assertRaisesRegex(UploadNeedsReview, "document import has no saved Drive ID"):
            self.combined()
        before = copy.deepcopy(self.drive.writes)
        self.drive.after_write = None
        with self.assertRaisesRegex(UploadNeedsReview, "document import has no saved Drive ID"):
            self.combined()
        self.assertEqual(before, self.drive.writes)

    def test_known_native_import_failure_can_retry_and_source_change_holds(self):
        def rejected(tool, arguments):
            if tool == "import_document":
                self.drive.before = None
                raise SimulatedTransportFailure(outcome_unknown=False)
        self.drive.before = rejected
        with self.assertRaises(SimulatedTransportFailure):
            self.combined()
        self.combined()
        document = self.notes / "documents" / "meeting.docx"
        document.write_bytes(b"Changed source")
        with self.assertRaisesRegex(UploadNeedsReview, "inputs or destination changed"):
            self.combined()

    def test_native_doc_rejects_size_based_or_wrong_type_verification(self):
        def wrong_type(tool, arguments, identity):
            if tool == "import_document":
                self.drive.items[identity]["mime_type"] = "application/octet-stream"
        self.drive.after_write = wrong_type
        with self.assertRaisesRegex(UploadNeedsReview, "unexpected file type"):
            self.combined()

    def test_combined_format_rejects_missing_or_external_document_and_nonmedia_archive(self):
        with self.assertRaisesRegex(UploadNeedsReview, "text is required"):
            self.combined(document_text=None)
        outside = self.root / "other.docx"
        outside.write_bytes(b"outside")
        with self.assertRaisesRegex(UploadNeedsReview, "inside this job"):
            self.combined(document_path=outside)
        self.meta["files"]["mic"] = "transcript.json"
        atomic_json(self.source / "meta.json", self.meta)
        with self.assertRaisesRegex(UploadNeedsReview, "only audio and video"):
            self.combined("archive")
        self.assertEqual(self.drive.writes, [])

    def test_native_document_text_reads_tables_and_rejects_other_document_or_extra_tabs(self):
        doc = {"documentId": "doc123", "tabs": [{"body": {"content": [
            {"paragraph": {"elements": [{"textRun": {"content": "Meeting\n"}}]}},
            {"table": {"tableRows": [{"tableCells": [{"content": [
                {"paragraph": {"elements": [{"textRun": {"content": "Action\n"}}]}}]}]}]}}]}}]}
        self.assertEqual(native_document_text(doc, "doc123"), "Meeting Action")
        with self.assertRaisesRegex(UploadNeedsReview, "different document"):
            native_document_text(doc, "other")
        doc["tabs"].append(copy.deepcopy(doc["tabs"][0]))
        with self.assertRaisesRegex(UploadNeedsReview, "tab layout"):
            native_document_text(doc, "doc123")

    def test_descriptive_title_uses_short_unpadded_date_without_time_or_job_suffix(self):
        with local_timezone("America/Los_Angeles"):
            receipt = self.upload(meeting_title="Portal planning - Alex, Morgan")
        expected = "Portal planning - Alex, Morgan - 1/2/26"
        self.assertEqual(self.drive.items[receipt["meeting_folder_id"]]["title"], expected)
        self.assertEqual(receipt["meeting_folder_name"], expected)
        self.assertEqual(receipt["meeting_folder_url"], self.drive.items[receipt["meeting_folder_id"]]["url"])
        self.assertNotIn(self.job, expected)

    def test_descriptive_date_uses_local_calendar_day(self):
        self.started = "2026-09-15T01:30:00Z"
        with local_timezone("America/Los_Angeles"):
            receipt = self.upload(meeting_title="Portal planning - Alex")
        self.assertEqual(receipt["meeting_folder_name"], "Portal planning - Alex - 9/14/26")

    def test_shared_folder_name_is_frozen_across_stages_title_changes_and_timezones(self):
        for first_stage, next_stage in (("archive", "publish"), ("publish", "archive")):
            with self.subTest(first=first_stage), tempfile.TemporaryDirectory() as state:
                self.drive = FakeDrive()
                self.uploader = DriveUploader(state, self.drive, FakeDrive.ROOT)
                with local_timezone("America/Los_Angeles"):
                    first = self.upload(first_stage, meeting_title="Portal planning - Alex")
                with local_timezone("Asia/Singapore"):
                    second = self.upload(next_stage, meeting_title="Changed title - Morgan")
                    before = copy.deepcopy(self.drive.writes)
                    repeated = self.upload(first_stage)
                self.assertEqual(first["meeting_folder_id"], second["meeting_folder_id"])
                self.assertEqual(first["meeting_folder_name"], second["meeting_folder_name"])
                self.assertEqual(first["meeting_folder_name"], repeated["meeting_folder_name"])
                self.assertEqual(before, self.drive.writes)
                self.assertEqual(sum(args["parent_folder"] == FakeDrive.COMPANY
                                     for tool, args in self.drive.writes if tool == "create_folder"), 1)

    def test_existing_timestamped_titled_name_stays_frozen_across_stages(self):
        old_name = "2026-01-02 02-30-00 PST - Portal planning - Alex"
        for first_stage, next_stage in (("archive", "publish"), ("publish", "archive")):
            with self.subTest(first=first_stage), tempfile.TemporaryDirectory() as state:
                self.drive = FakeDrive()
                self.uploader = DriveUploader(state, self.drive, FakeDrive.ROOT)
                with patch("quill_workflow.drive_upload._folder_name", side_effect=lambda date, job, title=None, meeting_timezone=None:
                           old_name if title else _folder_name(date, job)):
                    first = self.upload(first_stage, meeting_title="Portal planning - Alex")
                second = self.upload(next_stage, meeting_title="Changed title - Morgan")
                before = copy.deepcopy(self.drive.writes)
                repeated = self.upload(first_stage, meeting_title="Changed title - Morgan")
                self.assertEqual(first["meeting_folder_name"], old_name)
                self.assertEqual(second["meeting_folder_name"], old_name)
                self.assertEqual(repeated["meeting_folder_name"], old_name)
                self.assertEqual(first["meeting_folder_id"], second["meeting_folder_id"])
                self.assertEqual(first["meeting_folder_id"], repeated["meeting_folder_id"])
                self.assertEqual(before, self.drive.writes)
                self.assertEqual(sum(args["parent_folder"] == FakeDrive.COMPANY
                                     for tool, args in self.drive.writes if tool == "create_folder"), 1)

    def test_legacy_journal_keeps_existing_name_when_notes_gain_a_title(self):
        archived = self.upload()
        meeting_path = self.uploader.state / "results" / self.job / "drive-meeting.json"
        meeting = json.loads(meeting_path.read_text())
        meeting.pop("folder_name")
        atomic_json(meeting_path, meeting)
        published = self.upload("publish", meeting_title="Portal planning - Alex")
        self.assertEqual(archived["meeting_folder_id"], published["meeting_folder_id"])
        self.assertEqual(published["meeting_folder_name"], self.folder_name)

    def test_unknown_titled_folder_write_holds_without_saved_remote_id(self):
        def interrupt_once(tool, arguments, identity):
            self.drive.after_write = None
            raise SimulatedTransportFailure()
        self.drive.after_write = interrupt_once
        with self.assertRaisesRegex(UploadNeedsReview, "no saved Drive ID"):
            self.upload(meeting_title="Portal planning - Alex")
        original_name = self.drive.writes[0][1]["name"]
        writes = copy.deepcopy(self.drive.writes)
        with self.assertRaisesRegex(UploadNeedsReview, "no saved Drive ID"):
            self.upload(meeting_title="Later notes - Morgan")
        self.assertEqual(self.drive.writes, writes)
        self.assertEqual(sum(item["title"] == original_name for item in self.drive.items.values()), 1)

    def test_unknown_no_effect_write_cannot_adopt_another_jobs_same_title_folder(self):
        title = "Portal planning - Alex"
        def lost_before_completion(tool, arguments):
            if tool == "create_folder":
                self.drive.before = None
                raise SimulatedTransportFailure()
        self.drive.before = lost_before_completion
        with self.assertRaisesRegex(UploadNeedsReview, "no saved Drive ID"):
            self.upload(meeting_title=title)
        self.assertEqual(len(self.drive.items), 2, "The first request never created a remote folder")
        second = self.uploader.upload("different-job-123", "archive", self.source,
                                      self.route, self.started, meeting_title=title)
        writes = copy.deepcopy(self.drive.writes)
        with self.assertRaisesRegex(UploadNeedsReview, "no saved Drive ID"):
            self.upload(meeting_title=title)
        self.assertEqual(self.drive.writes, writes)
        journal = json.loads((self.uploader.state / "results" / self.job / "drive-meeting.json").read_text())
        self.assertNotIn("id", journal["operation"])
        self.assertIn(second["meeting_folder_id"], self.drive.items)

    def test_titled_folder_with_saved_id_recovers_after_readback_interruption(self):
        def fail_after_acknowledgment(tool, arguments):
            if tool == "get_file_metadata" and arguments["fileId"].startswith("created_item_"):
                self.drive.before = None
                raise SimulatedTransportFailure()
        self.drive.before = fail_after_acknowledgment
        with self.assertRaises(SimulatedTransportFailure):
            self.upload(meeting_title="Portal planning - Alex")
        journal = json.loads((self.uploader.state / "results" / self.job / "drive-meeting.json").read_text())
        known_id = journal["operation"]["id"]
        receipt = self.upload(meeting_title="Portal planning - Alex")
        self.assertEqual(receipt["meeting_folder_id"], known_id)
        self.assertEqual(sum(args["parent_folder"] == FakeDrive.COMPANY
                             for tool, args in self.drive.writes if tool == "create_folder"), 1)

    def test_titled_folder_known_not_dispatched_can_retry_creation(self):
        def before_dispatch(tool, arguments):
            if tool == "create_folder":
                self.drive.before = None
                raise SimulatedTransportFailure(outcome_unknown=False)
        self.drive.before = before_dispatch
        with self.assertRaises(SimulatedTransportFailure):
            self.upload(meeting_title="Portal planning - Alex")
        receipt = self.upload(meeting_title="Portal planning - Alex")
        self.assertEqual(sum(item["title"] == receipt["meeting_folder_name"]
                             for item in self.drive.items.values()), 1)

    def test_other_job_cannot_adopt_a_same_name_folder(self):
        title = "Portal planning - Alex"
        self.upload(meeting_title=title)
        writes = copy.deepcopy(self.drive.writes)
        with self.assertRaisesRegex(UploadNeedsReview, "without this upload's receipt"):
            self.uploader.upload("different-job-123", "archive", self.source,
                                 self.route, self.started, meeting_title=title)
        self.assertEqual(self.drive.writes, writes)

    def test_title_display_is_bounded_unicode_and_single_line_with_safe_separators(self):
        title = "  Alex's\nplan/roadmap\\phase\x00two\u202e - " + "研究" * 100
        receipt = self.upload(meeting_title=title)
        name = receipt["meeting_folder_name"]
        self.assertIn("Alex's plan-roadmap-phase two -", name)
        self.assertLessEqual(len(name.encode("utf-8")), 200)
        self.assertRegex(name.rsplit(" - ", 1)[1], r"^\d{1,2}/\d{1,2}/\d{2}$")
        self.assertNotIn("/", name.rsplit(" - ", 1)[0])
        for unsafe in ("\n", "\x00", "\u202e", "\\", "\ufffd"):
            self.assertNotIn(unsafe, name)
        writes = copy.deepcopy(self.drive.writes)
        self.assertEqual(self.upload(meeting_title=title)["meeting_folder_id"], receipt["meeting_folder_id"])
        self.assertEqual(self.drive.writes, writes)

    def test_empty_title_uses_old_fallback_and_invalid_title_has_no_side_effects(self):
        with self.assertRaisesRegex(UploadNeedsReview, "title must be text"):
            self.upload(meeting_title={"text": "unvalidated input"})
        self.assertEqual(self.drive.calls, [])
        self.assertFalse(self.uploader.state.exists())
        receipt = self.upload(meeting_title="\n/\\ . \u202e")
        self.assertEqual(receipt["meeting_folder_name"], self.folder_name)

    def test_inventory_uses_finalized_manifest_and_fixed_notes_only(self):
        for directory in (self.source, self.notes):
            (directory / "source-transcript.json").write_text("Do not publish this")
            (directory / "provenance.json").write_text("Do not publish this")
            (directory / "private.log").write_text("Do not publish this")
            (directory / "unlisted.mp4").write_bytes(b"Unlisted media")
        self.meta["files"]["screen"] = "screen.mp4"
        atomic_json(self.source / "meta.json", self.meta)
        (self.source / "screen.mp4").write_bytes(b"Synthetic listed video")
        names = [item["name"] for item in inventory(self.source, "archive")]
        self.assertEqual(names, ["meta.json", "mic.caf", "screen.mp4", "system.caf",
                                 "transcript.json", "transcript.md"])
        self.assertEqual([item["name"] for item in inventory(self.notes, "publish")], ["notes.json", "notes.md"])
        for item in inventory(self.source, "archive"):
            self.assertEqual(len(item["sha256"]), 64)
            self.assertGreater(item["size"], 0)
        self.meta.pop("ended")
        atomic_json(self.source / "meta.json", self.meta)
        with self.assertRaisesRegex(UploadNeedsReview, "finalized"):
            inventory(self.source, "archive")

    def test_inventory_rejects_path_escape_symlinks_and_missing_notes(self):
        self.meta["files"]["mic"] = "../outside.caf"
        atomic_json(self.source / "meta.json", self.meta)
        with self.assertRaises(WorkflowError):
            self.upload()
        self.assertEqual(self.drive.writes, [])
        (self.notes / "notes.md").unlink()
        (self.notes / "notes.md").symlink_to(self.source / "transcript.md")
        with self.assertRaises(WorkflowError):
            self.upload("publish")
        self.assertEqual(self.drive.writes, [])

    def test_private_upload_receipt_preserves_source_and_exact_file_set(self):
        before = {path.name: path.read_bytes() for path in self.source.iterdir()}
        receipt = self.upload()
        self.assertEqual(receipt["job"], self.job)
        self.assertEqual(receipt["idempotency_key"], self.job + ":archive")
        self.assertEqual(receipt["verification"], "metadata")
        self.assertEqual({item["name"] for item in receipt["files"]}, set(before))
        self.assertEqual({item["file_name"] for item in self.drive.uploads}, set(before))
        self.assertEqual({item["parent_folder_id"] for item in self.drive.uploads}, {receipt["folder_id"]})
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.source.iterdir()})
        self.assertEqual(self.drive.items[receipt["meeting_folder_id"]]["parent_ids"], [FakeDrive.COMPANY])

    def test_shared_unknown_and_different_owner_destinations_fail_before_writes(self):
        changes = [(FakeDrive.ROOT, {"shared": True}), (FakeDrive.ROOT, {"shared": None}),
                   (FakeDrive.ROOT, {"permissions": None}),
                   (FakeDrive.ROOT, {"current_user_can_share": False}),
                   (FakeDrive.COMPANY, {"drive_id": "shared_drive_123"}),
                   (FakeDrive.COMPANY, {"permissions": [{"type": "user", "role": "owner"}]}),
                   (FakeDrive.COMPANY, {"permissions": [{"type": "user", "role": "owner",
                                                          "emailAddress": "someone-else@example.test"}]})]
        for identity, fields in changes:
            with self.subTest(identity=identity, fields=fields):
                self.drive.items[identity].update(fields)
                with self.assertRaises(UploadNeedsReview):
                    self.upload()
                self.assertEqual(self.drive.writes, [])
                self.drive = FakeDrive()
                self.uploader.invoke = self.drive

    def test_company_parent_name_and_type_must_match_approval(self):
        for fields in ({"parent_ids": ["other_parent_123"]}, {"title": "Renamed company"},
                       {"mime_type": "application/octet-stream"}):
            with self.subTest(fields=fields):
                original = copy.deepcopy(self.drive.items[FakeDrive.COMPANY])
                self.drive.items[FakeDrive.COMPANY].update(fields)
                with self.assertRaises(UploadNeedsReview):
                    self.upload()
                self.assertEqual(self.drive.writes, [])
                self.drive.items[FakeDrive.COMPANY] = original

    def test_written_file_parent_mime_and_size_are_verified(self):
        for fields in ({"parent_ids": ["wrong_parent_123"]}, {"mime_type": "text/plain"}, {"size": "999999"}):
            with self.subTest(fields=fields):
                with tempfile.TemporaryDirectory() as state:
                    self.drive = FakeDrive()
                    self.uploader = DriveUploader(state, self.drive, FakeDrive.ROOT)
                    def corrupt(tool, arguments, identity):
                        if tool == "upload_file":
                            self.drive.items[identity].update(fields)
                    self.drive.after_write = corrupt
                    with self.assertRaises(UploadNeedsReview):
                        self.upload()
                    self.assertEqual(len(self.drive.uploads), 1)

    def test_unknown_folder_write_recovers_by_search_without_duplicate(self):
        def interrupt_once(tool, arguments, identity):
            self.drive.after_write = None
            raise SimulatedTransportFailure()
        self.drive.after_write = interrupt_once
        with self.assertRaises(SimulatedTransportFailure):
            self.upload()
        self.assertEqual(len(self.drive.writes), 1)
        receipt = self.upload()
        self.assertEqual(sum(item["title"] == self.folder_name for item in self.drive.items.values()), 1)
        self.assertEqual(len([tool for tool, _ in self.drive.writes if tool == "create_folder"]), 2)
        self.assertEqual(self.drive.items[receipt["meeting_folder_id"]]["title"], self.folder_name)

    def test_unknown_file_write_recovers_without_resending_bytes(self):
        def interrupt_file(tool, arguments, identity):
            if tool == "upload_file":
                self.drive.after_write = None
                raise SimulatedTransportFailure()
        self.drive.after_write = interrupt_file
        with self.assertRaises(SimulatedTransportFailure):
            self.upload()
        first_name = self.drive.uploads[0]["file_name"]
        receipt = self.upload()
        self.assertEqual([item["file_name"] for item in self.drive.uploads].count(first_name), 1)
        recovered = next(item for item in receipt["files"] if item["name"] == first_name)
        self.assertTrue(recovered["reconciled"])

    def test_uncertain_empty_search_never_repeats_a_write(self):
        def lost_before_completion(tool, arguments):
            if tool == "create_folder":
                self.drive.before = None
                raise SimulatedTransportFailure()
        self.drive.before = lost_before_completion
        with self.assertRaises(SimulatedTransportFailure):
            self.upload()
        writes = copy.deepcopy(self.drive.writes)
        for _ in range(2):
            with self.assertRaisesRegex(UploadRetryable, "not be blindly repeated"):
                self.upload()
            self.assertEqual(self.drive.writes, writes)

    def test_proven_not_dispatched_write_can_be_attempted_later(self):
        def before_dispatch(tool, arguments):
            if tool == "create_folder":
                self.drive.before = None
                raise SimulatedTransportFailure(outcome_unknown=False)
        self.drive.before = before_dispatch
        with self.assertRaises(SimulatedTransportFailure):
            self.upload()
        self.upload()
        self.assertEqual(sum(item["title"] == self.folder_name for item in self.drive.items.values()), 1)

    def test_duplicate_matches_across_pages_require_review(self):
        def pages(parent, name, folder, arguments):
            if name != self.folder_name:
                return None
            if "page_token" not in arguments:
                return {"results": [{"id": "duplicate_one_123"}], "next_page_token": "next-page"}
            self.assertEqual(arguments["page_token"], "next-page")
            return {"results": [{"id": "duplicate_two_123"}], "next_page_token": None}
        self.drive.search_override = pages
        with self.assertRaisesRegex(UploadNeedsReview, "Multiple matching"):
            self.upload()
        self.assertEqual(self.drive.writes, [])

    def test_partial_and_cyclic_pagination_never_permits_creation(self):
        cases = [
            {"results": [{"id": "existing_item_123"}] * 100, "next_page_token": None},
            {"results": [], "next_page_token": "repeated-token"},
        ]
        for result in cases:
            with self.subTest(result=result):
                self.drive.search_override = lambda parent, name, folder, args: result if name == self.folder_name else None
                with self.assertRaises(UploadNeedsReview):
                    self.upload()
                self.assertEqual(self.drive.writes, [])

    def test_existing_unreceipted_match_is_not_adopted(self):
        self.drive.add("existing_meeting_123", self.folder_name, FakeDrive.COMPANY, FOLDER_MIME)
        with self.assertRaisesRegex(UploadNeedsReview, "without this upload's receipt"):
            self.upload()
        self.assertEqual(self.drive.writes, [])

    def test_local_content_changed_between_inventory_and_write_is_not_uploaded(self):
        def mutate_on_first_read(tool, arguments):
            self.drive.before = None
            (self.source / "mic.caf").write_bytes(b"Changed local input")
        self.drive.before = mutate_on_first_read
        with self.assertRaisesRegex(UploadNeedsReview, "Local file changed"):
            self.upload()
        self.assertNotIn("mic.caf", [item["file_name"] for item in self.drive.uploads])

    def test_local_content_changed_during_upload_requires_review(self):
        def mutate_during_write(tool, arguments, identity):
            if tool == "upload_file":
                path = Path(arguments["file_uri"])
                path.write_bytes(path.read_bytes() + b"\n")
        self.drive.after_write = mutate_during_write
        with self.assertRaisesRegex(UploadNeedsReview, "Local source changed during"):
            self.upload()
        self.assertEqual(len(self.drive.uploads), 1)
        writes = copy.deepcopy(self.drive.writes)
        with self.assertRaisesRegex(UploadNeedsReview, "inputs or destination changed"):
            self.upload()
        self.assertEqual(self.drive.writes, writes)

    def test_repeated_run_reads_remote_state_but_does_not_repeat_writes(self):
        first = self.upload()
        writes, count = copy.deepcopy(self.drive.writes), len(self.drive.calls)
        second = self.upload()
        self.assertEqual(second["meeting_folder_id"], first["meeting_folder_id"])
        self.assertEqual(second["folder_id"], first["folder_id"])
        self.assertEqual(self.drive.writes, writes)
        self.assertGreater(len(self.drive.calls), count)
        self.drive.items[first["folder_id"]]["shared"] = True
        with self.assertRaisesRegex(UploadNeedsReview, "privacy"):
            self.upload()
        self.assertEqual(self.drive.writes, writes)

    def test_file_reconciliation_lists_types_omitted_by_search_without_duplicate_upload(self):
        first = self.upload()
        json_file = next(item for item in first["files"] if item["name"] == "transcript.json")
        search = self.drive("search", {"best_effort_fetch": False, "topn": 100,
            "special_filter_query_str": "trashed = false and '" + first["folder_id"]
                                        + "' in parents and name = 'transcript.json'"})
        self.assertEqual(search["results"], [], "Fixture reproduces the connector's JSON search omission")
        self.assertIn(json_file["id"], self.drive.items)
        calls, writes = len(self.drive.calls), copy.deepcopy(self.drive.writes)
        second = self.upload()
        self.assertEqual(self.drive.writes, writes)
        self.assertEqual([item["id"] for item in second["files"]], [item["id"] for item in first["files"]])
        discovery = self.drive.calls[calls:]
        self.assertTrue(any(tool == "list_folder" for tool, _ in discovery))
        self.assertTrue(all(arguments.get("item_type") == "folder"
                            for tool, arguments in discovery if tool == "search"))

    def test_file_listing_is_exact_name_and_does_not_recurse(self):
        parent = "synthetic_stage_123"
        self.drive.add(parent, "Notes", FakeDrive.COMPANY, FOLDER_MIME)
        self.drive.add("wanted_file_123", "notes.json", parent, "application/json", 10)
        self.drive.add("similar_file_123", "old-notes.json", parent, "application/json", 10)
        self.drive.add("nested_folder_123", "Nested", parent, FOLDER_MIME)
        self.drive.add("nested_file_123", "notes.json", "nested_folder_123", "application/json", 10)
        self.assertEqual(self.uploader._find(parent, "notes.json", False), ["wanted_file_123"])
        self.assertEqual(self.drive.calls, [("list_folder", {
            "url": "https://drive.google.com/drive/folders/" + parent, "top_k": 100})])

    def test_full_or_invalid_listing_holds_before_sending_file_bytes(self):
        cases = [{"files": [{"id": "existing_file_123", "title": "unrelated"}] * 100},
                 {"files": None}, {"files": [None]},
                 {"files": [{"id": "existing_file_123"}]},
                 {"files": [{"id": "bad", "title": "meta.json"}]}]
        for response in cases:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as state:
                self.drive = FakeDrive()
                self.drive.list_override = lambda *_: response
                self.uploader = DriveUploader(state, self.drive, FakeDrive.ROOT)
                with self.assertRaises((UploadNeedsReview, UploadRetryable)):
                    self.upload()
                self.assertEqual(self.drive.uploads, [])

    def test_listing_of_99_items_is_complete_and_duplicate_matches_hold(self):
        rows = [{"id": "existing_file_" + str(i).zfill(4), "title": "unrelated-" + str(i)} for i in range(99)]
        self.drive.list_override = lambda *_: {"files": rows}
        self.assertEqual(self.uploader._find("synthetic_stage_123", "notes.json", False), [])
        rows[0]["title"] = rows[1]["title"] = "meta.json"
        with self.assertRaisesRegex(UploadNeedsReview, "Multiple matching"):
            self.upload()
        self.assertEqual(self.drive.uploads, [])

    def test_file_bridge_limit_boundary_and_oversize_preflight(self):
        self.assertEqual(MAX_FILE_BYTES, 512 * 1024 * 1024)
        def boundary(_):
            return {"size": MAX_FILE_BYTES, "sha256": "0" * 64}
        with patch("quill_workflow.drive_upload.file_identity", side_effect=boundary):
            files = inventory(self.notes, "publish")
        self.assertTrue(all(item["size"] == MAX_FILE_BYTES for item in files))
        with patch("quill_workflow.drive_upload.file_identity", return_value={
                "size": MAX_FILE_BYTES + 1, "sha256": "0" * 64}):
            with self.assertRaisesRegex(UploadNeedsReview, "512"):
                self.upload()
        self.assertEqual(self.drive.calls, [])
        self.assertFalse((self.root / "state").exists())

    def direct_fixture(self):
        direct = Mock()
        account = Mock(return_value={"email": FakeDrive.OWNER})
        self.uploader = DriveUploader(self.root / "state", self.drive, FakeDrive.ROOT,
                                      large_upload=direct, large_upload_preflight=account)
        def complete(**kwargs):
            op = kwargs["operation"]
            identity = "reserved_media_" + kwargs["name"].replace(".", "_")
            op.setdefault("resumable", {"file_id": identity})
            kwargs["save"]()
            identity = op["resumable"]["file_id"]
            if identity not in self.drive.items:
                self.drive.add(identity, kwargs["name"], kwargs["parent"], kwargs["mime"], kwargs["file"]["size"])
            return {"success": True, "id": identity}
        direct.upload.side_effect = complete
        def selected_identity(path):
            value = file_identity(path)
            if Path(path).name == "mic.caf":
                value["size"] = MAX_FILE_BYTES + 1
            return value
        return direct, account, patch("quill_workflow.drive_upload.file_identity", side_effect=selected_identity)

    def archive_journal(self):
        path = self.uploader.state / "results" / self.job / "drive-archive.json"
        return path, json.loads(path.read_text())

    def test_all_new_media_uses_direct_upload_and_receipt_survives_retry(self):
        direct, account, sizes = self.direct_fixture()
        with sizes:
            receipt = self.upload()
            writes = list(self.drive.writes)
            again = self.upload()
        self.assertEqual(self.drive.writes, writes)
        self.assertEqual(direct.upload.call_count, 4)
        self.assertEqual(receipt["files"], again["files"])
        self.assertEqual([item["file_name"] for item in self.drive.uploads],
                         ["meta.json", "transcript.json", "transcript.md"])
        for name in ("mic.caf", "system.caf"):
            media = next(item for item in receipt["files"] if item["name"] == name)
            self.assertEqual(media["verification"], "metadata_and_checksum")
        self.assertEqual(receipt["verification"], "per_file")
        account.assert_called()

    def test_small_audio_and_video_reserve_their_transport_before_dispatch(self):
        self.meta["files"]["screen"] = "screen.mp4"
        atomic_json(self.source / "meta.json", self.meta)
        (self.source / "screen.mp4").write_bytes(b"synthetic screen bytes")
        direct, account, _ = self.direct_fixture()
        complete = direct.upload.side_effect
        def check_saved_choice(**kwargs):
            _, journal = self.archive_journal()
            op = journal["operations"]["file:" + kwargs["name"]]
            self.assertEqual(op["transport"], "google_drive_resumable")
            self.assertEqual(op["state"], "dispatched")
            self.assertIn("dispatched_at", op)
            self.assertNotIn("resumable", op)
            return complete(**kwargs)
        direct.upload.side_effect = check_saved_choice
        receipt = self.upload()
        self.assertEqual(direct.upload.call_count, 3)
        self.assertEqual([item["file_name"] for item in self.drive.uploads],
                         ["meta.json", "transcript.json", "transcript.md"])
        self.assertEqual(len({item["id"] for item in receipt["files"]}), len(receipt["files"]))
        _, journal = self.archive_journal()
        for name in ("meta.json", "transcript.json", "transcript.md"):
            self.assertEqual(journal["operations"]["file:" + name]["transport"], "codex_connector")
        account.assert_called_once()

    def test_direct_connection_missing_or_wrong_account_cannot_create_folders(self):
        direct, account, _ = self.direct_fixture()
        account.side_effect = UploadNeedsReview("Connect direct Drive first")
        with self.assertRaisesRegex(UploadNeedsReview, "Connect"):
            self.upload()
        self.assertEqual(self.drive.calls, [])
        direct.upload.assert_not_called()
        direct, account, _ = self.direct_fixture()
        account.return_value = {"email": "someone-else@example.test"}
        with self.assertRaisesRegex(UploadNeedsReview, "account must match"):
            self.upload()
        self.assertEqual(self.drive.writes, [])
        direct.upload.assert_not_called()

    def test_direct_retry_after_process_restart_reuses_saved_id_when_final_response_was_lost(self):
        direct, account, _ = self.direct_fixture()
        complete = direct.upload.side_effect
        def interrupt(**kwargs):
            complete(**kwargs)
            raise UploadRetryable("Synthetic final response lost")
        direct.upload.side_effect = interrupt
        with self.assertRaisesRegex(RecoverableUploadError, "response lost"):
            self.upload()
        path, journal = self.archive_journal()
        reserved = journal["operations"]["file:mic.caf"]["resumable"]["file_id"]
        self.assertIn(reserved, self.drive.items, "The remote write completed before the response was lost")
        direct.upload.side_effect = complete
        self.uploader = DriveUploader(self.root / "state", self.drive, FakeDrive.ROOT,
                                      large_upload=direct, large_upload_preflight=account)
        receipt = self.upload()
        self.assertEqual(sum(item["title"] == "mic.caf" for item in self.drive.items.values()), 1)
        self.assertEqual(sum(item["title"] == "system.caf" for item in self.drive.items.values()), 1)
        self.assertEqual(next(x["id"] for x in receipt["files"] if x["name"] == "mic.caf"), reserved)
        self.assertFalse(any(item["file_name"] in {"mic.caf", "system.caf"} for item in self.drive.uploads))

    def test_reserved_upload_recovers_through_restarted_metadata_timeouts_without_duplicates(self):
        direct, account, _ = self.direct_fixture()
        complete = direct.upload.side_effect
        reserved = "reserved_partial_mic_123"
        def interrupt_partial(**kwargs):
            kwargs["operation"]["resumable"] = {"file_id": reserved, "offset": 8}
            kwargs["save"]()
            raise UploadRetryable("Partial transfer interrupted")
        direct.upload.side_effect = interrupt_partial
        with self.assertRaises(RecoverableUploadError):
            self.upload()
        self.assertNotIn(reserved, self.drive.items)
        direct.upload.side_effect = complete
        writes = list(self.drive.writes)
        for operation in ("get_file_metadata", "search", "list_folder"):
            with self.subTest(operation=operation):
                def timeout_read(tool, arguments):
                    if tool == operation:
                        raise DriveTimeoutError("Synthetic read timeout")
                self.drive.before = timeout_read
                self.uploader = DriveUploader(self.root / "state", self.drive, FakeDrive.ROOT,
                                              large_upload=direct, large_upload_preflight=account)
                with self.assertRaises(RecoverableUploadError):
                    self.upload()
                self.assertEqual(self.drive.writes, writes)
        self.drive.before = None
        receipt = self.upload()
        self.assertEqual(next(item["id"] for item in receipt["files"] if item["name"] == "mic.caf"), reserved)
        for name in ("mic.caf", "system.caf"):
            self.assertEqual(sum(item["title"] == name for item in self.drive.items.values()), 1)
            self.assertFalse(any(item["file_name"] == name for item in self.drive.uploads))

    def test_metadata_timeouts_without_reserved_id_remain_bounded_failures(self):
        direct, account, _ = self.direct_fixture()
        for operation in ("get_file_metadata", "search", "list_folder"):
            with self.subTest(operation=operation):
                def timeout_read(tool, arguments):
                    if tool == operation:
                        raise DriveTimeoutError("Synthetic read timeout")
                self.drive.before = timeout_read
                with self.assertRaises(DriveTimeoutError):
                    self.upload()
                direct.upload.assert_not_called()

    def test_read_timeout_after_direct_completion_recovers_the_same_reserved_file(self):
        direct, account, _ = self.direct_fixture()
        complete = direct.upload.side_effect
        def complete_then_lose_verification(**kwargs):
            result = complete(**kwargs)
            def timeout_completed_file(tool, arguments):
                if tool == "get_file_metadata" and arguments["fileId"] == result["id"]:
                    raise DriveTimeoutError("Verification read timed out")
            self.drive.before = timeout_completed_file
            return result
        direct.upload.side_effect = complete_then_lose_verification
        with self.assertRaises(RecoverableUploadError):
            self.upload()
        _, journal = self.archive_journal()
        reserved = journal["operations"]["file:mic.caf"]["resumable"]["file_id"]
        self.assertIn(reserved, self.drive.items)
        self.drive.before = None
        direct.upload.side_effect = complete
        receipt = self.upload()
        self.assertEqual(next(item["id"] for item in receipt["files"] if item["name"] == "mic.caf"), reserved)
        self.assertEqual(sum(item["title"] == "mic.caf" for item in self.drive.items.values()), 1)

    def test_auth_refresh_retries_only_after_reserving_id_and_rejected_auth_still_holds(self):
        direct, account, _ = self.direct_fixture()
        complete = direct.upload.side_effect
        direct.upload.side_effect = DriveAuthRetryable("Google authorization is temporarily unavailable")
        with self.assertRaises(DriveAuthRetryable):
            self.upload()
        _, journal = self.archive_journal()
        self.assertNotIn("resumable", journal["operations"]["file:mic.caf"])
        reserved = "reserved_auth_mic_123"
        def reserve_then_fail_refresh(**kwargs):
            kwargs["operation"]["resumable"] = {"file_id": reserved}
            kwargs["save"]()
            raise DriveAuthRetryable("Google authorization is temporarily unavailable")
        direct.upload.side_effect = reserve_then_fail_refresh
        with self.assertRaises(RecoverableUploadError):
            self.upload()
        direct.upload.side_effect = DriveAuthNeedsConfiguration("Google authorization was rejected")
        with self.assertRaises(DriveAuthNeedsConfiguration):
            self.upload()
        _, journal = self.archive_journal()
        self.assertEqual(journal["operations"]["file:mic.caf"]["resumable"]["file_id"], reserved)
        self.assertNotIn(reserved, self.drive.items)
        self.assertFalse(any(item["file_name"] in {"mic.caf", "system.caf"} for item in self.drive.uploads))
        direct.upload.side_effect = complete
        self.upload()
        self.assertEqual(sum(item["title"] == "mic.caf" for item in self.drive.items.values()), 1)
        self.assertIn(reserved, self.drive.items)

    def test_non_timeout_connector_errors_are_not_promoted_for_reserved_uploads(self):
        direct, account, _ = self.direct_fixture()
        self.upload()
        writes = list(self.drive.writes)
        for error_type in (DriveOperationError, DriveProtocolError):
            with self.subTest(error_type=error_type):
                def reject_read(tool, arguments):
                    if tool == "get_file_metadata":
                        raise error_type("Synthetic connector rejection")
                self.drive.before = reject_read
                with self.assertRaises(error_type):
                    self.upload()
                self.assertEqual(self.drive.writes, writes)

    def test_write_timeouts_are_not_promoted_even_after_media_has_reserved_ids(self):
        direct, account, _ = self.direct_fixture()
        def timeout_text_write(tool, arguments):
            if tool == "upload_file" and arguments["file_name"] == "transcript.json":
                raise DriveTimeoutError("Synthetic write timeout", outcome_unknown=True)
        self.drive.before = timeout_text_write
        with self.assertRaises(DriveTimeoutError):
            self.upload()
        self.assertEqual(direct.upload.call_count, 2)
        _, journal = self.archive_journal()
        self.assertEqual(journal["operations"]["file:transcript.json"]["state"], "dispatched")
        self.assertNotIn("id", journal["operations"]["file:transcript.json"])
        def timeout_folder_write(tool, arguments):
            if tool == "create_folder":
                raise DriveTimeoutError("Synthetic folder write timeout", outcome_unknown=True)
        self.drive.before = timeout_folder_write
        with self.assertRaises(DriveTimeoutError):
            self.uploader._call("create_folder", {"name": "Synthetic folder", "parent_folder": FakeDrive.COMPANY})

    def test_reserved_retry_classification_does_not_leak_to_another_job(self):
        direct, account, _ = self.direct_fixture()
        self.upload()
        path, original = self.archive_journal()
        writes = list(self.drive.writes)
        def timeout_read(tool, arguments):
            if tool == "get_file_metadata":
                raise DriveTimeoutError("New job has no reserved ID")
        self.drive.before = timeout_read
        with self.assertRaises(DriveTimeoutError):
            self.uploader.upload("other-synthetic-job", "archive", self.source, self.route, self.started)
        self.assertEqual(self.drive.writes, writes)
        self.assertEqual(json.loads(path.read_text()), original)

    def test_saved_small_resumable_receipts_resume_direct_without_transport_marker(self):
        direct, account, _ = self.direct_fixture()
        receipt = self.upload()
        path, journal = self.archive_journal()
        for name in ("mic.caf", "system.caf"):
            journal["operations"]["file:" + name].pop("transport", None)
        atomic_json(path, journal)
        writes = list(self.drive.writes)
        direct.upload.reset_mock()
        again = self.upload()
        self.assertEqual(self.drive.writes, writes)
        self.assertEqual(direct.upload.call_count, 2)
        self.assertEqual(receipt["files"], again["files"])
        _, current = self.archive_journal()
        for name in ("mic.caf", "system.caf"):
            self.assertEqual(current["operations"]["file:" + name]["transport"], "google_drive_resumable")

    def test_saved_small_resumable_upload_holds_when_provider_is_unavailable(self):
        direct, account, _ = self.direct_fixture()
        complete = direct.upload.side_effect
        def interrupt(**kwargs):
            complete(**kwargs)
            raise UploadRetryable("Synthetic final response lost")
        direct.upload.side_effect = interrupt
        with self.assertRaises(RecoverableUploadError):
            self.upload()
        writes = list(self.drive.writes)
        self.uploader = DriveUploader(self.root / "state", self.drive, FakeDrive.ROOT)
        with self.assertRaises(UploadNeedsReview):
            self.upload()
        self.assertEqual(self.drive.writes, writes)
        self.assertEqual(sum(item["title"] == "mic.caf" for item in self.drive.items.values()), 1)

    def test_pinned_direct_upload_without_session_holds_if_provider_is_removed(self):
        direct, account, _ = self.direct_fixture()
        direct.upload.side_effect = UploadRetryable("Connection failed before reserving an ID")
        with self.assertRaises(UploadRetryable):
            self.upload()
        _, journal = self.archive_journal()
        self.assertEqual(journal["operations"]["file:mic.caf"]["transport"], "google_drive_resumable")
        self.assertNotIn("resumable", journal["operations"]["file:mic.caf"])
        writes = list(self.drive.writes)
        self.uploader = DriveUploader(self.root / "state", self.drive, FakeDrive.ROOT)
        with self.assertRaises(UploadNeedsReview):
            self.upload()
        self.assertEqual(self.drive.writes, writes)

    def test_corrupt_or_conflicting_saved_transport_holds_before_cloud_access(self):
        direct, account, _ = self.direct_fixture()
        self.upload()
        path, original = self.archive_journal()
        cases = [
            {"transport": "unknown_provider"},
            {"resumable": {}},
            {"resumable": None},
            {"resumable": []},
            {"transport": "codex_connector"},
        ]
        for changed in cases:
            with self.subTest(changed=changed):
                journal = copy.deepcopy(original)
                journal["operations"]["file:mic.caf"].update(changed)
                atomic_json(path, journal)
                calls = list(self.drive.calls)
                direct.upload.reset_mock()
                account.reset_mock()
                with self.assertRaises(UploadNeedsReview):
                    self.upload()
                self.assertEqual(self.drive.calls, calls)
                direct.upload.assert_not_called()
                account.assert_not_called()

    def test_direct_failure_never_falls_back_to_connector_upload(self):
        direct, account, _ = self.direct_fixture()
        direct.upload.side_effect = UploadRetryable("Temporary direct connection failure")
        for _ in range(2):
            with self.assertRaisesRegex(UploadRetryable, "Temporary direct"):
                self.upload()
        self.assertEqual(direct.upload.call_count, 2)
        self.assertFalse(any(item["file_name"] in {"mic.caf", "system.caf"} for item in self.drive.uploads))
        _, journal = self.archive_journal()
        self.assertEqual(journal["operations"]["file:mic.caf"]["transport"], "google_drive_resumable")

    def test_unknown_legacy_media_write_remains_connector_and_is_not_repeated(self):
        def lose_response_before_completion(tool, arguments):
            if tool == "upload_file" and arguments["file_name"] == "mic.caf":
                raise SimulatedTransportFailure()
        self.drive.before = lose_response_before_completion
        with self.assertRaises(SimulatedTransportFailure):
            self.upload()
        path, journal = self.archive_journal()
        for op in journal["operations"].values():
            op.pop("transport", None)
        atomic_json(path, journal)
        self.drive.before = None
        writes = list(self.drive.writes)
        direct, account, _ = self.direct_fixture()
        for _ in range(2):
            with self.assertRaisesRegex(UploadRetryable, "not be blindly repeated"):
                self.upload()
        self.assertEqual(self.drive.writes, writes)
        direct.upload.assert_not_called()
        _, current = self.archive_journal()
        op = current["operations"]["file:mic.caf"]
        self.assertEqual(op["state"], "dispatched")
        self.assertEqual(op["transport"], "codex_connector")
        self.assertNotIn("resumable", op)
        self.assertNotIn("id", op)

    def test_completed_legacy_media_keeps_receipts_without_direct_preflight(self):
        receipt = self.upload()
        path, journal = self.archive_journal()
        for op in journal["operations"].values():
            op.pop("transport", None)
        atomic_json(path, journal)
        writes = list(self.drive.writes)
        direct, account, _ = self.direct_fixture()
        account.side_effect = AssertionError("Completed legacy uploads must not need direct credentials")
        again = self.upload()
        self.assertEqual(receipt["files"], again["files"])
        self.assertEqual(self.drive.writes, writes)
        direct.upload.assert_not_called()
        account.assert_not_called()
        _, current = self.archive_journal()
        for name in ("mic.caf", "system.caf"):
            self.assertEqual(current["operations"]["file:" + name]["transport"], "codex_connector")

    def test_explicit_connector_choice_survives_a_proven_undispatched_attempt(self):
        def fail_before_dispatch(tool, arguments):
            if tool == "upload_file" and arguments["file_name"] == "mic.caf":
                raise SimulatedTransportFailure(outcome_unknown=False)
        self.drive.before = fail_before_dispatch
        with self.assertRaises(SimulatedTransportFailure):
            self.upload()
        path, journal = self.archive_journal()
        op = journal["operations"]["file:mic.caf"]
        self.assertEqual(op["state"], "planned")
        self.assertEqual(op["transport"], "codex_connector")
        self.drive.before = None
        direct, account, _ = self.direct_fixture()
        self.upload()
        direct_names = [call.kwargs["name"] for call in direct.upload.call_args_list]
        self.assertNotIn("mic.caf", direct_names)
        self.assertEqual([item["file_name"] for item in self.drive.uploads].count("mic.caf"), 2)
        self.assertEqual(sum(item["title"] == "mic.caf" for item in self.drive.items.values()), 1)

    def test_unpinned_planned_legacy_media_can_select_direct_before_first_dispatch(self):
        def fail_before_dispatch(tool, arguments):
            if tool == "upload_file" and arguments["file_name"] == "mic.caf":
                raise SimulatedTransportFailure(outcome_unknown=False)
        self.drive.before = fail_before_dispatch
        with self.assertRaises(SimulatedTransportFailure):
            self.upload()
        path, journal = self.archive_journal()
        for op in journal["operations"].values():
            op.pop("transport", None)
        atomic_json(path, journal)
        self.drive.before = None
        direct, account, _ = self.direct_fixture()
        self.upload()
        self.assertEqual([call.kwargs["name"] for call in direct.upload.call_args_list], ["mic.caf", "system.caf"])
        self.assertEqual([item["file_name"] for item in self.drive.uploads].count("mic.caf"), 1)

    def test_large_text_never_uses_media_bypass(self):
        direct, account, _ = self.direct_fixture()
        with patch("quill_workflow.drive_upload.file_identity", return_value={"size": MAX_FILE_BYTES + 1, "sha256": "0" * 64}):
            with self.assertRaisesRegex(UploadNeedsReview, "512"):
                self.upload("publish")
        self.assertEqual(self.drive.calls, [])
        direct.upload.assert_not_called()

    def test_small_recordings_without_direct_connection_keep_existing_connector_path(self):
        receipt = self.upload()
        self.assertEqual(receipt["verification"], "metadata")
        self.assertEqual([item["file_name"] for item in self.drive.uploads],
                         ["meta.json", "mic.caf", "system.caf", "transcript.json", "transcript.md"])
        _, journal = self.archive_journal()
        for name in ("mic.caf", "system.caf"):
            self.assertEqual(journal["operations"]["file:" + name]["transport"], "codex_connector")

    def test_stage_two_reuses_meeting_folder_and_only_publishes_notes(self):
        for name in ("source-transcript.json", "provenance.json", "runner.log"):
            (self.notes / name).write_text("Must remain local")
        archive = self.upload()
        archive_count = len(self.drive.uploads)
        published = self.upload("publish")
        self.assertEqual(published["meeting_folder_id"], archive["meeting_folder_id"])
        self.assertNotEqual(published["folder_id"], archive["folder_id"])
        self.assertEqual(self.drive.items[published["folder_id"]]["title"], "Notes")
        self.assertEqual([item["file_name"] for item in self.drive.uploads[archive_count:]], ["notes.json", "notes.md"])
        self.assertEqual(sum(item["title"] == self.folder_name for item in self.drive.items.values()), 1)

    def test_publish_first_then_archive_still_uses_one_meeting_folder(self):
        published = self.upload("publish")
        archived = self.upload()
        self.assertEqual(published["meeting_folder_id"], archived["meeting_folder_id"])
        self.assertEqual(sum(item["title"] == self.folder_name for item in self.drive.items.values()), 1)

    def test_same_size_local_revision_cannot_reuse_old_receipt(self):
        self.upload()
        writes = copy.deepcopy(self.drive.writes)
        path = self.source / "mic.caf"
        path.write_bytes(b"X" * path.stat().st_size)
        with self.assertRaisesRegex(UploadNeedsReview, "inputs or destination changed"):
            self.upload()
        self.assertEqual(self.drive.writes, writes)

    def test_owner_change_between_stages_requires_review(self):
        self.upload()
        writes = copy.deepcopy(self.drive.writes)
        for item in self.drive.items.values():
            item["permissions"][0]["emailAddress"] = "new-owner@example.test"
        with self.assertRaisesRegex(UploadNeedsReview, "owner changed"):
            self.upload("publish")
        self.assertEqual(self.drive.writes, writes)

    def test_stage_folder_moved_between_files_prevents_next_write(self):
        def move_stage(tool, arguments, identity):
            if tool == "upload_file":
                self.drive.items[arguments["parent_folder_id"]]["parent_ids"] = ["different_private_parent_123"]
        self.drive.after_write = move_stage
        with self.assertRaisesRegex(UploadNeedsReview, "parent"):
            self.upload()
        self.assertEqual(len(self.drive.uploads), 1,
                         "No additional file may be uploaded after the folder leaves its approved parent")

    def test_guard_failure_stops_before_any_external_write(self):
        def revoke():
            raise UploadNeedsReview("Routing approval changed")
        self.uploader.guard = revoke
        with self.assertRaisesRegex(UploadNeedsReview, "approval changed"):
            self.upload()
        self.assertEqual(self.drive.calls, [])


if __name__ == "__main__":
    unittest.main()
