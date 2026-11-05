"""Speaker corrections stay selected, explicit and protected by review gates."""
import copy
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.review_server import MAX_FORM_BYTES, create_server, _selection, _notes_preview
from test_review_presentation import by_class, parsed


class SpeakerReviewServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.reads, self.calls = [], []
        self.items = [{"job": job, "revision": "sharing-revision", "title": title,
                       "status": "available", "preview": "# " + title,
                       "recipients": [], "cancellable": True}
                      for job, title in (("earlier", "Earlier meeting"), ("later", "Later meeting"))]
        self.options = {
            "job": "later", "revision": "speaker-revision", "speakers": [
                {"id": "mic_speaker_1", "label": "Microphone · Voice 1", "name": "Jamie",
                 "email": "jamie@example.com", "status": "user_confirmed", "remembered": True,
                 "samples": ["I can send the draft tomorrow."]},
                {"id": "system_speaker_1", "label": "Meeting audio · Voice 1", "name": "Morgan",
                 "email": None, "status": "suggested", "reason": "Name mentioned without an introduction.",
                 "samples": ["Morgan, could you review it?"]}],
            "candidates": [{"name": "Morgan", "email": "morgan@example.com", "source": "calendar"}],
            "unassigned_segments": 2,
        }
        def options(job):
            self.reads.append(job)
            return copy.deepcopy(self.options) if job == "later" else None
        self.server = create_server(
            Path(self.temporary.name), sharing_options=lambda: self.items,
            on_sharing=lambda *a, **kw: self.fail("Speaker corrections must not share notes"),
            speaker_options=options,
            on_speaker=lambda *a, **kw: self.calls.append((a, kw)))
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()
        self.temporary.cleanup()

    def request(self, path=None, fields=None, headers=None, raw=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        base = {"Host": self.server.review_host}
        is_post = fields is not None or raw is not None
        body = raw if raw is not None else urlencode(fields) if fields is not None else None
        if is_post:
            base.update({"Origin": self.server.review_origin,
                         "Content-Type": "application/x-www-form-urlencoded"})
        base.update(headers or {})
        try:
            connection.request("POST" if is_post else "GET", path or self.server.review_path, body, base)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def detail(self, job="later", tab="speakers"):
        return self.request(self.server.review_path + "?" + urlencode({"meeting": _selection(job), "tab": tab}))

    def fields(self, action="speaker_save", **changes):
        result = {"action": action, "job": "later", "revision": "speaker-revision", "speaker_id": "mic_speaker_1"}
        if action == "speaker_save":
            result.update(name="Jamie", email="jamie@example.com")
        result.update(changes)
        return result

    def post(self, action="speaker_save", **changes):
        return self.request(self.server.review_path + "decision", self.fields(action, **changes))

    def test_selected_tab_only_loads_selected_speaker_details(self):
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.detail(tab="notes")[0], 200)
        self.assertEqual(self.detail(tab="transcript")[0], 200)
        self.assertEqual(self.reads, [])
        status, headers, body = self.detail()
        self.assertEqual(status, 200)
        self.assertEqual(self.reads, ["later"])
        document = parsed(body)
        self.assertNotIn("Earlier meeting", document.text)
        self.assertIn("Name mentioned without an introduction.", document.text)
        self.assertIn("Your correction", document.text)
        self.assertIn("Suggested", document.text)
        self.assertNotIn("mic_speaker_1", document.text)
        self.assertNotIn("system_speaker_1", document.text)
        self.assertIn("2 speech segments", document.text)
        self.assertIn("Existing Drive documents stay unchanged", document.text)
        self.assertEqual([node.attributes["value"] for node in document.find("input", name="speaker_id")],
                         ["mic_speaker_1", "mic_speaker_1", "mic_speaker_1", "system_speaker_1"])
        for checkbox in document.find("input", name="remember"):
            self.assertNotIn("checked", checkbox.attributes)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertFalse(document.find("script"))
        self.assertEqual(self.calls, [])

    def test_tab_is_optional_and_has_no_action_without_both_callbacks(self):
        for key in ("speaker_options", "on_speaker"):
            old = getattr(self.server, key)
            setattr(self.server, key, None)
            try:
                body = self.detail()[2]
                document = parsed(body)
                self.assertFalse([link for link in document.find("a") if link.text == "Speakers"])
                self.assertNotIn('value="speaker_save"', body)
                self.assertEqual(self.post()[0], 409)
            finally:
                setattr(self.server, key, old)
        self.assertEqual(self.calls, [])

    def test_save_preserves_identity_and_redirects_to_same_speakers_tab(self):
        status, headers, _ = self.post(remember="yes")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], self.server.review_path + "?" + urlencode({
            "meeting": _selection("later"), "tab": "speakers"}))
        self.assertEqual(self.calls, [(("speaker_save", "later", "speaker-revision", "mic_speaker_1"),
                                      {"name": "Jamie", "email": "jamie@example.com", "remember": True})])
        self.assertEqual(self.request(headers["Location"])[0], 200)

    def test_separate_explicit_saves_can_assign_same_name_to_two_voices(self):
        for identity in ("mic_speaker_1", "system_speaker_1"):
            self.assertEqual(self.post(speaker_id=identity, email="")[0], 303)
        self.assertEqual([args[3] for args, _ in self.calls], ["mic_speaker_1", "system_speaker_1"])
        self.assertTrue(all(kw == {"name": "Jamie", "email": "", "remember": False} for _, kw in self.calls))

    def test_clear_and_forget_are_separate_explicit_actions(self):
        for action in ("speaker_clear", "speaker_forget"):
            self.assertEqual(self.post(action)[0], 303)
        self.assertEqual([args[0] for args, _ in self.calls], ["speaker_clear", "speaker_forget"])
        self.assertTrue(all(kw == {"name": "", "email": "", "remember": False} for _, kw in self.calls))
        self.assertEqual(self.post("speaker_forget", speaker_id="system_speaker_1")[0], 409)
        self.assertEqual(len(self.calls), 2)

    def test_unknown_speech_never_becomes_assignable(self):
        self.options["speakers"] = []
        body = self.detail()[2]
        self.assertIn("No separate voices", body)
        self.assertIn("2 speech segments", body)
        self.assertNotIn('value="speaker_save"', body)
        self.assertEqual(self.post(speaker_id="unknown")[0], 400)
        self.assertEqual(self.post()[0], 409)
        self.assertEqual(self.calls, [])

    def test_unsafe_or_malformed_form_fields_rejected_without_callback(self):
        changes = [{"job": "../outside"}, {"revision": ""}, {"revision": "r" * 257},
                   {"speaker_id": "system_speaker_0"}, {"name": " "}, {"name": "n" * 61},
                   {"name": "Jamie\nAdmin"}, {"email": "x" * 255}, {"email": "x\x7fy"},
                   {"remember": "no"}, {"remember": "yes", "email": ""},
                   {"remember": "yes", "email": " "}, {"company_id": "other"}]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(self.post(**change)[0], 400)
        raw = urlencode(self.fields()) + "&name=Someone"
        self.assertEqual(self.request(self.server.review_path + "decision", raw=raw)[0], 400)
        self.assertEqual(self.request(self.server.review_path + "decision", raw="x" * (MAX_FORM_BYTES + 1))[0], 413)
        self.assertEqual(self.calls, [])

    def test_wrong_host_origin_and_token_cannot_save(self):
        for headers in ({"Origin": "https://example.com"}, {"Host": "example.com"}):
            self.assertEqual(self.request(self.server.review_path + "decision", self.fields(), headers)[0], 403)
        self.assertEqual(self.request("/wrong/decision", self.fields())[0], 404)
        self.assertEqual(self.calls, [])

    def test_stale_revision_missing_speaker_and_deleted_job_do_not_dispatch(self):
        self.assertEqual(self.post(revision="old-revision")[0], 409)
        self.assertEqual(self.post(speaker_id="mic_speaker_22")[0], 409)
        self.assertEqual(self.post(job="deleted")[0], 409)
        self.assertEqual(self.calls, [])

    def test_cancellation_and_deletion_gate_old_forms_and_hide_new_forms(self):
        for state in ("cancelling", "cancelled"):
            self.server.cancellation_options = lambda job=None: [{"job": "later", "state": state,
                "title": "Later meeting", "revision": "cancellation-revision", "phase": "transcription"}]
            self.assertEqual(self.post()[0], 409)
            self.assertNotIn('value="speaker_save"', self.detail()[2])
        self.server.cancellation_options = None
        for value in ({"cancelling": True}, {"interrupted": True}, {"available": False}):
            self.server.deletion_options = lambda: [dict(job="later", title="Later meeting", revision="delete-revision", **value)]
            self.assertEqual(self.post()[0], 409)
            self.assertNotIn('value="speaker_save"', self.detail()[2])
        self.assertEqual(self.calls, [])

    def test_callback_failures_are_escaped_and_do_not_claim_success(self):
        def fail(*args, **kw):
            raise ValueError('<script>alert("private")</script>')
        self.server.on_speaker = fail
        status, _, body = self.post()
        self.assertEqual(status, 409)
        self.assertNotIn('<script>', body)
        self.assertIn('&lt;script&gt;', body)
        self.assertIn('tab=speakers', body)

    def test_callback_options_fail_closed_on_invalid_voice_ids(self):
        for bad in (None, {}, {"job": "other", "revision": "r", "speakers": []},
                    {"job": "later", "revision": "r", "speakers": [{"id": "unknown"}]}):
            self.server.speaker_options = lambda job: bad
            self.assertIn("Speaker details are unavailable", self.detail()[2])
            self.assertEqual(self.post()[0], 409)
        self.assertEqual(self.calls, [])

    def test_all_speaker_and_candidate_text_is_escaped(self):
        hostile = '<script>alert("x")</script>'
        self.options["speakers"][0].update(label=hostile, name=hostile, email=hostile,
                                            reason=hostile, samples=[hostile])
        self.options["candidates"] = [{"name": hostile, "email": hostile, "source": hostile}]
        body = self.detail()[2]
        document = parsed(body)
        self.assertNotIn('<script>', body)
        self.assertIn('&lt;script&gt;', body)
        self.assertFalse(document.find("script"))
        labels = {node.attributes.get("for") for node in document.find("label")}
        for field in document.find("input", name="name") + document.find("input", name="email"):
            self.assertIn(field.attributes["id"], labels)
        self.assertEqual(len(by_class(document, "speaker-card")), 2)

    def test_candidate_sources_are_distinguished_without_claiming_voice_identity(self):
        self.options["candidates"] = [
            {"name": "Morgan", "email": "morgan@example.com", "source": "calendar"},
            {"name": None, "email": "email-only@example.com", "source": "calendar"},
            {"name": "Casey", "email": None, "source": "visible"},
            {"name": "Jamie", "email": "jamie@example.com", "source": "confirmed_correction"},
        ]
        document = parsed(self.detail()[2])
        labels = [item.attributes.get("label", "") for item in document.find("option")]
        for source in ("Calendar attendee", "Seen in meeting", "Remembered correction"):
            self.assertTrue(any(source in label for label in labels))
        names = document.find("datalist", id="speaker-0-names")[0]
        emails = document.find("datalist", id="speaker-0-emails")[0]
        self.assertEqual([item.attributes["value"] for item in names.find("option")], ["Morgan", "Casey", "Jamie"])
        email_only = emails.find("option", value="email-only@example.com")[0]
        self.assertEqual(email_only.attributes["label"], "Calendar attendee")
        self.assertEqual(len(document.find("button", value="speaker_forget")), 1)
        self.assertEqual(len(document.find("button", value="speaker_clear")), 1)
        self.assertEqual(self.calls, [])

    def test_get_does_not_write_review_state(self):
        self.detail()  # Initialize any existing library database read handles.
        directory = Path(self.temporary.name)
        before = {str(path.relative_to(directory)): path.read_bytes()
                  for path in directory.rglob("*") if path.is_file()}
        self.assertEqual(self.detail()[0], 200)
        after = {str(path.relative_to(directory)): path.read_bytes()
                 for path in directory.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.calls, [])

    def test_notes_refresh_status_is_truthful_and_retry_is_only_offered_when_needed(self):
        for state, label in (("pending", "Updating local notes…"), ("running", "Updating local notes…"),
                             ("succeeded", "Local notes updated"), ("failed", "Notes need refreshing"),
                             ("stale", "Notes need refreshing")):
            with self.subTest(state=state):
                self.options["notes_status"] = state
                status, _, body = self.detail()
                document = parsed(body)
                self.assertEqual(status, 200)
                panel = by_class(document, "speaker-notes-status")[0]
                self.assertIn(label, panel.text)
                retry = panel.find("button", value="speaker_refresh_notes")
                self.assertEqual(len(retry), int(state in ("failed", "stale")))
                if retry:
                    self.assertEqual(panel.find("input", name="speaker_id")[0].attributes["value"], "mic_speaker_1")
        for state in (None, "unrecognized"):
            self.options["notes_status"] = state
            self.assertFalse(by_class(parsed(self.detail()[2]), "speaker-notes-status"))
        self.assertEqual(self.calls, [])

    def test_failed_notes_retry_dispatches_exact_action_with_revision_and_id(self):
        self.options.update(notes_status="failed", notes_error='<script>alert("failure")</script>')
        body = self.detail()[2]
        self.assertIn('&lt;script&gt;', body)
        self.assertNotIn('<script>', body)
        status, headers, _ = self.post("speaker_refresh_notes")
        self.assertEqual(status, 303)
        self.assertIn("meeting=" + _selection("later") + "&tab=speakers", headers["Location"])
        self.assertEqual(self.calls, [(("speaker_refresh_notes", "later", "speaker-revision", "mic_speaker_1"),
                                      {"name": "", "email": "", "remember": False})])

    def test_notes_retry_rechecks_status_revision_identity_and_cancellation(self):
        for state in (None, "pending", "running", "succeeded"):
            self.options["notes_status"] = state
            self.assertEqual(self.post("speaker_refresh_notes")[0], 409)
        self.options["notes_status"] = "stale"
        self.assertEqual(self.post("speaker_refresh_notes", revision="old")[0], 409)
        self.assertEqual(self.post("speaker_refresh_notes", speaker_id="mic_speaker_2")[0], 409)
        self.assertEqual(self.post("speaker_refresh_notes", remember="yes")[0], 400)
        self.server.cancellation_options = lambda job=None: [{"job": "later", "state": "cancelled"}]
        self.assertEqual(self.post("speaker_refresh_notes")[0], 409)
        self.server.cancellation_options = None
        self.server.deletion_options = lambda: [{"job": "later", "cancelling": True}]
        self.assertEqual(self.post("speaker_refresh_notes")[0], 409)
        self.assertEqual(self.calls, [])

    def test_create_server_rejects_non_callable_speaker_callbacks(self):
        for key in ("speaker_options", "on_speaker"):
            with self.assertRaises(TypeError):
                create_server(Path(self.temporary.name), **{key: "invalid"})


