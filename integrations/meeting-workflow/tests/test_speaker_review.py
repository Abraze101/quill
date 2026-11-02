"""Synthetic reviewed speaker mappings; no model, capture, or remote access."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.codex_notes import SECTIONS, validate_result
from quill_workflow.meeting_document import render_document
from quill_workflow.native_template import build_payload
from quill_workflow.pipeline import WorkflowError, render_notes
from quill_workflow.transcript_sources import (
    bind_speaker_review, effective_attributions, introduction_candidates,
    introduction_name, render_reviewed_transcript, segment_label, speaker_attributions,
)


def source():
    return {"speaker_labels": "diarized_v1", "segments": [
        {"start_ms": 0, "end_ms": 900, "source": "mic", "speaker": "mic_speaker_1", "text": "My name is"},
        {"start_ms": 1000, "end_ms": 1900, "source": "mic", "speaker": "mic_speaker_1", "text": "Anna, and I lead operations."},
        {"start_ms": 2000, "end_ms": 2900, "source": "system", "speaker": "system_speaker_1", "text": "Thanks. I will send the agenda."},
        {"start_ms": 3000, "end_ms": 3900, "source": "mic", "speaker": "mic_speaker_1", "text": "I will prepare the slides."},
        {"start_ms": 4000, "end_ms": 4900, "source": "system", "speaker": "unknown", "text": "The connection is unclear."}],
        "speakers": {"mic_speaker_1": {"source": "mic", "label": "Microphone speaker 1"},
                     "system_speaker_1": {"source": "system", "label": "Other speaker 1"}}}


def context():
    return {"provider": "google-meet", "key": "abc-defg-hij", "coverage": "visible_only",
            "participants": [{"name": "Anna", "self": True}, {"name": "Jamie", "self": False}]}


def draft():
    value = {section: [] for section in SECTIONS}
    value.update(schema_version=1, summary=[{"text": "Prepare the meeting materials.", "evidence": [2, 3]}],
                 actions=[{"text": "Prepare the slides.", "owner": "Anna", "due": None, "evidence": [3]}],
                 follow_up_draft=None,
                 meeting_title={"text": "Agenda planning", "evidence": [2],
                                "participants": [{"name": "Anna", "evidence": [0, 1]}]},
                 speaker_review={"schema_version": 1, "assignments": [
                     {"speaker_id": "mic_speaker_1", "name": "Anna", "status": "confirmed",
                      "evidence": [0, 1], "context_participants": []},
                     {"speaker_id": "system_speaker_1", "name": "Jamie", "status": "suggested",
                      "evidence": [2], "context_participants": [1]}]})
    return value


class SpeakerReviewTests(unittest.TestCase):
    def test_shared_native_v2_corpus(self):
        path = Path(__file__).resolve().parents[3] / "Tests/Fixtures/speaker-name-policy-v2.json"
        corpus = json.loads(path.read_text())
        for case in corpus["introductions"]:
            with self.subTest(text=case["text"]):
                self.assertEqual(introduction_name(case["text"], corpus["policy"]), case["name"])
        for case in corpus["sessions"]:
            with self.subTest(session=case["label"]):
                candidates = introduction_candidates(case["segments"], corpus["policy"])
                counts = {}
                for names in candidates.values():
                    for name in names:
                        counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
                actual = {identity: {"name": next(iter(names)), "evidence": next(iter(names.values()))}
                          for identity, names in candidates.items()
                          if len(names) == 1 and counts[next(iter(names)).casefold()] == 1}
                self.assertEqual(actual, case["expected"])

    def test_native_v2_names_validate_split_evidence_without_changing_legacy(self):
        transcript = source()
        transcript["speakers"]["mic_speaker_1"].update(name="Anna", name_source="self_introduction", evidence=[0, 1])
        with self.assertRaises(ValueError):
            speaker_attributions(transcript)
        transcript["speaker_name_policy"] = "explicit_intro_v2"
        self.assertEqual(speaker_attributions(transcript)["mic_speaker_1"]["name"], "Anna")

    def test_review_is_bound_and_preserves_source(self):
        transcript = source()
        unchanged = copy.deepcopy(transcript)
        unbound = draft()
        notes = bind_speaker_review(unbound, transcript, context())
        validate_result(notes, transcript)
        people = effective_attributions(transcript, notes)
        self.assertEqual(people["mic_speaker_1"]["name"], "Anna")
        self.assertNotIn("name", people["system_speaker_1"])
        self.assertEqual(segment_label(transcript["segments"][2], people), "Jamie (suggested)")
        self.assertEqual(transcript, unchanged)
        self.assertNotIn("context", unbound["speaker_review"])
        transcript["segments"][2]["text"] += " Changed."
        with self.assertRaisesRegex(WorkflowError, "source transcript"):
            validate_result(notes, transcript)

    def test_suggestions_do_not_become_definite_owners_or_attendees(self):
        transcript = source()
        notes = bind_speaker_review(draft(), transcript, context())
        notes["actions"][0].update(owner="Jamie", evidence=[2])
        with self.assertRaisesRegex(WorkflowError, "absent"):
            validate_result(notes, transcript)
        with self.assertRaisesRegex(WorkflowError, "absent"):
            render_notes(notes, transcript)
        notes["actions"] = []
        notes["meeting_title"]["participants"] = [{"name": "Jamie", "evidence": [2]}]
        with self.assertRaisesRegex(WorkflowError, "absent"):
            validate_result(notes, transcript)
        # Independent exact conversational evidence remains valid, even when
        # the speaker mapping itself is only a suggestion.
        transcript["segments"][2]["text"] = "Jamie will send the agenda."
        unbound = draft()
        unbound["actions"][0].update(owner="Jamie", evidence=[2])
        validate_result(bind_speaker_review(unbound, transcript, context()), transcript)

    def test_context_and_transcript_evidence_must_supply_exact_name(self):
        for mutation in ({"name": "Jamie Smith"}, {"name": "jamie@example.test"},
                         {"context_participants": []}, {"context_participants": [True]},
                         {"speaker_id": "unknown"}, {"evidence": [4]}, {"evidence": [{}]},
                         {"status": "confirmed"}, {"evidence": [2, 2]}):
            with self.subTest(mutation=mutation):
                unbound = draft()
                unbound["speaker_review"]["assignments"][1].update(mutation)
                notes = bind_speaker_review(unbound, source(), context())
                with self.assertRaises((WorkflowError, ValueError)):
                    validate_result(notes, source())

    def test_duplicate_name_cross_voice_and_conflicting_source_name_rejected(self):
        unbound = draft()
        unbound["speaker_review"]["assignments"][1].update(name="Anna", context_participants=[0])
        with self.assertRaisesRegex(ValueError, "same name"):
            effective_attributions(source(), bind_speaker_review(unbound, source(), context()))
        transcript = source()
        transcript["segments"][2]["text"] = "My name is Jamie."
        transcript["speakers"]["system_speaker_1"].update(name="Jamie", name_source="self_introduction", evidence=[2])
        with self.assertRaisesRegex(ValueError, "established"):
            effective_attributions(transcript, bind_speaker_review(draft(), transcript, context()))

    def test_confirmed_name_conflicts_with_casefolded_intro_on_unnamed_voice(self):
        for name, other in (("Alex", "ALEX"), ("José", "JOSE\u0301")):
            with self.subTest(name=name, other=other):
                transcript = source()
                transcript["segments"][0]["text"] = "My name is " + name + "."
                transcript["segments"][1]["text"] = "Let's begin."
                transcript["segments"][2]["text"] = "My name is " + other + "."
                unbound = draft()
                unbound["actions"] = []
                unbound["meeting_title"]["participants"] = []
                unbound["speaker_review"]["assignments"] = [{
                    "speaker_id": "mic_speaker_1", "name": name, "status": "confirmed",
                    "evidence": [0], "context_participants": []}]
                notes = bind_speaker_review(unbound, transcript)
                with self.assertRaisesRegex(WorkflowError, "more than one voice"):
                    validate_result(notes, transcript)
                with self.assertRaisesRegex(WorkflowError, "more than one voice"):
                    render_notes(notes, transcript)

    def assert_prose_rejected_by_consumers(self, unbound, transcript):
        notes = bind_speaker_review(unbound, transcript, context())
        meta = {"started": "2026-10-20T00:00:00Z", "ended": "2026-10-20T00:01:00Z"}
        route = {"company_name": "Example"}
        for consumer in (lambda: validate_result(notes, transcript),
                         lambda: render_notes(notes, transcript),
                         lambda: build_payload(notes, transcript, meta, route),
                         lambda: render_document(notes, transcript, meta, route),
                         lambda: render_reviewed_transcript(transcript, notes)):
            with self.assertRaisesRegex((WorkflowError, ValueError), "adjacent \\(suggested\\)"):
                consumer()

    def test_suggested_name_cannot_become_definite_prose_in_any_notes_section(self):
        for section in SECTIONS + ("actions",):
            with self.subTest(section=section):
                unbound = draft()
                item = {"text": "Jamie agreed to send the agenda.", "evidence": [2]}
                if section == "actions":
                    item.update(owner=None, due=None)
                unbound[section] = [item]
                self.assert_prose_rejected_by_consumers(unbound, source())

    def test_suggested_name_cannot_become_definite_draft_or_title(self):
        for field in ("subject", "body", "title"):
            with self.subTest(field=field):
                unbound = draft()
                if field == "title":
                    unbound["meeting_title"]["text"] = "Jamie agreed to send the agenda"
                else:
                    unbound["follow_up_draft"] = {"subject": "Agenda follow up", "body": "Thanks for the discussion.",
                                                  "evidence": [2]}
                    unbound["follow_up_draft"][field] = "Jamie agreed to send the agenda."
                self.assert_prose_rejected_by_consumers(unbound, source())

    def test_suggested_prose_needs_marked_occurrences_or_independent_evidence(self):
        for text in ("Jamie (suggested) agreed to send the agenda.",
                     "JAMIE (suggested) agreed; Jamie (suggested) will follow up.",
                     "Anna will prepare the slides."):
            with self.subTest(text=text):
                unbound = draft()
                unbound["summary"] = [{"text": text, "evidence": [2, 3]}]
                notes = bind_speaker_review(unbound, source(), context())
                validate_result(notes, source())
                render_notes(notes, source())
        for text in ("Jamie (suggested) agreed, and Jamie will follow up.",
                     "Jamie, (suggested) agreed to send the agenda.",
                     "Jamie's agenda is ready."):
            with self.subTest(text=text):
                unbound = draft()
                unbound["summary"] = [{"text": text, "evidence": [2]}]
                self.assert_prose_rejected_by_consumers(unbound, source())
        transcript = source()
        transcript["segments"][2]["text"] = "Jamie agreed to send the agenda."
        unbound = draft()
        unbound["summary"] = [{"text": "Jamie agreed to send the agenda.", "evidence": [2]}]
        unbound["follow_up_draft"] = {"subject": "Jamie's agenda", "body": "Jamie will send the agenda.", "evidence": [2]}
        notes = bind_speaker_review(unbound, transcript, context())
        validate_result(notes, transcript)
        render_notes(notes, transcript)

    def test_model_cannot_supply_provenance_or_unknown_review_versions(self):
        for mutation in ({"context": context()}, {"transcript_sha256": "0" * 64}):
            unbound = draft()
            unbound["speaker_review"].update(mutation)
            with self.assertRaises(ValueError):
                bind_speaker_review(unbound, source(), context())
        notes = bind_speaker_review(draft(), source(), context())
        notes["speaker_review"]["schema_version"] = True
        with self.assertRaises(WorkflowError):
            render_notes(notes, source())

    def test_unknown_names_and_legacy_source_tracks_can_have_empty_review(self):
        transcript = {"segments": [{"speaker": "me", "start_ms": 0, "text": "Plan the meeting."}]}
        unbound = {"schema_version": 1, "summary": [{"text": "Plan the meeting.", "evidence": [0]}],
                   "speaker_review": {"schema_version": 1, "assignments": []}}
        notes = bind_speaker_review(unbound, transcript)
        self.assertIsNone(effective_attributions(transcript, notes))
        self.assertIn("Microphone: Plan the meeting.", render_reviewed_transcript(transcript, notes))
        render_notes(notes, transcript)

    def test_all_renderers_use_same_visible_suggestion_and_exact_source(self):
        transcript = source()
        notes = bind_speaker_review(draft(), transcript, context())
        meta = {"started": "2026-10-20T00:00:00Z", "ended": "2026-10-20T00:01:00Z"}
        route = {"company_name": "Example"}
        text = render_reviewed_transcript(transcript, notes)
        native = build_payload(notes, transcript, meta, route)["replacements"]["{{QUILL_TRANSCRIPT}}"]
        _, paragraphs, _ = render_document(notes, transcript, meta, route)
        for rendered in (text, native, "\n".join(paragraphs)):
            self.assertIn("Jamie (suggested)", rendered)
            self.assertIn("Anna", rendered)
            self.assertIn("System audio (unidentified)", rendered)
            for segment in transcript["segments"]:
                self.assertIn(segment["text"], rendered)


if __name__ == "__main__":
    unittest.main()
