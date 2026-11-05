"""Synthetic, local-only speaker correction and identity-learning lifecycle."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.codex_notes import SECTIONS
from quill_workflow.local_deletion import SCHEMA as DELETION_SCHEMA
from quill_workflow.pipeline import SCHEMA, atomic_json
from quill_workflow.speaker_corrections import SpeakerCorrections, load_bundle
from quill_workflow.speaker_identity import attach, empty_bundle, validate_bundle
from quill_workflow.speaker_review import bind_report
from quill_workflow.transcript_sources import effective_attributions, render_reviewed_transcript
from test_focused_speaker_review import source, context, draft


class SpeakerCorrectionsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        self.job = "synthetic-speaker-meeting"
        self.recording = self.state / "recordings" / self.job
        self.result = self.state / "results" / self.job
        self.db = sqlite3.connect(self.state / "jobs.sqlite3")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO jobs VALUES (?,?,?)", (self.job, str(self.root / "original"), 1))
        self.transcript = source()
        atomic_json(self.recording / "transcript.json", self.transcript)
        atomic_json(self.recording / "meta.json", {"started": "2026-11-01T10:00:00Z", "files": {}})
        (self.recording / "transcript.md").write_text("Original unreviewed transcript.\n")
        self.original = (self.recording / "transcript.json").read_bytes()
        self.notes = {key: [] for key in SECTIONS}
        self.notes.update(schema_version=1, actions=[], follow_up_draft=None,
                          summary=[{"text": "Prepare the agenda.", "evidence": [1, 2]}],
                          meeting_title={"text": "Agenda planning", "evidence": [1, 2], "participants": []})
        settings = {"enabled": True, "command": ["synthetic"]}
        receipt = {"job": self.job, "stage": "notes", "idempotency_key": self.job + ":notes",
                   "settings_sha256": hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest(),
                   "input_sha256": hashlib.sha256(self.original).hexdigest()}
        self.db.execute("INSERT INTO tasks (job,stage,status,settings,receipt) VALUES (?,?,?,?,?)",
                        (self.job, "notes", "succeeded", json.dumps(settings), json.dumps(receipt)))
        self.db.commit()
        atomic_json(self.result / "notes-receipt.json", receipt)
        atomic_json(self.result / "notes" / "provenance.json", receipt)
        atomic_json(self.result / "notes" / "notes.json", self.notes)
        (self.result / "notes" / "source-transcript.json").write_bytes(self.original)
        atomic_json(self.result / "publish-receipt.json", {"synthetic": "preserve filed document receipt"})
        atomic_json(self.result / "drive-publish.json", {"synthetic": "preserve upload journal"})
        self.protected = {path: path.read_bytes() for path in [
            self.recording / "transcript.json", self.recording / "transcript.md",
            self.result / "notes" / "notes.json", self.result / "notes" / "provenance.json",
            self.result / "notes" / "source-transcript.json", self.result / "publish-receipt.json",
            self.result / "drive-publish.json"]}
        self.store = SpeakerCorrections(self.state)

    def options(self):
        value = self.store.options(self.job)
        self.assertIsNotNone(value)
        return value

    def decide(self, action="speaker_save", speaker="system_speaker_1", name="Jamie Taylor",
               email="", remember=False, revision=None):
        return self.store.decision(action, self.job, revision or self.options()["revision"],
                                   speaker, name=name, email=email, remember=remember)

    def assert_originals_unchanged(self):
        for path, data in self.protected.items():
            with self.subTest(path=path.name):
                self.assertEqual(path.read_bytes(), data)

    def saved_bundle(self):
        from quill_workflow.review_library import _Sources
        sources = _Sources(self.state)
        try:
            return load_bundle(sources, self.job, self.transcript, self.notes)
        finally:
            sources.close()

    def test_save_renders_correction_without_changing_recording_notes_or_drive(self):
        before = self.options()
        self.assertEqual(before["unassigned_segments"], 1)
        self.assertEqual(len(before["speakers"]), 2)
        self.assertTrue(all(person["samples"] for person in before["speakers"]))
        after = self.decide()
        person = next(person for person in after["speakers"] if person["id"] == "system_speaker_1")
        self.assertEqual((person["name"], person["status"]), ("Jamie Taylor", "user_confirmed"))
        self.assertNotEqual(before["revision"], after["revision"])
        text = (self.result / "speakers" / "transcript-reviewed.md").read_text()
        self.assertIn("Jamie Taylor", text)
        self.assertNotIn("Jamie Taylor (suggested)", text)
        self.assertIn("System audio (unidentified)", text)
        self.assertTrue((self.result / "speakers" / "notes-reviewed.md").is_file())
        self.assertEqual(self.store.learned(), [])
        self.assert_originals_unchanged()

    def test_same_person_can_be_explicitly_confirmed_for_two_separated_ids(self):
        self.decide(speaker="mic_speaker_1", name="Alex")
        self.decide(speaker="system_speaker_1", name="Alex")
        people = effective_attributions(self.transcript, attach(self.notes, self.transcript, self.saved_bundle()))
        self.assertEqual([p["name"] for p in people.values()], ["Alex", "Alex"])
        self.assertEqual(list(people), ["mic_speaker_1", "system_speaker_1"])
        self.assert_originals_unchanged()

    def test_meeting_detail_reads_the_correction_without_reclassifying_cloud_storage(self):
        from quill_workflow.review_library import meeting_detail
        before = meeting_detail(self.state, self.job)
        self.decide(name="Jamie Taylor")
        after = meeting_detail(self.state, self.job)
        self.assertIn("Jamie Taylor", after["transcript_text"])
        self.assertIn("Jamie Taylor", after["participants"])
        self.assertEqual(after["storage"], before["storage"])
        self.assertEqual(after["storage_status"], before["storage_status"])
        self.assert_originals_unchanged()

    def test_unknown_voice_and_invalid_name_are_rejected_without_writes(self):
        for kwargs in ({"speaker": "unknown"}, {"speaker": "system_speaker_99"},
                       {"name": "Speaker 2"}, {"name": "unknown"}, {"email": "not-an-email"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.decide(**kwargs)
        self.assertFalse((self.result / "speakers" / "corrections.json").exists())
        self.assertEqual(self.store.learned(), [])
        self.assert_originals_unchanged()

    def test_stale_revision_cannot_replace_a_newer_name(self):
        old = self.options()["revision"]
        self.decide(name="Jamie")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.decide(name="Other Name", revision=old)
        self.assertEqual(self.saved_bundle()["corrections"]["assignments"]["system_speaker_1"]["name"], "Jamie")

    def test_changed_transcript_rejects_previous_review_and_corrections(self):
        old = self.options()["revision"]
        self.decide(name="Jamie")
        changed = copy.deepcopy(self.transcript)
        changed["segments"][2]["text"] = "Changed source speech."
        atomic_json(self.recording / "transcript.json", changed)
        self.assertIsNone(self.store.options(self.job))
        with self.assertRaisesRegex(ValueError, "transcript"):
            self.decide(name="Other Name", revision=old)
        self.assertEqual(json.loads((self.result / "speakers" / "corrections.json").read_text())[
            "assignments"]["system_speaker_1"]["name"], "Jamie")

    def test_remember_requires_email_and_only_explicit_choice_learns(self):
        with self.assertRaisesRegex(ValueError, "email"):
            self.decide(remember=True)
        self.assertFalse((self.result / "speakers" / "corrections.json").exists())
        self.decide(email="jamie@example.com", remember=False)
        self.assertEqual(self.store.learned(), [])
        self.decide(email="jamie@example.com", remember=True)
        self.assertEqual(self.store.learned(), [{"name": "Jamie Taylor", "email": "jamie@example.com",
            "source": "confirmed_correction", "aliases": []}])
        self.assertNotIn("segments", (self.state / "speaker-identities.json").read_text())
        self.assertNotIn("voice", (self.state / "speaker-identities.json").read_text())
        self.assert_originals_unchanged()

    def test_renaming_a_remembered_email_retains_prior_spelling_as_alias(self):
        self.decide(name="Jamie", email="jamie@example.com", remember=True)
        self.decide(name="Jamie Taylor", email="jamie@example.com", remember=True)
        self.assertEqual(self.store.learned(), [{"name": "Jamie Taylor", "email": "jamie@example.com",
            "source": "confirmed_correction", "aliases": ["Jamie"]}])

    def test_forget_stops_future_learning_but_preserves_meeting_correction(self):
        self.decide(email="jamie@example.com", remember=True)
        self.decide(action="speaker_forget")
        self.assertEqual(self.store.learned(), [])
        person = self.saved_bundle()["corrections"]["assignments"]["system_speaker_1"]
        self.assertEqual(person["name"], "Jamie Taylor")
        self.assertEqual(person["status"], "user_confirmed")

    def test_clear_removes_local_correction_without_forgetting_saved_identity(self):
        self.decide(email="jamie@example.com", remember=True)
        self.decide(action="speaker_clear")
        self.assertNotIn("system_speaker_1", self.saved_bundle()["corrections"]["assignments"])
        self.assertEqual(len(self.store.learned()), 1)
        self.assertNotIn("Jamie Taylor", (self.result / "speakers" / "transcript-reviewed.md").read_text())
        self.assertGreaterEqual(len(list((self.result / "speakers" / "history").glob("*.json"))), 2)
        self.assert_originals_unchanged()

    def test_save_review_keeps_user_corrections_and_never_learns_model_names(self):
        self.decide(name="Confirmed Person")
        bundle = empty_bundle(self.transcript, context())
        bundle["report"] = bind_report(draft(), self.transcript, context())
        self.store.save_review(self.job, bundle, self.options()["revision"])
        current = self.saved_bundle()
        self.assertEqual(current["corrections"]["assignments"]["system_speaker_1"]["name"], "Confirmed Person")
        self.assertEqual(current["report"]["speakers"][1]["name"], "Jamie Taylor")
        self.assertIn("Confirmed Person", (self.result / "speakers" / "transcript-reviewed.md").read_text())
        self.assertEqual(self.store.learned(), [])
        self.assert_originals_unchanged()

    def test_save_review_rejects_stale_revision_or_other_source(self):
        before = self.options()["revision"]
        self.decide()
        bundle = empty_bundle(self.transcript, context())
        bundle["report"] = bind_report(draft(), self.transcript, context())
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.save_review(self.job, bundle, before)
        bundle["report"]["transcript_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source transcript"):
            self.store.save_review(self.job, bundle, self.options()["revision"])
        self.assertFalse((self.result / "speakers" / "review.json").exists())

    def test_bound_correction_is_visible_with_no_completed_notes(self):
        self.db.execute("DELETE FROM tasks WHERE stage='notes'")
        self.db.commit()
        self.decide(name="Jamie")
        self.assertIn("Jamie", (self.result / "speakers" / "transcript-reviewed.md").read_text())
        self.assertEqual((self.result / "speakers" / "notes-reviewed.md").read_text(), "")

    def test_deletion_and_processing_cancellation_close_correction_actions(self):
        revision = self.options()["revision"]
        self.db.executescript(DELETION_SCHEMA)
        self.db.execute("INSERT INTO local_deletions VALUES (?,?)", (self.job, json.dumps({"status": "cancelling"})))
        self.db.commit()
        self.assertIsNone(self.store.options(self.job))
        with self.assertRaisesRegex(ValueError, "stopped"):
            self.decide(revision=revision)
        self.db.execute("DELETE FROM local_deletions")
        self.db.execute("INSERT INTO processing_cancellations (job,device,inode,started) VALUES (?,?,?,?)",
                        (self.job, 1, 1, "2026-11-01T10:00:00Z"))
        self.db.commit()
        self.assertIsNone(self.store.options(self.job))
        with self.assertRaisesRegex(ValueError, "stopped"):
            self.decide(revision=revision)
        self.assertFalse((self.result / "speakers" / "corrections.json").exists())

    def test_symlinked_speaker_directory_cannot_write_outside_state(self):
        revision = self.options()["revision"]
        outside = self.root / "outside"
        outside.mkdir()
        (self.result / "speakers").symlink_to(outside, target_is_directory=True)
        self.assertIsNone(self.store.options(self.job))
        with self.assertRaises((OSError, ValueError)):
            self.decide(revision=revision)
        self.assertEqual(list(outside.iterdir()), [])
        self.assert_originals_unchanged()

    def test_symlinked_history_and_saved_identity_files_are_rejected(self):
        revision = self.options()["revision"]
        directory = self.result / "speakers"
        directory.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (directory / "history").symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            self.decide(revision=revision)
        self.assertEqual(list(outside.iterdir()), [])
        (directory / "history").unlink()
        identities = self.root / "external-identities.json"
        atomic_json(identities, {"schema_version": 1, "candidates": []})
        (self.state / "speaker-identities.json").symlink_to(identities)
        with self.assertRaises(ValueError):
            self.store.learned()
        self.assertIsNone(self.store.options(self.job))

    def test_report_and_correction_binding_reject_foreign_source(self):
        bundle = empty_bundle(self.transcript)
        bundle["corrections"]["transcript_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "transcript"):
            validate_bundle(bundle, self.transcript)
        bundle = empty_bundle(self.transcript)
        bundle["corrections"]["assignments"]["unknown"] = {
            "name": "Jamie", "email": None, "status": "user_confirmed"}
        with self.assertRaisesRegex(ValueError, "existing separated voice"):
            render_reviewed_transcript(self.transcript, attach(self.notes, self.transcript, bundle))


if __name__ == "__main__":
    unittest.main()