class CorrectedNotesPreviewTests(unittest.TestCase):
    def setUp(self):
        import test_speaker_corrections as fixtures
        fixtures.SpeakerCorrectionsTests.setUp(self)
        self.fixture = fixtures
        self.config = self.root / "workflow.json"
        from quill_workflow import codex_notes
        from quill_workflow.pipeline import atomic_json, render_notes
        from quill_workflow.speaker_notes import refresh
        atomic_json(self.config, {"notes": {"enabled": True, "command": [
            sys.executable, str(Path(codex_notes.__file__)), "--input", "{input}",
            "--output", "{output}", "--skill", "{skill}", "--context", "{context}",
            "--codex", "/synthetic/codex"]}})
        (self.result / "notes" / "notes.md").write_text(render_notes(self.notes, self.transcript))
        self.protected[self.result / "notes" / "notes.md"] = (self.result / "notes" / "notes.md").read_bytes()
        def decision(action, job, revision, speaker_id, **kwargs):
            self.store.decision(action, job, revision, speaker_id, **kwargs)
            refresh(self.state, job, self.config)
        self.server = create_server(self.state, speaker_options=self.store.options, on_speaker=decision)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()

    request = SpeakerReviewServerTests.request
    detail = SpeakerReviewServerTests.detail

    def generate(self, _input, output, **kwargs):
        from quill_workflow.pipeline import atomic_json
        result = copy.deepcopy(self.notes)
        result["_speaker_identity"] = json.loads(Path(kwargs["identity_path"]).read_text())
        result["summary"] = [{"text": "Jamie Taylor will send the agenda.", "evidence": [2]}]
        atomic_json(output, result)

    def test_http_correction_and_refresh_replace_visible_notes_without_rewriting_receipts(self):
        before = self.detail(self.job, "notes")[2]
        self.assertIn("Prepare the agenda.", before)
        self.assertNotIn("Jamie Taylor will send the agenda.", before)
        options = self.store.options(self.job)
        receipt = self.db.execute("SELECT receipt FROM tasks WHERE job=? AND stage='notes'", (self.job,)).fetchone()[0]
        fields = {"action": "speaker_save", "job": self.job, "revision": options["revision"],
                  "speaker_id": "system_speaker_1", "name": "Jamie Taylor", "email": ""}
        with patch("quill_workflow.speaker_notes.codex_notes.run", side_effect=self.generate) as model:
            status, headers, _ = self.request(self.server.review_path + "decision", fields)
        self.assertEqual(status, 303)
        self.assertIn("tab=speakers", headers["Location"])
        self.assertEqual(model.call_count, 1)
        after = self.detail(self.job, "notes")[2]
        self.assertIn("Jamie Taylor will send the agenda.", after)
        self.assertNotIn("Prepare the agenda.", after)
        self.assertIn("Jamie Taylor", self.detail(self.job, "transcript")[2])
        self.assertEqual(self.db.execute("SELECT receipt FROM tasks WHERE job=? AND stage='notes'", (self.job,)).fetchone()[0], receipt)
        self.fixture.SpeakerCorrectionsTests.assert_originals_unchanged(self)

    def test_unverified_registered_notes_never_fall_back_to_raw_markdown(self):
        from quill_workflow.pipeline import atomic_json
        raw = self.result / "notes" / "notes.md"
        raw.write_text("UNVERIFIED RAW PREVIEW")
        atomic_json(self.result / "notes" / "provenance.json", {"input_sha256": "wrong"})
        self.assertIsNone(_notes_preview(self.state, self.job))
        self.assertNotIn("UNVERIFIED RAW PREVIEW", self.detail(self.job, "notes")[2])

    def test_deleted_or_unreadable_selection_never_uses_raw_preview(self):
        from quill_workflow.local_deletion import SCHEMA
        (self.result / "notes" / "notes.md").write_text("DELETED RAW PREVIEW")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO local_deletions VALUES (?,?)", (self.job, '{"status":"deleted"}'))
        self.db.commit()
        self.assertIsNone(_notes_preview(self.state, self.job))
        with patch("quill_workflow.review_server.meeting_detail", return_value=None):
            self.assertIsNone(_notes_preview(self.state, self.job))

    def test_routing_only_fallback_rejects_structured_unverified_artifacts(self):
        from quill_workflow.pipeline import atomic_json
        state = self.root / "routing-only"
        folder = state / "results" / "pending-job" / "notes"
        folder.mkdir(parents=True)
        (folder / "notes.md").write_text("Legacy pending routing preview")
        self.assertEqual(_notes_preview(state, "pending-job"), "Legacy pending routing preview")
        for path in (folder / "notes.json", folder / "provenance.json", folder / "source-transcript.json",
                     folder.parent / "notes-receipt.json"):
            atomic_json(path, {})
            self.assertIsNone(_notes_preview(state, "pending-job"))
            path.unlink()


if __name__ == "__main__":
    unittest.main()
