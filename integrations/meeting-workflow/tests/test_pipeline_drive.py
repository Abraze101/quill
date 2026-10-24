"""Drive integration tests use synthetic files and never invoke a model or cloud."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.pipeline import (ConnectorRunner, NeedsConfiguration, Queue, RecoverableUploadError,
                                     WorkflowError, atomic_json, configured)
from quill_workflow.routing import RoutingReviews


class FakeNeedsReview(Exception):
    pass


class FakeRetryable(Exception):
    pass


class PipelineDriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = self.root / "recordings/synthetic-meeting"
        self.session.mkdir(parents=True)
        for name in ("mic.caf", "system.caf"):
            (self.session / name).write_bytes(b"synthetic fixture, not recorded audio")
        self.started = "2026-01-01T10:00:00Z"
        atomic_json(self.session / "meta.json", {"started": self.started,
                    "ended": "2026-01-01T10:01:00Z",
                    "files": {"mic": "mic.caf", "system": "system.caf"}})
        atomic_json(self.session / "transcript.json", {
            "segments": [{"start_ms": 0, "text": "Acme will provide the agenda."}]})
        self.companies = [{"id": "acme", "name": "Acme", "drive_folder_id": "company_folder_123"}]
        self.settings = {"enabled": True, "type": "codex_drive", "meetings_root_id": "meetings_root_123"}
        self.config = {"recordings_root": str(self.session.parent),
                       "archive": dict(self.settings),
                       "publish": dict(self.settings),
                       "route": {"enabled": True, "companies": self.companies, "command": ["unused"]}}
        self.queue = Queue(self.root / "state")
        self.reviews = RoutingReviews(self.queue.state)
        self.runner = ConnectorRunner(self.queue.state, ROOT / "skills/call-summary/SKILL.md")
        self.uploader = Mock()
        self.transport = Mock()
        self.uploader_factory = Mock(return_value=self.uploader)
        self.transport_factory = Mock(return_value=self.transport)
        upload_module = types.ModuleType("quill_workflow.drive_upload")
        upload_module.DriveUploader = self.uploader_factory
        upload_module.UploadNeedsReview = FakeNeedsReview
        upload_module.UploadRetryable = FakeRetryable
        transport_module = types.ModuleType("quill_workflow.codex_drive_transport")
        transport_module.CodexDriveTransport = self.transport_factory
        self.modules = patch.dict(sys.modules, {upload_module.__name__: upload_module,
                                                transport_module.__name__: transport_module})
        self.modules.start()

    def tearDown(self):
        self.modules.stop()
        self.reviews.close()
        self.queue.close()
        self.temp.cleanup()

    def prepare(self, config=None):
        job = self.queue.enqueue(self.session, config or self.config)
        transcript = self.queue.state / "recordings" / job / "transcript.json"
        review = self.reviews.propose(job, transcript.read_bytes(), self.companies, {
            "schema_version": 1, "company_id": "acme", "confidence": "high",
            "reason": "Explicit company context.", "evidence": [0], "ambiguous": False})
        self.reviews.approve(job, "acme", review["revision"])
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='succeeded' WHERE job=? AND stage IN ('route','notes')", (job,))
        notes = self.queue.state / "results" / job / "notes"
        notes.mkdir(parents=True)
        (notes / "notes.md").write_text("Synthetic notes")
        self.uploader.upload.side_effect = lambda job, stage, *args, **kwargs: {
            "job": job, "stage": stage, "idempotency_key": job + ":" + stage,
            "destination": "verified-destination", "folder_id": "meeting_folder_123",
            "files": [], "verification": "metadata", "verified_at": "2026-01-01T10:02:00Z"}
        return job, transcript

    def test_codex_drive_configuration_only_accepts_external_stages_and_raw_root_id(self):
        for stage in ("archive", "publish"):
            self.assertTrue(configured(self.settings, stage))
        for stage in ("notes", "route"):
            self.assertFalse(configured(dict(self.settings, command=["unused"]), stage))
        for root in (None, "root", "https://drive.google.com/drive/folders/example", "../folder", 123):
            self.assertFalse(configured(dict(self.settings, meetings_root_id=root), "archive"))
        self.assertFalse(configured(dict(self.settings, enabled=False), "archive"))
        self.assertFalse(configured(dict(self.settings, codex=""), "archive"))
        self.assertFalse(configured(dict(self.settings, model=False), "archive"))
        self.assertFalse(configured(dict(self.settings, text_format="anything"), "publish"))
        self.assertFalse(configured(dict(self.settings, document_timezone=False), "publish"))
        self.assertFalse(configured(dict(self.settings, notes_layout="unknown"), "publish"))
        self.assertTrue(configured(dict(self.settings, notes_layout="clean"), "publish"))
        self.assertTrue(configured(dict(self.settings, text_format="combined_google_doc_v1",
                                        document_timezone="America/Los_Angeles"), "publish"))

    def test_large_file_configuration_is_explicit_archive_only_and_contains_no_tokens(self):
        large = {"type": "google_drive_resumable", "credentials_file": "/fixture/private-drive.json"}
        self.assertTrue(configured(dict(self.settings, large_files=large), "archive"))
        self.assertFalse(configured(dict(self.settings, large_files=large), "publish"))
        for value in [False, {}, {"type": "unknown", "credentials_file": "/fixture/private-drive.json"},
                      dict(large, credentials_file="relative.json"), dict(large, access_token="secret")]:
            self.assertFalse(configured(dict(self.settings, large_files=value), "archive"))

    def test_large_media_provider_is_wired_only_when_explicitly_configured(self):
        job, _ = self.prepare()
        auth = Mock()
        media = Mock()
        auth_module = types.ModuleType("quill_workflow.google_drive_auth")
        auth_module.GoogleDriveAuth = Mock(return_value=auth)
        media_module = types.ModuleType("quill_workflow.resumable_drive")
        media_module.ResumableDriveUploader = Mock(return_value=media)
        settings = dict(self.settings, large_files={"type": "google_drive_resumable",
                                                   "credentials_file": "/fixture/private-drive.json"})
        with patch.dict(sys.modules, {auth_module.__name__: auth_module, media_module.__name__: media_module}):
            self.runner("archive", job, settings)
        auth_module.GoogleDriveAuth.assert_called_once_with("/fixture/private-drive.json")
        media_module.ResumableDriveUploader.assert_called_once_with(auth.access_token)
        options = self.uploader_factory.call_args.kwargs
        self.assertIs(options["large_upload"], media)
        self.assertEqual(options["large_upload_preflight"], auth.preflight)
        auth.preflight.assert_not_called()
        auth.access_token.assert_not_called()

    def test_direct_transfer_guard_holds_if_connection_or_enabled_state_changes(self):
        job, _ = self.prepare()
        config_path = self.root / "workflow.json"
        large = {"type": "google_drive_resumable", "credentials_file": "/fixture/private-drive.json"}
        settings = dict(self.settings, large_files=large)
        config = dict(self.config, archive=settings)
        atomic_json(config_path, config)
        self.runner.config_path = config_path
        auth_module = types.ModuleType("quill_workflow.google_drive_auth")
        auth_module.GoogleDriveAuth = Mock()
        media_module = types.ModuleType("quill_workflow.resumable_drive")
        media_module.ResumableDriveUploader = Mock()
        with patch.dict(sys.modules, {auth_module.__name__: auth_module, media_module.__name__: media_module}):
            self.runner("archive", job, settings)
        guard = self.uploader_factory.call_args.kwargs["guard"]
        guard()
        atomic_json(config_path, dict(config, archive=dict(settings, enabled=False)))
        with self.assertRaisesRegex(NeedsConfiguration, "disabled or its connection changed"):
            guard()
        atomic_json(config_path, dict(config, archive=dict(settings, large_files=dict(large, credentials_file="/different.json"))))
        with self.assertRaisesRegex(NeedsConfiguration, "disabled or its connection changed"):
            guard()

    def test_archive_and_publish_use_correct_sources_and_frozen_company(self):
        job, _ = self.prepare()
        expected_route = {"company_id": "acme", "company_name": "Acme", "drive_folder_id": "company_folder_123"}
        for stage, source in (("archive", self.queue.state / "recordings" / job),
                              ("publish", self.queue.state / "results" / job / "notes")):
            with self.subTest(stage=stage):
                receipt = self.runner(stage, job, self.settings)
                self.uploader.upload.assert_called_with(job, stage, source, expected_route, self.started, meeting_title=None)
                self.assertEqual(receipt["verification"], "metadata")
                self.assertIn("settings_sha256", receipt)
                self.assertIn("input_sha256", receipt)
                self.assertIn("route_sha256", receipt)
        self.transport_factory.assert_called_with(
            "/Applications/Codex.app/Contents/Resources/codex", model=None, timeout=180)
        args, kwargs = self.uploader_factory.call_args
        self.assertEqual(args, (self.queue.state, self.transport.invoke, "meetings_root_123"))
        self.assertTrue(callable(kwargs["guard"]))
        self.transport.invoke.assert_not_called()

    def test_configured_model_executable_and_timeout_are_forwarded(self):
        job, _ = self.prepare()
        settings = dict(self.settings, codex="/fixture/codex", model="configured-model", timeout_seconds=90)
        self.runner("archive", job, settings)
        self.transport_factory.assert_called_once_with("/fixture/codex", model="configured-model", timeout=90)

    def recorded_date_fixture(self):
        self.started = "2026-09-28T05:12:41Z"
        metadata = json.loads((self.session / "meta.json").read_text())
        metadata.update(started=self.started, ended="2026-09-28T05:13:41Z",
                        recording_timezone="Asia/Singapore", recording_utc_offset_seconds=28800)
        atomic_json(self.session / "meta.json", metadata)
        settings = dict(self.settings, text_format="native_template_google_doc_v1",
                        template_document_id="template_document123", template_sha256="a" * 64,
                        document_timezone="America/Los_Angeles")
        return dict(self.config, archive=settings, publish=settings, local_naming={"enabled": True})

    def test_new_recording_timezone_controls_both_folder_and_native_document(self):
        config = self.recorded_date_fixture()
        job, transcript = self.prepare(config)
        self.titled_notes(job, transcript)
        frozen = {row["stage"]: json.loads(row["settings"]) for row in self.queue.tasks(job)}
        naming = json.loads(self.queue.db.execute("SELECT settings FROM local_names WHERE job=?", (job,)).fetchone()[0])
        for stage in ("archive", "publish"):
            self.assertEqual(frozen[stage]["meeting_timezone"], "Asia/Singapore")
            self.runner(stage, job, frozen[stage])
            self.assertEqual(self.uploader.upload.call_args.kwargs["meeting_timezone"], "Asia/Singapore")
        self.assertEqual(naming["meeting_timezone"], "Asia/Singapore")
        options = self.uploader.upload.call_args.kwargs
        self.assertEqual(options["document_title"], "Agenda planning - 9/28/26")
        self.assertEqual(options["template_payload"]["date"]["day"], 28)
        self.assertEqual(options["template_payload"]["date"]["timezone"], "Asia/Singapore")

    def test_repeated_hooks_and_release_preserve_frozen_recording_timezone(self):
        config = self.recorded_date_fixture()
        job, _ = self.prepare(config)
        changed = dict(config, archive=dict(config["archive"], document_timezone="UTC", meeting_timezone="UTC"),
                       publish=dict(config["publish"], document_timezone="UTC", meeting_timezone="UTC"))
        before = self.queue.tasks(job)
        self.assertEqual(self.queue.enqueue(self.session, changed), job)
        self.assertEqual(self.queue.tasks(job), before)
        for stage in ("archive", "publish"):
            with self.queue.db:
                self.queue.db.execute("UPDATE tasks SET status='held' WHERE job=? AND stage=?", (job, stage))
            self.queue.release(job, stage, changed)
        for row in self.queue.tasks(job):
            if row["stage"] in ("archive", "publish"):
                self.assertEqual(json.loads(row["settings"])["meeting_timezone"], "Asia/Singapore")

    def test_mismatched_or_missing_frozen_zone_never_starts_transport(self):
        config = self.recorded_date_fixture()
        job, _ = self.prepare(config)
        for zone in (None, "America/Los_Angeles"):
            with self.subTest(zone=zone):
                settings = dict(config["archive"])
                if zone is not None:
                    settings["meeting_timezone"] = zone
                with self.assertRaisesRegex(NeedsConfiguration, "timezone differs"):
                    self.runner("archive", job, settings)
        self.transport_factory.assert_not_called()

    def test_invalid_recording_timezone_does_not_create_a_job_or_upload(self):
        config = self.recorded_date_fixture()
        metadata = json.loads((self.session / "meta.json").read_text())
        metadata["recording_utc_offset_seconds"] = 0
        atomic_json(self.session / "meta.json", metadata)
        with self.assertRaisesRegex(WorkflowError, "UTC offset"):
            self.queue.enqueue(self.session, config)
        self.assertEqual(self.queue.tasks(), [])
        self.transport_factory.assert_not_called()

    def titled_notes(self, job, transcript):
        notes_dir = self.queue.state / "results" / job / "notes"
        notes = {"schema_version": 1, "summary": [{"text": "Agenda planning", "evidence": [0]}],
                 "meeting_title": {"text": "Agenda planning", "evidence": [0], "participants": []}}
        receipt = {"input_sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
                   "settings_sha256": "synthetic-settings"}
        atomic_json(notes_dir / "notes.json", notes)
        atomic_json(notes_dir / "provenance.json", receipt)
        atomic_json(notes_dir.parent / "notes-receipt.json", receipt)
        (notes_dir / "source-transcript.json").write_bytes(transcript.read_bytes())
        return notes_dir

    def test_title_from_matching_accepted_notes_reaches_both_upload_stages(self):
        job, transcript = self.prepare()
        self.titled_notes(job, transcript)
        for stage in ("archive", "publish"):
            with self.subTest(stage=stage):
                self.runner(stage, job, self.settings)
                self.assertEqual(self.uploader.upload.call_args.kwargs["meeting_title"], "Agenda planning")

    def test_title_with_wrong_provenance_or_cited_source_never_starts_transport(self):
        for changed in ("receipt", "provenance", "source", "title", "held"):
            with self.subTest(changed=changed):
                job, transcript = self.prepare()
                notes_dir = self.titled_notes(job, transcript)
                if changed in ("receipt", "provenance"):
                    path = notes_dir.parent / "notes-receipt.json" if changed == "receipt" else notes_dir / "provenance.json"
                    value = json.loads(path.read_text())
                    value["input_sha256"] = "wrong"
                    atomic_json(path, value)
                elif changed == "source":
                    (notes_dir / "source-transcript.json").write_text("Different source")
                elif changed == "title":
                    value = json.loads((notes_dir / "notes.json").read_text())
                    value["meeting_title"]["participants"] = [{"name": "Max", "evidence": [0]}]
                    atomic_json(notes_dir / "notes.json", value)
                else:
                    with self.queue.db:
                        self.queue.db.execute("UPDATE tasks SET status='held' WHERE job=? AND stage='notes'", (job,))
                with self.assertRaises(WorkflowError):
                    self.runner("archive", job, self.settings)
                self.transport_factory.assert_not_called()
                # Each subcase uses the same immutable job; restore task state
                # and remove generated fixtures before recreating its review.
                for path in notes_dir.iterdir():
                    path.unlink()
                notes_dir.rmdir()

    def test_combined_publish_builds_one_source_checked_document_and_archive_is_media_only(self):
        job, transcript = self.prepare()
        self.titled_notes(job, transcript)
        settings = dict(self.settings, text_format="combined_google_doc_v1",
                        document_timezone="America/Los_Angeles", notes_layout="clean")
        self.runner("archive", job, settings)
        options = self.uploader.upload.call_args.kwargs
        self.assertEqual(options["text_format"], "combined_google_doc_v1")
        self.assertNotIn("document_path", options)
        self.runner("publish", job, settings)
        options = self.uploader.upload.call_args.kwargs
        self.assertEqual(options["text_format"], "combined_google_doc_v1")
        self.assertEqual(options["document_title"], "Agenda planning - Notes and transcript")
        self.assertEqual(options["document_path"], self.queue.state / "results" / job / "documents/meeting.docx")
        self.assertTrue(options["document_path"].is_file())
        self.assertIn("Full transcript", options["document_text"])
        before_transcript = options["document_text"][:options["document_text"].index("Full transcript")]
        self.assertFalse(any("Source " in paragraph for paragraph in before_transcript))
        manifest = json.loads((options["document_path"].parent / "manifest.json").read_text())
        self.assertEqual(manifest["notes_layout"], "clean")
        self.assertIn("Acme will provide the agenda.", options["document_text"][-1])
        self.assertEqual(self.uploader.upload.call_args.args[2], self.queue.state / "results" / job)

    def test_native_template_publish_validates_notes_and_passes_frozen_template(self):
        job, transcript = self.prepare()
        self.titled_notes(job, transcript)
        settings = dict(self.settings, text_format="native_template_google_doc_v1",
                        template_document_id="template_document123", template_sha256="a" * 64,
                        document_timezone="America/Los_Angeles")
        self.assertTrue(configured(settings, "publish"))
        self.assertTrue(configured(dict(self.settings, text_format="native_template_google_doc_v1"), "archive"))
        for invalid in (dict(settings, template_document_id="root"), dict(settings, template_sha256="oops"),
                        {key: value for key, value in settings.items() if key != "template_sha256"}):
            self.assertFalse(configured(invalid, "publish"))
        self.runner("archive", job, settings)
        self.assertEqual(self.uploader.upload.call_args.kwargs, {
            "meeting_title": "Agenda planning", "text_format": "native_template_google_doc_v1"})
        with patch("quill_workflow.native_template.build_payload", return_value={"title": "Agenda planning - 1/1/26"}) as build:
            self.runner("publish", job, settings)
        options = self.uploader.upload.call_args.kwargs
        self.assertEqual(options["document_title"], "Agenda planning - 1/1/26")
        self.assertEqual(options["template_document_id"], settings["template_document_id"])
        self.assertEqual(options["template_sha256"], settings["template_sha256"])
        self.assertEqual(options["template_payload"], build.return_value)
        self.assertEqual(options["text_format"], "native_template_google_doc_v1")
        self.assertNotIn("document_path", options)
        self.assertFalse((self.queue.state / "results" / job / "documents/meeting.docx").exists())
        self.assertEqual(build.call_args.args[-1], "America/Los_Angeles")

    def test_native_template_release_keeps_original_template_and_old_jobs_do_not_gain_one(self):
        settings = dict(self.settings, text_format="native_template_google_doc_v1",
                        template_document_id="template_document123", template_sha256="a" * 64)
        config = dict(self.config, publish=settings)
        job, _ = self.prepare(config)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='held' WHERE job=? AND stage IN ('publish','archive')", (job,))
        changed = dict(config, publish=dict(settings, template_document_id="template_document456", template_sha256="b" * 64),
                       archive=settings)
        self.queue.release(job, "publish", changed)
        self.queue.release(job, "archive", changed)
        frozen = {row["stage"]: json.loads(row["settings"]) for row in self.queue.tasks(job)}
        self.assertEqual(frozen["publish"]["template_document_id"], "template_document123")
        self.assertEqual(frozen["publish"]["template_sha256"], "a" * 64)
        self.assertNotIn("template_document_id", frozen["archive"])
        self.assertNotIn("template_sha256", frozen["archive"])
        self.assertNotIn("text_format", frozen["archive"])

    def test_combined_document_checks_provenance_even_without_new_title_field(self):
        job, transcript = self.prepare()
        notes_dir = self.titled_notes(job, transcript)
        notes = json.loads((notes_dir / "notes.json").read_text())
        del notes["meeting_title"]
        atomic_json(notes_dir / "notes.json", notes)
        provenance = json.loads((notes_dir / "provenance.json").read_text())
        provenance["input_sha256"] = "wrong"
        atomic_json(notes_dir / "provenance.json", provenance)
        settings = dict(self.settings, text_format="combined_google_doc_v1")
        with self.assertRaisesRegex(NeedsConfiguration, "different source"):
            self.runner("publish", job, settings)
        self.transport_factory.assert_not_called()
        self.assertFalse((notes_dir.parent / "documents/meeting.docx").exists())

    def test_combined_document_missing_notes_does_not_upload_raw_text(self):
        job, _ = self.prepare()
        settings = dict(self.settings, text_format="combined_google_doc_v1")
        with self.assertRaisesRegex(NeedsConfiguration, "validated notes"):
            self.runner("publish", job, settings)
        self.uploader.upload.assert_not_called()

    def test_release_legacy_held_upload_preserves_absent_format_and_timezone(self):
        job, _ = self.prepare()
        config = dict(self.config)
        for stage in ("archive", "publish"):
            config[stage] = dict(self.settings, text_format="combined_google_doc_v1",
                                 document_timezone="America/Los_Angeles", notes_layout="clean", meeting_timezone="UTC")
            with self.queue.db:
                self.queue.db.execute("UPDATE tasks SET status='held' WHERE job=? AND stage=?", (job, stage))
            self.queue.release(job, stage, config)
            task = next(task for task in self.queue.tasks(job) if task["stage"] == stage)
            frozen = json.loads(task["settings"])
            self.assertEqual(task["status"], "pending")
            self.assertNotIn("text_format", frozen)
            self.assertNotIn("document_timezone", frozen)
            self.assertNotIn("meeting_timezone", frozen)
            self.assertNotIn("notes_layout", frozen)

    def test_release_after_legacy_receipt_preserves_retry_identity(self):
        job, _ = self.prepare()
        task = next(task for task in self.queue.tasks(job) if task["stage"] == "archive")
        frozen = json.loads(task["settings"])
        receipt = self.runner("archive", job, frozen)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='failed',attempts=1 WHERE job=? AND stage='archive'", (job,))
        config = dict(self.config, archive=dict(self.settings, text_format="combined_google_doc_v1",
                                                document_timezone="America/Los_Angeles"))
        self.queue.release(job, "archive", config)
        task = next(task for task in self.queue.tasks(job) if task["stage"] == "archive")
        self.assertEqual(json.loads(task["settings"]), frozen)
        self.assertEqual(self.runner("archive", job, json.loads(task["settings"])), receipt)
        self.uploader.upload.assert_called_once()

    def test_release_combined_upload_preserves_its_original_timezone(self):
        config = dict(self.config, publish=dict(self.settings, text_format="combined_google_doc_v1",
                                                document_timezone="America/Los_Angeles", notes_layout="clean"))
        job, _ = self.prepare(config)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='held' WHERE job=? AND stage='publish'", (job,))
        changed = dict(config, publish=dict(self.settings, text_format="legacy", document_timezone="UTC", notes_layout="sources"))
        self.queue.release(job, "publish", changed)
        task = next(task for task in self.queue.tasks(job) if task["stage"] == "publish")
        frozen = json.loads(task["settings"])
        self.assertEqual(frozen["text_format"], "combined_google_doc_v1")
        self.assertEqual(frozen["document_timezone"], "America/Los_Angeles")
        self.assertEqual(frozen["notes_layout"], "clean")

    def test_legacy_job_cannot_use_drive_without_a_saved_route_stage(self):
        legacy = {"recordings_root": str(self.session.parent), "archive": self.settings}
        job = self.queue.enqueue(self.session, legacy)
        with self.assertRaisesRegex(NeedsConfiguration, "routing review"):
            self.runner("archive", job, self.settings)
        self.transport_factory.assert_not_called()

    def test_disabled_saved_route_cannot_use_drive_even_with_an_approval(self):
        job, _ = self.prepare()
        settings = dict(self.config["route"], enabled=False)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET settings=? WHERE job=? AND stage='route'", (json.dumps(settings), job))
        with self.assertRaisesRegex(NeedsConfiguration, "routing review"):
            self.runner("archive", job, self.settings)
        self.uploader_factory.assert_not_called()

    def test_cached_receipt_does_not_repeat_upload_but_still_checks_transcript(self):
        job, transcript = self.prepare()
        first = self.runner("archive", job, self.settings)
        second = self.runner("archive", job, self.settings)
        self.assertEqual(first, second)
        self.uploader.upload.assert_called_once()
        changed = json.loads(transcript.read_text())
        changed["segments"][0]["text"] = "Changed source"
        atomic_json(transcript, changed)
        with self.assertRaisesRegex(NeedsConfiguration, "Transcript or company folders changed"):
            self.runner("archive", job, self.settings)
        self.uploader.upload.assert_called_once()

    def test_guard_checks_changes_between_individual_upload_writes(self):
        job, transcript = self.prepare()
        writes = []
        def changing_upload(*args, **kwargs):
            guard = self.uploader_factory.call_args.kwargs["guard"]
            guard()
            writes.append("first permitted write")
            transcript.write_bytes(transcript.read_bytes() + b"\n")
            guard()
            writes.append("must never run")
        self.uploader.upload.side_effect = changing_upload
        with self.assertRaisesRegex(NeedsConfiguration, "Transcript or company folders changed"):
            self.runner("archive", job, self.settings)
        self.assertEqual(writes, ["first permitted write"])
        self.assertFalse((self.queue.state / "results" / job / "archive-receipt.json").exists())

    def test_guard_checks_saved_routing_status_before_next_write(self):
        job, _ = self.prepare()
        def stopped_route(*args, **kwargs):
            with self.queue.db:
                self.queue.db.execute("UPDATE tasks SET status='held' WHERE job=? AND stage='route'", (job,))
            self.uploader_factory.call_args.kwargs["guard"]()
        self.uploader.upload.side_effect = stopped_route
        with self.assertRaisesRegex(NeedsConfiguration, "routing review"):
            self.runner("archive", job, self.settings)

    def test_review_required_upload_error_holds_task_without_retry(self):
        job, _ = self.prepare()
        self.uploader.upload.side_effect = FakeNeedsReview("Ambiguous prior upload needs review")
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='held' WHERE job=? AND stage='publish'", (job,))
        self.queue.run_ready(self.runner, now=100)
        task = next(task for task in self.queue.tasks(job) if task["stage"] == "archive")
        self.assertEqual((task["status"], task["attempts"]), ("held", 1))
        self.assertIn("Ambiguous prior upload", task["error"])

    def test_retryable_upload_error_uses_existing_queue_backoff(self):
        job, _ = self.prepare()
        self.uploader.upload.side_effect = FakeRetryable("Connection unavailable")
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='held' WHERE job=? AND stage='publish'", (job,))
        self.queue.run_ready(self.runner, now=100)
        task = next(task for task in self.queue.tasks(job) if task["stage"] == "archive")
        self.assertEqual((task["status"], task["attempts"], task["next_attempt"]), ("retry", 1, 130))
        self.queue.run_ready(self.runner, now=129)
        self.uploader.upload.assert_called_once()

    def test_reserved_upload_error_survives_runner_and_keeps_automatic_retry(self):
        job, _ = self.prepare()
        interrupted = RecoverableUploadError("Reserved upload connection unavailable")
        self.uploader.upload.side_effect = interrupted
        with self.assertRaises(RecoverableUploadError) as raised:
            self.runner("archive", job, self.settings)
        self.assertIs(raised.exception, interrupted)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='held' WHERE job=? AND stage='publish'", (job,))
            self.queue.db.execute("UPDATE tasks SET attempts=5 WHERE job=? AND stage='archive'", (job,))
        self.queue.run_ready(self.runner, now=100)
        task = next(task for task in self.queue.tasks(job) if task["stage"] == "archive")
        self.assertEqual((task["status"], task["attempts"], task["next_attempt"]), ("retry", 6, 1060))
        self.assertIsNone(task["receipt"])

    def test_upload_receipt_cannot_replace_worker_provenance(self):
        job, _ = self.prepare()
        original = self.uploader.upload.side_effect
        self.uploader.upload.side_effect = lambda *args, **kwargs: dict(original(*args, **kwargs), input_sha256="wrong")
        with self.assertRaisesRegex(WorkflowError, "conflicting reserved"):
            self.runner("archive", job, self.settings)
        self.assertFalse((self.queue.state / "results" / job / "archive-receipt.json").exists())

    def test_generic_adapter_cannot_replace_worker_provenance(self):
        job, _ = self.prepare()
        settings = {"enabled": True, "command": ["unused", "{output}"]}
        def execute(argv, *_):
            atomic_json(argv[1], {"job": job, "stage": "archive", "idempotency_key": job + ":archive",
                                 "destination": "somewhere", "settings_sha256": "wrong"})
        self.runner.execute = execute
        with self.assertRaisesRegex(WorkflowError, "conflicting reserved"):
            self.runner("archive", job, settings)

    def test_matching_reserved_receipt_fields_remain_acceptable(self):
        base = {"job": "test", "stage": "archive", "idempotency_key": "test:archive",
                "input_sha256": hashlib.sha256(b"fixture").hexdigest()}
        returned = dict(base, destination="verified")
        self.assertEqual(self.runner.merge_external_receipt(base, returned), returned)


if __name__ == "__main__":
    unittest.main()
