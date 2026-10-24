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
from quill_workflow.review_library import meeting_library


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

    def complete(self, stage):
        if stage == "publish":
            payload = build_payload(self.notes, self.transcript, self.metadata, self.route, "Asia/Singapore")
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

    def unset_stage(self, stage, status="pending"):
        self.task(stage, status)
        (self.result / (stage + "-receipt.json")).unlink(missing_ok=True)
        (self.result / ("drive-" + stage + ".json")).unlink(missing_ok=True)

    def older_publish(self, text_format):
        self.settings["publish"]["text_format"] = text_format
        (self.result / "notes/notes.md").write_text(render_notes(self.notes, self.transcript))
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
                                      self.result / "documents", "Asia/Singapore")
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
        self.assertEqual(result["title"], "Dashboard review - Max")
        self.assertEqual(result["folder_name"], "Example")
        self.assertEqual(result["folder_id"], "company_folder_123")
        self.assertEqual(result["storage_status"], "saved_in_drive")
        self.assertEqual(result["storage"]["document"]["file_ids"], ["remote_publish_0_123"])
        self.assertEqual(result["storage"]["media"]["status"], "confirmed")
        self.assertEqual(result["participants"], [])  # A mentioned Max is not proof he attended.

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
        self.assertEqual(result["title"], "A later local title - Max")
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


if __name__ == "__main__":
    unittest.main()
