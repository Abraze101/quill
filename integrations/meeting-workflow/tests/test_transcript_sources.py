"""Synthetic speaker evidence only; no model, recording or cloud access."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.transcript_sources import introduction_name, speaker_attributions, segment_label


def diarized_transcript():
    return {"speaker_labels": "diarized_v1", "segments": [
        {"start_ms": 0, "end_ms": 900, "source": "mic", "speaker": "mic_speaker_1", "text": "Hi, I am Anna."},
        {"start_ms": 1000, "end_ms": 1900, "source": "system", "speaker": "system_speaker_1", "text": "My name is Jamie."},
        {"start_ms": 2000, "end_ms": 2900, "source": "system", "speaker": "system_speaker_1", "text": "I will send the agenda."},
        {"start_ms": 3000, "end_ms": 3900, "source": "mic", "speaker": "mic_speaker_1", "text": "I will prepare the slides."},
        {"start_ms": 4000, "end_ms": 4900, "source": "system", "speaker": "unknown", "text": "The connection is unclear."}],
        "speakers": {
            "mic_speaker_1": {"source": "mic", "label": "Microphone speaker 1", "name": "Anna",
                              "name_source": "self_introduction", "evidence": [0]},
            "system_speaker_1": {"source": "system", "label": "Other speaker 1", "name": "Jamie",
                                 "name_source": "self_introduction", "evidence": [1]}}}


class TranscriptSourcesTests(unittest.TestCase):
    def test_complete_introductions_preserve_name_spelling(self):
        for text, name in (("My name is Jamie.", "Jamie"), ("Hi, I am Anna!", "Anna"),
                           ("Hello I'm O’Connor.", "O’Connor"), ("Hey, I’m Anne-Marie McDonald.", "Anne-Marie McDonald"),
                           ("This is José speaking.", "José"), ("my name is Mei Chen", "Mei Chen")):
            with self.subTest(text=text):
                self.assertEqual(introduction_name(text), name)

    def test_mentions_questions_quotes_explanations_and_role_labels_are_not_names(self):
        for text in ('He said "My name is Jamie."', '"My name is Jamie."', "My name is Jamie?",
                     "My name is Jamie. Just kidding.", "If I say my name is Jamie.", "My name is jamie.",
                     "My name is A.", "Hi, I am Ready.", "Hi, I am The Speaker.", "I'm Jamie.",
                     "My name is Jamie and Anna.", "My name is Jamie.\n", "This is Jamie speaking for Anna."):
            with self.subTest(text=text):
                self.assertIsNone(introduction_name(text))

    def test_names_apply_only_to_matching_separated_ids_and_unknown_stays_unidentified(self):
        transcript = diarized_transcript()
        people = speaker_attributions(transcript)
        self.assertEqual([segment_label(segment, people) for segment in transcript["segments"]],
                         ["Anna", "Jamie", "Jamie", "Anna", "System audio (unidentified)"])
        transcript["segments"][-1]["source"] = "mic"
        self.assertEqual(segment_label(transcript["segments"][-1], speaker_attributions(transcript)),
                         "Microphone (unidentified)")

    def test_clear_intro_without_name_metadata_does_not_silently_enroll_a_name(self):
        transcript = diarized_transcript()
        transcript["speakers"]["system_speaker_1"] = {"source": "system", "label": "Other speaker 1"}
        self.assertEqual(segment_label(transcript["segments"][2], speaker_attributions(transcript)), "Other speaker 1")

    def test_forged_name_provenance_source_identity_and_evidence_are_rejected(self):
        changes = ({"name": "Max"}, {"name": "Jamie Jones"}, {"name_source": "calendar"},
                   {"evidence": [0]}, {"evidence": [True]}, {"evidence": [1, 1]}, {"evidence": []},
                   {"evidence": [999]}, {"source": "mic"}, {"label": "Jamie"})
        for change in changes:
            with self.subTest(change=change):
                transcript = diarized_transcript()
                transcript["speakers"]["system_speaker_1"].update(change)
                with self.assertRaises(ValueError):
                    speaker_attributions(transcript)

    def test_conflicting_introductions_and_same_name_on_two_voices_are_rejected(self):
        for replacement in ("My name is Max.", "My name is Anna."):
            transcript = diarized_transcript()
            transcript["segments"][2]["text"] = replacement
            with self.assertRaisesRegex(ValueError, "conflicts"):
                speaker_attributions(transcript)

    def test_all_repeated_intro_indices_are_required_in_original_order(self):
        transcript = diarized_transcript()
        transcript["segments"][2]["text"] = "My name is Jamie."
        with self.assertRaisesRegex(ValueError, "exact introduction"):
            speaker_attributions(transcript)
        transcript["speakers"]["system_speaker_1"]["evidence"] = [1, 2]
        speaker_attributions(transcript)

    def test_canonical_unicode_introductions_match_but_duplicate_names_across_ids_do_not(self):
        transcript = diarized_transcript()
        transcript["segments"][1]["text"] = "My name is José."
        transcript["segments"][2]["text"] = "My name is Jose\u0301."
        transcript["speakers"]["system_speaker_1"].update(name="José", evidence=[1, 2])
        self.assertEqual(speaker_attributions(transcript)["system_speaker_1"]["name"], "José")
        transcript["segments"][0]["text"] = "Hi, I am Jose\u0301."
        transcript["speakers"]["mic_speaker_1"]["name"] = "Jose\u0301"
        with self.assertRaisesRegex(ValueError, "conflicts"):
            speaker_attributions(transcript)

    def test_split_quote_roleplay_and_correction_context_veto_names(self):
        for context in ("Just kidding.", "I quoted him.", "Imagine someone saying this.", "Let's role play.",
                        "That isn't my name.", "Here is a hypothetical example."):
            for position in ("before", "after"):
                with self.subTest(context=context, position=position):
                    transcript = diarized_transcript()
                    if position == "after":
                        transcript["segments"][2]["text"] = context
                    else:
                        transcript["segments"][1]["text"] = context
                        transcript["segments"][2]["text"] = "My name is Jamie."
                        transcript["speakers"]["system_speaker_1"]["evidence"] = [2]
                    with self.assertRaisesRegex(ValueError, "unsupported"):
                        speaker_attributions(transcript)

    def test_invalid_segment_source_and_identity_do_not_borrow_a_name(self):
        for update in ({"source": "mic"}, {"speaker": "system_speaker_2"}, {"speaker": {}}, {"text": None}):
            transcript = diarized_transcript()
            transcript["segments"][2].update(update)
            with self.assertRaises(ValueError):
                speaker_attributions(transcript)

    def test_legacy_source_metadata_does_not_become_name_attribution(self):
        transcript = diarized_transcript()
        transcript.pop("speaker_labels")
        self.assertIsNone(speaker_attributions(transcript))
        self.assertEqual(segment_label({"speaker": "me"}), "Microphone")
        self.assertEqual(segment_label({"speaker": "Jamie"}), "Audio source unspecified")


if __name__ == "__main__":
    unittest.main()
