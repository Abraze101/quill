"""Synthetic read-only meeting-library metadata and filing evidence tests."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.drive_upload import COMBINED_TEXT_FORMAT, NATIVE_TEMPLATE_FORMAT, _hash
from quill_workflow.meeting_document import build_document
from quill_workflow.native_template import build_payload
from quill_workflow.pipeline import SCHEMA, atomic_json, render_notes
from quill_workflow.review_library import meeting_detail, meeting_library


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class ReviewLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        self.job = "synthetic-meeting"
        self.recording = self.state / "recordings" / self.job
        self.result = self.state / "results" / self.job
        self.db = sqlite3.connect(self.state / "jobs.sqlite3")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO jobs VALUES (?,?,?)", (self.job, str(self.root / "never-read-original"), 1))
        self.metadata = {"started": "2026-10-20T23:30:00Z", "ended": "2026-10-20T23:31:00Z",
                         "recording_timezone": "Asia/Singapore", "recording_utc_offset_seconds": 28800,
                         "files": {"mic": "mic.caf", "system": "system.caf"}}
        self.transcript = {"segments": [{"start_ms": 0, "end_ms": 1000, "speaker": "me",
                                        "text": "We discussed Max's proposed dashboard."}]}
        atomic_json(self.recording / "meta.json", self.metadata)
        atomic_json(self.recording / "transcript.json", self.transcript)
        for name in ("mic.caf", "system.caf"):
            (self.recording / name).write_bytes(b"synthetic-not-audio")
        self.manifest = {path.name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}
                         for path in self.recording.iterdir()}
        atomic_json(self.recording / "manifest.json", self.manifest)
        self.transcript_bytes = (self.recording / "transcript.json").read_bytes()
        self.transcript_hash = hashlib.sha256(self.transcript_bytes).hexdigest()
        self.notes = {"schema_version": 1, "meeting_title": {"text": "Dashboard review", "evidence": [0],
                      "participants": [{"name": "Max", "evidence": [0]}]},
                      "summary": [{"text": "Review the proposed dashboard.", "evidence": [0]}]}
        self.route = {"company_id": "example", "company_name": "Example", "drive_folder_id": "company_folder_123"}
        self.settings = {"notes": {"enabled": True, "command": ["synthetic"]},
                         "route": {"enabled": True, "companies": [{"id": "example", "name": "Example",
                                                                  "drive_folder_id": "company_folder_123"}]}}
        for stage in ("archive", "publish"):
            self.settings[stage] = {"enabled": True, "type": "codex_drive", "meetings_root_id": "meetings_root_123",
                                    "meeting_timezone": "Asia/Singapore", "text_format": NATIVE_TEMPLATE_FORMAT}
        self.settings["publish"].update(template_document_id="template_document_123", template_sha256="a" * 64)
        self.receipts = {}
        self.journals = {}
        for stage in ("notes", "route"):
            self.receipt(stage, route=self.route) if stage == "route" else self.receipt(stage)
        atomic_json(self.result / "notes/notes.json", self.notes)
        atomic_json(self.result / "notes/provenance.json", self.receipts["notes"])
        (self.result / "notes/source-transcript.json").write_bytes(self.transcript_bytes)
        self.complete("publish")
        self.complete("archive")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def task(self, stage, status, receipt=None):
        self.db.execute("INSERT OR REPLACE INTO tasks (job,stage,status,settings,receipt) VALUES (?,?,?,?,?)",
                        (self.job, stage, status, json.dumps(self.settings[stage]), json.dumps(receipt) if receipt else None))
        self.db.commit()

    def receipt(self, stage, **extra):
        receipt = {"job": self.job, "stage": stage, "idempotency_key": self.job + ":" + stage,
                   "settings_sha256": digest(self.settings[stage]), "input_sha256": self.transcript_hash, **extra}
        self.receipts[stage] = receipt
        atomic_json(self.result / (stage + "-receipt.json"), receipt)
        self.task(stage, "succeeded", receipt)
        return receipt

    def complete(self, stage, *, participants_first=True):
        if stage == "publish":
            payload = build_payload(self.notes, self.transcript, self.metadata, self.route, "Asia/Singapore",
                                    participants_first=participants_first)
            files = [{"name": "native-template", "mime_type": "application/vnd.google-apps.document",
                      "payload_sha256": _hash(payload)}]
            verification = "metadata_and_template_content"
        else:
            files = [{"name": name, "mime_type": "audio/x-caf", **self.manifest[name]} for name in ("mic.caf", "system.caf")]
            verification = "metadata"
        identity = {"job": self.job, "stage": stage, "idempotency_key": self.job + ":" + stage,
                    "meetings_root_id": "meetings_root_123", "route": self.route,
                    "started": self.metadata["started"], "files": files,
                    "source": str(self.recording if stage == "archive" else self.result),
                    "meeting_timezone": "Asia/Singapore", "text_format": NATIVE_TEMPLATE_FORMAT}
        if stage == "publish":
            identity.update(document_title=payload["title"], template_document_id="template_document_123", template_sha256="a" * 64)
        if self.settings[stage].get("account_link_id") is not None:
            identity["account_link_id"] = self.settings[stage]["account_link_id"]
        folder = stage + "_folder_123"
        external = {"job": self.job, "stage": stage, "idempotency_key": self.job + ":" + stage,
                    "folder_id": folder, "meeting_folder_id": "meeting_folder_123", "meeting_folder_name": "Dashboard review - 10/21/26",
                    "files": [dict(file, id=f"remote_{stage}_{index}_123", verification=verification) for index, file in enumerate(files)],
                    "verification": verification}
        operations = {"stage": {"state": "verified", "id": folder, "parent": "meeting_folder_123"}}
        for remote in external["files"]:
            operations["file:" + remote["name"]] = {"state": "verified", "id": remote["id"], "parent": folder,
                                                     "verification": verification, "fill": {"state": "verified"}}
        journal = {"schema_version": 1, "identity": identity, "identity_sha256": _hash(identity),
                   "receipt": external, "operations": operations}
        self.journals[stage] = journal
        atomic_json(self.result / ("drive-" + stage + ".json"), journal)
        self.receipt(stage, **{key: value for key, value in external.items() if key not in ("job", "stage", "idempotency_key")},
                     route_sha256=digest(self.route))

    def view(self):
        return meeting_library(self.state)[self.job]

    def test_saved_review_applies_suggestions_only_to_derived_transcript(self):
        from test_speaker_review import source, context, draft
        from quill_workflow.transcript_sources import bind_speaker_review
        self.transcript = source()
        atomic_json(self.recording / "transcript.json", self.transcript)
        self.transcript_bytes = (self.recording / "transcript.json").read_bytes()
        self.transcript_hash = hashlib.sha256(self.transcript_bytes).hexdigest()
        self.notes = bind_speaker_review(draft(), self.transcript, context())
        self.receipt("notes")
        atomic_json(self.result / "notes/notes.json", self.notes)
        atomic_json(self.result / "notes/provenance.json", self.receipts["notes"])
        (self.result / "notes/source-transcript.json").write_bytes(self.transcript_bytes)
        view = self.view()
        self.assertIn("Jamie (suggested): Thanks.", view["transcript_text"])
        self.assertEqual(view["participants"], ["Anna"])
        detail = meeting_detail(self.state, self.job)
        self.assertEqual(detail["transcript_text"], view["transcript_text"])
        self.assertEqual(detail["participants"], view["participants"])
        self.assertEqual(detail["notes_text"], render_notes(self.notes, self.transcript))
        self.assertEqual((self.recording / "transcript.json").read_bytes(), self.transcript_bytes)
        self.notes["speaker_review"]["transcript_sha256"] = "0" * 64
        atomic_json(self.result / "notes/notes.json", self.notes)
        view = self.view()
        self.assertNotIn("Jamie", view["transcript_text"])
        self.assertIn("Other speaker 1: Thanks.", view["transcript_text"])
        self.assertIsNone(meeting_detail(self.state, self.job)["notes_text"])

    def test_detail_reads_only_selected_meeting_content_and_preserves_library_shape(self):
        from quill_workflow.review_library import _Sources
        self.db.execute("INSERT INTO jobs VALUES (?,?,?)", ("other-meeting", str(self.root / "private"), 2))
        self.db.commit()
        expected = self.view()
        opened = []
        original = _Sources.read

        def read_selected(sources, relative, *args, **kwargs):
            opened.append(str(relative))
            self.assertEqual(Path(relative).parts[1], self.job)
            return original(sources, relative, *args, **kwargs)

        with patch.object(_Sources, "read", read_selected):
            detail = meeting_detail(self.state, self.job)
        self.assertTrue(opened)
        self.assertEqual(detail.pop("notes_text"), render_notes(self.notes, self.transcript))
        self.assertEqual(detail, expected)
        self.assertNotIn("notes_text", self.view())
        self.assertNotIn("settings", detail)
        self.assertNotIn("receipt", detail)

    def test_detail_renders_validated_notes_instead_of_unverified_markdown(self):
        (self.result / "notes/notes.md").write_text("Unverified edited notes must not escape")
        detail = meeting_detail(self.state, self.job)
        self.assertEqual(detail["notes_text"], render_notes(self.notes, self.transcript))
        self.assertNotIn("Unverified", detail["notes_text"])
        provenance = dict(self.receipts["notes"], input_sha256="0" * 64)
        atomic_json(self.result / "notes/provenance.json", provenance)
        detail = meeting_detail(self.state, self.job)
        self.assertIsNone(detail["notes_text"])
        self.assertIsNotNone(detail["transcript_text"])

    def test_detail_does_not_expose_notes_with_changed_source_or_receipt(self):
        source = self.result / "notes/source-transcript.json"
        source.write_bytes(b'{"segments": []}')
        self.assertIsNone(meeting_detail(self.state, self.job)["notes_text"])
        source.write_bytes(self.transcript_bytes)
        receipt = self.result / "notes-receipt.json"
        receipt.unlink()
        self.assertIsNone(meeting_detail(self.state, self.job)["notes_text"])

    def test_detail_rejects_invalid_absent_and_deleted_selections_before_content_reads(self):
        with patch("quill_workflow.review_library._Sources", side_effect=AssertionError("Invalid ID opened state")):
            for job in (None, True, [], "", "../escape", "valid/escape", "a" * 129):
                self.assertIsNone(meeting_detail(self.state, job))
        with patch("quill_workflow.review_library._Sources.read", side_effect=AssertionError("Unselected content opened")):
            self.assertIsNone(meeting_detail(self.state, "missing-meeting"))
            for status in ("cancelling", "ready", "trashed"):
                self.db.execute("INSERT OR REPLACE INTO local_deletions VALUES (?,?)",
                                (self.job, json.dumps({"status": status})))
                self.db.commit()
                self.assertIsNone(meeting_detail(self.state, self.job))

    def test_detail_is_read_only_and_missing_state_creates_nothing(self):
        before = {str(path.relative_to(self.state)): path.read_bytes()
                  for path in self.state.rglob("*") if path.is_file()}
        with patch("quill_workflow.pipeline.Queue", side_effect=AssertionError("Must not create Queue")):
            self.assertIsNotNone(meeting_detail(self.state, self.job))
        after = {str(path.relative_to(self.state)): path.read_bytes()
                 for path in self.state.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        missing = self.root / "missing-state"
        self.assertIsNone(meeting_detail(missing, self.job))
        self.assertFalse(missing.exists())
        empty = self.root / "empty-state"
        empty.mkdir()
        self.assertIsNone(meeting_detail(empty, self.job))
        self.assertEqual(list(empty.iterdir()), [])

    def test_detail_bounds_notes_and_does_not_follow_linked_notes(self):
        with patch("quill_workflow.review_library.MAX_NOTES_CHARACTERS", 20):
            text = meeting_detail(self.state, self.job)["notes_text"]
        self.assertEqual(text, render_notes(self.notes, self.transcript)[:20] + "\n\n[Notes preview shortened.]")
        notes = self.result / "notes/notes.json"
        outside = self.root / "outside-notes.json"
        notes.rename(outside)
        notes.symlink_to(outside)
        self.assertIsNone(meeting_detail(self.state, self.job)["notes_text"])

    def unset_stage(self, stage, status="pending"):
        self.task(stage, status)
        (self.result / (stage + "-receipt.json")).unlink(missing_ok=True)
        (self.result / ("drive-" + stage + ".json")).unlink(missing_ok=True)

    def older_publish(self, text_format, *, participants_first=True):
        self.settings["publish"]["text_format"] = text_format
        (self.result / "notes/notes.md").write_text(render_notes(
            self.notes, self.transcript, participants_first=participants_first))
        journal = json.loads(json.dumps(self.journals["publish"]))
        identity = journal["identity"]
        for key in ("document_title", "template_document_id", "template_sha256"):
            identity.pop(key, None)
        identity["text_format"] = text_format
        if text_format == "legacy":
            identity["source"] = str(self.result / "notes")
            files = [{"name": name, "mime_type": "application/json" if name.endswith(".json") else "text/markdown",
                      "size": (self.result / "notes" / name).stat().st_size,
                      "sha256": hashlib.sha256((self.result / "notes" / name).read_bytes()).hexdigest()}
                     for name in ("notes.json", "notes.md")]
            verification = "metadata"
        else:
            document = build_document(self.notes, self.transcript, self.metadata, self.route,
                                      self.result / "documents", "Asia/Singapore", participants_first=participants_first)
            files = [{"name": "documents/meeting.docx", "mime_type": "application/vnd.google-apps.document",
                      "source_mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                      "size": document["path"].stat().st_size, "sha256": document["manifest"]["docx_sha256"]}]
            identity["document_title"] = document["title"]
            identity["document_text_sha256"] = hashlib.sha256(" ".join(" ".join(document["text"]).split()).encode()).hexdigest()
            verification = "metadata_and_document_text"
        identity["files"] = files
        journal["identity_sha256"] = _hash(identity)
        external = journal["receipt"]
        external["files"] = [dict(file, id=f"remote_older_{index}_123", verification=verification) for index, file in enumerate(files)]
        external["verification"] = verification
        journal["operations"] = {"stage": {"state": "verified", "id": "publish_folder_123", "parent": "meeting_folder_123"}}
        for file in external["files"]:
            journal["operations"]["file:" + file["name"]] = {"state": "verified", "id": file["id"],
                "parent": "publish_folder_123", "verification": verification}
        self.journals["publish"] = journal
        atomic_json(self.result / "drive-publish.json", journal)
        self.receipt("publish", **{key: value for key, value in external.items() if key not in ("job", "stage", "idempotency_key")},
                     route_sha256=digest(self.route))

    def test_complete_filing_has_separate_confirmed_document_and_media(self):
        result = self.view()
        self.assertEqual(result["title"], "Max - Dashboard review")
        self.assertEqual(result["folder_name"], "Example")
        self.assertEqual(result["folder_id"], "company_folder_123")
        self.assertEqual(result["storage_status"], "saved_in_drive")
        self.assertEqual(result["storage"]["document"]["file_ids"], ["remote_publish_0_123"])
        self.assertEqual(result["storage"]["media"]["status"], "confirmed")
        self.assertEqual(result["participants"], [])  # A mentioned Max is not proof he attended.

    def test_account_bound_filing_confirms_both_notes_and_media(self):
        for stage in ("archive", "publish"):
            self.settings[stage]["account_link_id"] = "link_filed_account"
            self.complete(stage)
        result = self.view()
        self.assertEqual(result["storage_status"], "saved_in_drive")
        self.assertEqual(result["storage"]["document"]["status"], "confirmed")
        self.assertEqual(result["storage"]["media"]["status"], "confirmed")

    def test_different_or_missing_filing_account_cannot_confirm(self):
        for stage, kind in (("archive", "media"), ("publish", "document")):
            self.settings[stage]["account_link_id"] = "link_filed_account"
            self.complete(stage)
            for other in (None, "link_another_account"):
                with self.subTest(stage=stage, account=other):
                    identity = self.journals[stage]["identity"]
                    if other is None:
                        identity.pop("account_link_id", None)
                    else:
                        identity["account_link_id"] = other
                    self.journals[stage]["identity_sha256"] = _hash(identity)
                    atomic_json(self.result / ("drive-" + stage + ".json"), self.journals[stage])
                    self.assertEqual(self.view()["storage"][kind]["status"], "uncertain")

    def test_legacy_filing_cannot_adopt_unrecorded_account(self):
        journal = self.journals["publish"]
        journal["identity"]["account_link_id"] = "link_another_account"
        journal["identity_sha256"] = _hash(journal["identity"])
        atomic_json(self.result / "drive-publish.json", journal)
        self.assertEqual(self.view()["storage"]["document"]["status"], "uncertain")

    def test_transcript_preview_adds_one_notice_without_changing_labels_or_speech(self):
        from quill_workflow.capture_quality import NOTICE
        original = self.view()["transcript_text"]
        self.assertEqual(original, "Microphone: We discussed Max's proposed dashboard.")
        self.transcript["capture_quality"] = {"status": "incomplete", "affected_sources": ["system", "mic"]}
        atomic_json(self.recording / "transcript.json", self.transcript)
        self.assertEqual(self.view()["transcript_text"], NOTICE + "\n\n" + original)
        self.assertEqual(self.view()["transcript_text"].count(NOTICE), 1)
        self.transcript.pop("capture_quality")
        atomic_json(self.recording / "transcript.json", self.transcript)
        self.assertEqual(self.view()["transcript_text"], original)

    def test_malformed_capture_quality_hides_only_affected_transcript(self):
        with self.db:
            self.db.execute("INSERT INTO jobs VALUES (?,?,?)", ("other-meeting", str(self.root / "other-original"), 2))
        atomic_json(self.state / "recordings/other-meeting/transcript.json", self.transcript)
        self.transcript["capture_quality"] = {"status": "incomplete", "affected_sources": ["mic"],
                                              "notice": "RAW PRIVATE DEVICE ERROR"}
        atomic_json(self.recording / "transcript.json", self.transcript)
        result = meeting_library(self.state)
        self.assertIsNone(result[self.job]["transcript_text"])
        self.assertEqual(result[self.job]["recording_files"][0]["name"], "mic.caf")
        self.assertEqual(result["other-meeting"]["transcript_text"], "Microphone: We discussed Max's proposed dashboard.")
        self.assertNotIn("RAW PRIVATE DEVICE ERROR", json.dumps(result))

    def test_capture_notice_does_not_make_an_empty_transcript_available(self):
        atomic_json(self.recording / "transcript.json", {"segments": [], "capture_quality": {
            "status": "incomplete", "affected_sources": ["mic"]}})
        self.assertIsNone(self.view()["transcript_text"])

    def test_date_uses_recording_timezone_not_queue_time_or_current_machine(self):
        result = self.view()
        self.assertEqual(result["display_date"], "10/21/26")
        self.assertEqual(result["recorded_at"], datetime.fromisoformat("2026-10-20T23:30:00+00:00").timestamp())
        self.metadata.pop("recording_timezone")
        self.metadata.pop("recording_utc_offset_seconds")
        atomic_json(self.recording / "meta.json", self.metadata)
        self.assertEqual(self.view()["display_date"], "10/20/26")

    def test_invalid_timezone_does_not_guess(self):
        self.metadata["recording_utc_offset_seconds"] = 3600
        atomic_json(self.recording / "meta.json", self.metadata)
        result = self.view()
        self.assertIsNone(result["display_date"])
        self.assertIsNone(result["recorded_at"])
        self.assertEqual(result["storage_status"], "upload_uncertain")

    def test_pending_media_never_claims_complete_save(self):
        self.unset_stage("archive")
        result = self.view()
        self.assertEqual(result["storage_status"], "partly_saved")
        self.assertEqual(result["storage"]["media"]["status"], "pending")
        self.assertEqual(result["storage"]["document"]["status"], "confirmed")

    def test_unacknowledged_write_preserves_uncertain_status(self):
        self.unset_stage("archive", "held")
        atomic_json(self.result / "drive-archive.json", {"operations": {"file:mic.caf": {"state": "dispatched"}}})
        result = self.view()
        self.assertEqual(result["storage_status"], "upload_uncertain")
        self.assertEqual(result["storage"]["document"]["status"], "confirmed")

    def test_cancelled_upload_keeps_confirmed_and_in_flight_evidence(self):
        self.unset_stage("archive", "cancelled")
        result = self.view()
        self.assertEqual(result["storage"]["media"]["status"], "cancelled")
        self.assertEqual(result["storage"]["document"]["status"], "confirmed")
        atomic_json(self.result / "drive-archive.json", {"operations": {"file:mic.caf": {"state": "dispatched"}}})
        self.assertEqual(self.view()["storage"]["media"]["status"], "uncertain")

    def test_missing_or_mismatched_receipt_cannot_confirm(self):
        for path in (self.result / "publish-receipt.json", self.result / "drive-publish.json"):
            original = path.read_bytes()
            path.unlink()
            self.assertEqual(self.view()["storage"]["document"]["status"], "uncertain")
            path.write_bytes(original)
        self.receipts["publish"]["folder_id"] = "different_folder_123"
        atomic_json(self.result / "publish-receipt.json", self.receipts["publish"])
        self.assertEqual(self.view()["storage_status"], "upload_uncertain")

    def test_disabled_archive_with_no_external_evidence_does_not_block_document(self):
        self.unset_stage("archive", "held")
        self.settings["archive"]["enabled"] = False
        self.task("archive", "held")
        result = self.view()
        self.assertEqual(result["storage_status"], "saved_in_drive")
        self.assertEqual(result["storage"]["media"]["status"], "not_requested")

    def test_disabled_stage_with_old_unknown_write_stays_uncertain(self):
        self.settings["archive"]["enabled"] = False
        self.task("archive", "held")
        self.assertEqual(self.view()["storage_status"], "upload_uncertain")

    def test_local_files_are_not_evidence_that_drive_is_empty(self):
        self.db.execute("DELETE FROM tasks WHERE stage IN ('publish','archive')")
        self.db.commit()
        result = self.view()
        self.assertEqual(result["storage_status"], "not_confirmed")
        self.assertEqual(result["storage"]["media"]["status"], "unknown")

    def test_pending_route_does_not_show_suggested_destination(self):
        self.task("route", "held")
        atomic_json(self.result / "route.json", self.route)
        self.assertIsNone(self.view()["folder_id"])
        self.assertEqual(self.view()["storage_status"], "upload_uncertain")

    def test_route_must_match_its_frozen_company_map(self):
        self.settings["route"]["companies"][0]["drive_folder_id"] = "another_company_123"
        self.receipt("route", route=self.route)
        self.assertIsNone(self.view()["folder_name"])

    def test_nested_route_confirms_filing_only_when_the_complete_path_matches(self):
        path = [{"name": "Example", "drive_folder_id": "company_folder_123"},
                {"name": "Sales Meetings", "drive_folder_id": "sales_folder_123"}]
        self.route.update(company_id="example-sales", company_name="Example / Sales Meetings",
                          drive_folder_id="sales_folder_123", drive_folder_path=path)
        self.settings["route"]["companies"] = [{"id": "example-sales", "name": "Example / Sales Meetings",
            "drive_folder_id": "sales_folder_123", "drive_folder_path": json.loads(json.dumps(path))}]
        self.receipt("route", route=self.route)
        self.complete("publish")
        self.complete("archive")
        result = self.view()
        self.assertEqual(result["folder_name"], "Example / Sales Meetings")
        self.assertEqual(result["folder_id"], "sales_folder_123")
        self.assertEqual(result["storage_status"], "saved_in_drive")
        self.assertEqual(result["storage"]["media"]["status"], "confirmed")
        # The same folder and files must no longer confirm if the saved
        # filing choice and receipt disagree about who can see them.
        self.settings["route"]["companies"][0]["drive_reader_emails"] = ["viewer@example.com"]
        self.receipt("route", route=self.route)
        self.assertIsNone(self.view()["folder_name"])
        self.route["drive_reader_emails"] = ["viewer@example.com"]
        self.receipt("route", route=self.route)
        self.complete("publish")
        self.complete("archive")
        self.assertEqual(self.view()["storage_status"], "saved_in_drive")
        self.settings["route"]["companies"][0]["drive_folder_path"][0]["drive_folder_id"] = "changed_company_123"
        self.receipt("route", route=self.route)
        self.assertIsNone(self.view()["folder_name"])
        self.assertEqual(self.view()["storage_status"], "upload_uncertain")
        self.settings["route"]["companies"][0]["drive_folder_path"][0]["drive_folder_id"] = "../invalid"
        self.receipt("route", route=self.route)
        self.assertIsNone(self.view()["folder_name"])

    def test_changed_notes_invalidate_current_document_confirmation(self):
        self.notes["summary"][0]["text"] = "A later unpublished revision."
        atomic_json(self.result / "notes/notes.json", self.notes)
        result = self.view()
        self.assertEqual(result["storage"]["document"]["status"], "uncertain")
        self.assertEqual(result["storage"]["media"]["status"], "confirmed")

    def test_invalid_notes_fail_closed_without_crashing_library(self):
        self.notes["summary"][0]["evidence"] = [100]
        atomic_json(self.result / "notes/notes.json", self.notes)
        self.assertIsNone(self.view()["title"])

    def test_attached_transcript_takes_precedence_and_is_not_silently_bypassed(self):
        atomic_json(self.result / "input/transcript.json", {"segments": [{"speaker": "them", "start_ms": 0, "text": "A revised transcript."}]})
        result = self.view()
        self.assertIn("A revised transcript.", result["transcript_text"])
        self.assertIsNone(result["title"])
        (self.result / "input/transcript.json").write_text("not JSON")
        self.assertIsNone(self.view()["transcript_text"])

    def test_only_source_validated_diarized_names_become_participants(self):
        transcript = {"speaker_labels": "diarized_v1", "speakers": {
            "mic_speaker_1": {"label": "Microphone speaker 1", "source": "mic", "name": "Alex",
                              "name_source": "self_introduction", "evidence": [0]},
            "system_speaker_1": {"label": "Other speaker 1", "source": "system"}},
            "segments": [{"speaker": "mic_speaker_1", "source": "mic", "start_ms": 0, "text": "My name is Alex."},
                         {"speaker": "system_speaker_1", "source": "system", "start_ms": 1000, "text": "Hi Alex."}]}
        atomic_json(self.result / "input/transcript.json", transcript)
        result = self.view()
        self.assertEqual(result["participants"], ["Alex"])
        self.assertIn("Other speaker 1: Hi Alex.", result["transcript_text"])
        transcript["speakers"]["mic_speaker_1"]["name"] = "Someone Else"
        atomic_json(self.result / "input/transcript.json", transcript)
        self.assertEqual(self.view()["participants"], [])

    def test_transcript_is_plain_text_not_interpolated_html_and_is_bounded(self):
        transcript = {"segments": [{"speaker": "me", "start_ms": 0, "text": "<script>alert(1)</script>"}]}
        atomic_json(self.result / "input/transcript.json", transcript)
        self.assertIn("<script>alert(1)</script>", self.view()["transcript_text"])
        with patch("quill_workflow.review_library.MAX_TRANSCRIPT_CHARACTERS", 10):
            self.assertIn("[Transcript preview shortened.]", self.view()["transcript_text"])

    def test_symlinked_transcript_and_parent_are_not_read(self):
        original = self.recording / "transcript.json"
        outside = self.root / "private.json"
        outside.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(outside)
        self.assertIsNone(self.view()["transcript_text"])
        original.unlink()
        original.write_bytes(outside.read_bytes())
        moved = self.root / "outside-recording"
        self.recording.rename(moved)
        self.recording.symlink_to(moved, target_is_directory=True)
        result = self.view()
        self.assertIsNone(result["transcript_text"])
        self.assertIsNone(result["display_date"])

    def test_oversized_duplicate_nonfinite_and_special_files_are_not_read(self):
        path = self.result / "input/transcript.json"
        path.parent.mkdir(parents=True)
        for content in ('{"segments": [], "segments": []}', '{"segments": NaN}'):
            path.write_text(content)
            self.assertIsNone(self.view()["transcript_text"])
        path.write_bytes(b" " * 8_000_001)
        self.assertIsNone(self.view()["transcript_text"])
        path.unlink()
        os.mkfifo(path)
        self.assertIsNone(self.view()["transcript_text"])

    def test_tombstones_and_unsafe_job_identifiers_cannot_expose_files(self):
        self.db.execute("INSERT INTO jobs VALUES (?,?,?)", ("../escape", str(self.root), 1))
        self.db.execute("INSERT INTO local_deletions VALUES (?,?)", (self.job, '{"status":"cancelling"}'))
        self.db.commit()
        self.assertEqual(meeting_library(self.state), {})

    def test_missing_state_and_database_create_nothing(self):
        missing = self.root / "missing-state"
        self.assertEqual(meeting_library(missing), {})
        self.assertFalse(missing.exists())
        empty = self.root / "empty-state"
        empty.mkdir()
        self.assertEqual(meeting_library(empty), {})
        self.assertEqual(list(empty.iterdir()), [])

    def test_symlinked_state_or_database_are_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.state, target_is_directory=True)
        self.assertEqual(meeting_library(alias), {})
        self.db.close()
        database = self.state / "jobs.sqlite3"
        moved = self.root / "outside.sqlite3"
        database.rename(moved)
        database.symlink_to(moved)
        self.assertEqual(meeting_library(self.state), {})

    def test_library_does_not_mutate_tasks_or_call_queue_constructor(self):
        before = self.db.execute("SELECT * FROM tasks ORDER BY stage").fetchall()
        files_before = {str(path.relative_to(self.state)): path.read_bytes() for path in self.state.rglob("*") if path.is_file()}
        with patch("quill_workflow.pipeline.Queue", side_effect=AssertionError("Must not create Queue")):
            self.view()
        after = self.db.execute("SELECT * FROM tasks ORDER BY stage").fetchall()
        files_after = {str(path.relative_to(self.state)): path.read_bytes() for path in self.state.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(files_before, files_after)

    def test_recording_labels_are_fixed_and_paths_never_read(self):
        result = self.view()
        self.assertEqual(result["recording_files"], [
            {"kind": "mic", "label": "Microphone audio", "name": "mic.caf"},
            {"kind": "system", "label": "System audio", "name": "system.caf"}])
        self.assertEqual(result["local_folder"], str(self.root / "never-read-original"))
        self.metadata["files"].update(mic="../private.caf", system="linked/track.caf", screen="<script>.mp4", arbitrary="private.json")
        atomic_json(self.recording / "meta.json", self.metadata)
        # A valid plain basename is returned as data, never markup. The UI
        # must escape it; traversal and unrecognized kinds are omitted.
        self.assertEqual(self.view()["recording_files"], [{"kind": "screen", "label": "Screen video", "name": "<script>.mp4"}])

    def test_local_folder_uses_only_successful_persisted_rename(self):
        original = str(self.root / "never-read-original")
        destination = str(self.root / "never-read-renamed")
        self.db.execute("INSERT INTO local_names (job,settings,destination,device,inode,status) VALUES (?,?,?,?,?,?)",
                        (self.job, "{}", destination, 1, 1, "planned"))
        self.db.commit()
        self.assertEqual(self.view()["local_folder"], original)
        self.db.execute("UPDATE local_names SET status='succeeded' WHERE job=?", (self.job,))
        self.db.commit()
        self.assertEqual(self.view()["local_folder"], destination)
        self.assertFalse(Path(destination).exists())

    def test_malformed_one_job_does_not_hide_other_metadata(self):
        bad_job = "damaged-historical"
        self.db.execute("INSERT INTO jobs VALUES (?,?,?)", (bad_job, "/private/never-read", 1))
        self.db.execute("INSERT INTO tasks (job,stage,status,settings,receipt) VALUES (?,?,?,?,?)",
                        (bad_job, "publish", "succeeded", '{"bad":NaN}', "malformed"))
        self.db.commit()
        atomic_json(self.state / "recordings" / bad_job / "meta.json", {"started": [], "files": ["wrong shape"]})
        result = meeting_library(self.state)
        self.assertEqual(result[self.job]["storage_status"], "saved_in_drive")
        self.assertEqual(result[bad_job]["storage_status"], "not_confirmed")
        self.assertIsNone(result[bad_job]["display_date"])

    def test_archive_manifest_mismatch_and_unverified_fill_are_uncertain(self):
        self.manifest["mic.caf"]["sha256"] = "0" * 64
        atomic_json(self.recording / "manifest.json", self.manifest)
        self.assertEqual(self.view()["storage"]["media"]["status"], "uncertain")
        self.journals["publish"]["operations"]["file:native-template"]["fill"]["state"] = "dispatched"
        atomic_json(self.result / "drive-publish.json", self.journals["publish"])
        self.assertEqual(self.view()["storage"]["document"]["status"], "uncertain")

    def test_symlinked_sqlite_sidecar_is_not_followed(self):
        outside = self.root / "outside-wal"
        outside.write_bytes(b"never read or changed")
        (self.state / "jobs.sqlite3-wal").symlink_to(outside)
        self.assertEqual(meeting_library(self.state), {})
        self.assertEqual(outside.read_bytes(), b"never read or changed")

    def test_legacy_current_files_are_confirmed_but_later_notes_revision_is_not(self):
        self.older_publish("legacy")
        self.assertEqual(self.view()["storage"]["document"]["status"], "confirmed")
        original = (self.result / "notes/notes.json").read_bytes()
        self.notes["meeting_title"]["text"] = "A later local title"
        atomic_json(self.result / "notes/notes.json", self.notes)
        result = self.view()
        self.assertEqual(result["title"], "Max - A later local title")
        self.assertEqual(result["storage"]["document"]["status"], "uncertain")
        (self.result / "notes/notes.json").write_bytes(original)
        (self.result / "notes/notes.md").write_text("A later unpublished Markdown preview.")
        self.assertEqual(self.view()["storage"]["document"]["status"], "uncertain")

    def test_combined_current_provenance_confirms_but_changed_json_or_markdown_does_not(self):
        self.older_publish(COMBINED_TEXT_FORMAT)
        self.assertEqual(self.view()["storage"]["document"]["status"], "confirmed")
        original = (self.result / "notes/notes.json").read_bytes()
        self.notes["summary"][0]["text"] = "A later unpublished summary."
        atomic_json(self.result / "notes/notes.json", self.notes)
        self.assertEqual(self.view()["storage"]["document"]["status"], "uncertain")
        (self.result / "notes/notes.json").write_bytes(original)
        (self.result / "notes/notes.md").write_text("A later unpublished Markdown preview.")
        self.assertEqual(self.view()["storage"]["document"]["status"], "uncertain")

    def test_combined_missing_manifest_changed_export_or_changed_readback_binding_stays_uncertain(self):
        self.older_publish(COMBINED_TEXT_FORMAT)
        manifest = self.result / "documents/manifest.json"
        original = manifest.read_bytes()
        manifest.unlink()
        self.assertEqual(self.view()["storage"]["document"]["status"], "uncertain")
        manifest.write_bytes(original)
        export = self.result / "documents/meeting.docx"
        original_export = export.read_bytes()
        export.write_bytes(b"changed docx")
        self.assertEqual(self.view()["storage"]["document"]["status"], "uncertain")
        export.write_bytes(original_export)
        value = json.loads(original)
        value["paragraphs"].append("Unverified replacement content")
        atomic_json(manifest, value)
        self.assertEqual(self.view()["storage"]["document"]["status"], "uncertain")

    def test_legacy_missing_or_symlinked_notes_never_confirm(self):
        self.older_publish("legacy")
        path = self.result / "notes/notes.md"
        original = path.read_bytes()
        path.unlink()
        self.assertEqual(self.view()["storage"]["document"]["status"], "uncertain")
        outside = self.root / "private.md"
        outside.write_bytes(original)
        path.symlink_to(outside)
        self.assertEqual(self.view()["storage"]["document"]["status"], "uncertain")

    def test_native_independently_edited_markdown_cannot_inherit_saved_status(self):
        path = self.result / "notes/notes.md"
        path.write_text(render_notes(self.notes, self.transcript))
        self.assertEqual(self.view()["storage"]["document"]["status"], "confirmed")
        path.write_text("A later unpublished preview that was never filed.")
        result = self.view()
        self.assertEqual(result["storage"]["document"]["status"], "uncertain")
        self.assertEqual(result["storage_status"], "upload_uncertain")
        self.assertEqual(result["storage"]["media"]["status"], "confirmed")

    def test_topic_first_native_document_and_saved_markdown_remain_confirmed(self):
        self.complete("publish", participants_first=False)
        path = self.result / "notes/notes.md"
        path.write_text(render_notes(self.notes, self.transcript, participants_first=False))
        before = path.read_bytes()
        self.assertEqual(self.view()["storage"]["document"]["status"], "confirmed")
        self.assertEqual(path.read_bytes(), before)
        path.write_text(before.decode().replace("Review the proposed dashboard.", "Unpublished changes."))
        self.assertEqual(self.view()["storage"]["document"]["status"], "uncertain")

    def test_topic_first_docx_and_saved_markdown_remain_confirmed(self):
        self.older_publish(COMBINED_TEXT_FORMAT, participants_first=False)
        self.assertEqual(self.view()["storage"]["document"]["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
