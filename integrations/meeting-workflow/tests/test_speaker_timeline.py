"""Platform timing tests use fictional voices and names, never real meetings."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.speaker_identity import apply_bundle, empty_bundle
from quill_workflow.speaker_review import bind_report, platform_evidence, prompt_context, validate_context, validate_report
from quill_workflow.speaker_timeline import match_timeline, validate_timeline


def fixture():
    transcript = {"speaker_labels": "diarized_v1", "speaker_name_policy": "explicit_intro_v3",
                  "speakers": {"mic_speaker_1": {"source": "mic", "label": "Microphone speaker 1"},
                               "system_speaker_1": {"source": "system", "label": "Other speaker 1"}},
                  "segments": []}
    timeline = {"schema_version": 1, "provider": "google-meet", "key": "abc-defg-hij",
                "participants": [{"id": "doc-A:p1", "name": "Casey Rivera", "self": True},
                                 {"id": "doc-A:p2", "name": "Jamie Taylor", "self": False}],
                "intervals": [], "truncated": False}
    for start in (0, 4000, 8000):
        transcript["segments"].extend([
            {"source": "system", "speaker": "system_speaker_1", "start_ms": start,
             "end_ms": start + 2000, "text": "We can complete the next step this week."},
            {"source": "mic", "speaker": "mic_speaker_1", "start_ms": start + 2500,
             "end_ms": start + 3500, "text": "That sounds useful. What comes next?"}])
        timeline["intervals"].extend([
            {"start_ms": point, "end_ms": point + 500, "active": ["doc-A:p2"]}
            for point in range(start, start + 2000, 500)])
        timeline["intervals"].extend([
            {"start_ms": point, "end_ms": point + 500, "active": ["doc-A:p1"]}
            for point in range(start + 2500, start + 3500, 500)])
    return transcript, timeline


def context(timeline):
    return {"schema_version": 2, "timeline": timeline, "candidates": [
        {"name": "Jamie Taylor", "email": "jamie@example.test", "source": "calendar", "aliases": []}]}


def draft():
    return {"schema_version": 1, "speakers": [
        {"speaker_id": "system_speaker_1", "status": "matched", "name": "Jamie Taylor",
         "email": "jamie@example.test", "evidence": [0, 2, 4], "candidate_indices": [0],
         "reason": "Matched to repeated meeting activity."},
        {"speaker_id": "mic_speaker_1", "status": "suggested", "name": "Casey Rivera",
         "email": None, "evidence": [1, 3, 5], "candidate_indices": [],
         "reason": "Local participant is known; microphone may be shared."}]}


class SpeakerTimelineTests(unittest.TestCase):
    def test_long_meeting_checks_all_speech_and_bounds_evidence_across_turns(self):
        transcript, timeline = fixture()
        transcript["segments"], timeline["intervals"] = [], []
        for turn in range(20):
            start = turn * 123000
            transcript["segments"].extend({"source": "system", "speaker": "system_speaker_1",
                "start_ms": start + offset, "end_ms": start + offset + 2000,
                "text": "We discussed another useful detail and the next step."}
                for offset in range(0, 120000, 2000))
            timeline["intervals"].append({"start_ms": start, "end_ms": start + 120000, "active": ["doc-A:p2"]})
        result = match_timeline(transcript, timeline)
        evidence = result["matches"]["system_speaker_1"]["evidence"]
        self.assertEqual(len(evidence), 100)
        self.assertEqual(evidence, sorted(set(evidence)))
        self.assertEqual({index // 60 for index in evidence}, set(range(20)))
        self.assertEqual((evidence[0], evidence[-1]), (0, 1199))
        self.assertEqual(match_timeline(transcript, timeline), result)
        # A conflict outside the bounded citation sample must still block a match.
        uncited = next(index for index in range(1200) if index not in evidence)
        segment = transcript["segments"][uncited]
        at = uncited // 60
        interval = timeline["intervals"].pop(at)
        timeline["participants"].append({"id": "doc-A:p3", "name": "Morgan Lee", "self": False})
        replacement = [{"start_ms": interval["start_ms"], "end_ms": segment["start_ms"], "active": ["doc-A:p2"]},
                       {"start_ms": segment["start_ms"], "end_ms": segment["end_ms"], "active": ["doc-A:p3"]},
                       {"start_ms": segment["end_ms"], "end_ms": interval["end_ms"], "active": ["doc-A:p2"]}]
        timeline["intervals"][at:at] = [item for item in replacement if item["start_ms"] < item["end_ms"]]
        self.assertEqual(match_timeline(transcript, timeline)["matches"], {})

    def test_native_coalesced_intervals_preserve_long_continuous_turns(self):
        transcript, timeline = fixture()
        transcript["segments"] = [{"source": "system", "speaker": "system_speaker_1",
            "start_ms": start, "end_ms": start + 10000, "text": "A continuous turn with useful details."}
            for start in (0, 12000, 24000)]
        timeline["intervals"] = [{"start_ms": start, "end_ms": start + 10000, "active": ["doc-A:p2"]}
                                 for start in (0, 12000, 24000)]
        self.assertEqual(match_timeline(transcript, timeline)["matches"]["system_speaker_1"]["name"], "Jamie Taylor")

    def test_local_participant_without_timing_does_not_assign_a_voice(self):
        transcript, timeline = fixture()
        timeline["intervals"] = []
        result = match_timeline(transcript, timeline)
        self.assertEqual(result["matches"], {})
        self.assertEqual(result["suggestions"], {})
        self.assertEqual(result["local_participants"], [timeline["participants"][0]])

    def test_repeated_remote_turns_match_while_self_stays_a_suggestion(self):
        transcript, timeline = fixture()
        original = copy.deepcopy((transcript, timeline))
        result = match_timeline(transcript, timeline)
        self.assertEqual(set(result["matches"]), {"system_speaker_1"})
        self.assertEqual(result["matches"]["system_speaker_1"]["evidence"], [0, 2, 4])
        self.assertEqual(result["matches"]["system_speaker_1"]["name"], "Jamie Taylor")
        self.assertEqual(result["suggestions"]["mic_speaker_1"]["name"], "Casey Rivera")
        self.assertEqual(result["local_participants"], [timeline["participants"][0]])
        self.assertEqual((transcript, timeline), original)

    def test_fragmenting_one_monologue_does_not_create_three_turns(self):
        transcript, timeline = fixture()
        transcript["segments"] = [{"source": "system", "speaker": "system_speaker_1",
            "start_ms": start, "end_ms": start + 1000, "text": "A single continuous turn."}
            for start in range(0, 6000, 1000)]
        timeline["intervals"] = [{"start_ms": start, "end_ms": start + 500, "active": ["doc-A:p2"]}
                                 for start in range(0, 6000, 500)]
        self.assertEqual(match_timeline(transcript, timeline)["matches"], {})

    def test_insufficient_duration_or_incomplete_coverage_has_no_match(self):
        for duration in (500, 1000, 1500):
            transcript, timeline = fixture()
            timeline["intervals"] = [item for item in timeline["intervals"]
                                     if item["active"] == ["doc-A:p1"] or item["start_ms"] % 4000 < duration]
            self.assertEqual(match_timeline(transcript, timeline)["matches"], {})

    def test_gaps_are_not_filled_with_last_active_participant(self):
        transcript, timeline = fixture()
        timeline["intervals"] = [item for item in timeline["intervals"] if item["start_ms"] >= 8000]
        self.assertEqual(match_timeline(transcript, timeline)["matches"], {})

    def test_even_short_platform_conflict_blocks_automatic_name(self):
        for active in (["doc-A:p1", "doc-A:p2"], ["doc-A:p1"]):
            transcript, timeline = fixture()
            timeline["intervals"][0]["active"] = active
            self.assertNotIn("system_speaker_1", match_timeline(transcript, timeline)["matches"])

    def test_another_remote_participant_blocks_winner_even_with_large_majority(self):
        transcript, timeline = fixture()
        timeline["participants"].append({"id": "doc-A:p3", "name": "Morgan Lee", "self": False})
        timeline["intervals"][0]["end_ms"] = 20
        timeline["intervals"][0]["active"] = ["doc-A:p3"]
        timeline["intervals"].insert(1, {"start_ms": 20, "end_ms": 500, "active": ["doc-A:p2"]})
        self.assertEqual(match_timeline(transcript, timeline)["matches"], {})

    def test_overlapping_unknown_or_other_voice_cannot_be_named_automatically(self):
        for speaker in ("unknown", "mic_speaker_1"):
            transcript, timeline = fixture()
            transcript["segments"].append({"source": "mic", "speaker": speaker,
                "start_ms": 100, "end_ms": 120, "text": "Yes."})
            self.assertEqual(match_timeline(transcript, timeline)["matches"], {})

    def test_two_separated_voices_on_one_device_stay_suggestions(self):
        transcript, timeline = fixture()
        transcript["speakers"]["system_speaker_2"] = {"source": "system", "label": "Other speaker 2"}
        for start in (12000, 16000, 20000):
            transcript["segments"].append({"source": "system", "speaker": "system_speaker_2",
                "start_ms": start, "end_ms": start + 2000, "text": "Someone else at the same device."})
            timeline["intervals"].extend({"start_ms": point, "end_ms": point + 500, "active": ["doc-A:p2"]}
                for point in range(start, start + 2000, 500))
        result = match_timeline(transcript, timeline)
        self.assertEqual(result["matches"], {})
        self.assertIn("system_speaker_1", result["suggestions"])
        self.assertIn("system_speaker_2", result["suggestions"])

    def test_short_second_voice_is_also_shared_device_evidence(self):
        transcript, timeline = fixture()
        transcript["speakers"]["system_speaker_2"] = {"source": "system", "label": "Other speaker 2"}
        transcript["segments"].append({"source": "system", "speaker": "system_speaker_2",
            "start_ms": 12000, "end_ms": 12500, "text": "A short reply."})
        timeline["intervals"].append({"start_ms": 12000, "end_ms": 12500, "active": ["doc-A:p2"]})
        self.assertEqual(match_timeline(transcript, timeline)["matches"], {})

    def test_duplicate_names_across_documents_are_not_merged(self):
        transcript, timeline = fixture()
        timeline["participants"].append({"id": "doc-B:p2", "name": "Jamie Taylor", "self": False})
        self.assertEqual(match_timeline(transcript, timeline)["matches"], {})

    def test_truncated_capture_only_offers_suggestions(self):
        transcript, timeline = fixture()
        timeline["truncated"] = True
        result = match_timeline(transcript, timeline)
        self.assertEqual(result["matches"], {})
        self.assertIn("system_speaker_1", result["suggestions"])

    def test_explicit_introduction_takes_precedence_over_platform_display_name(self):
        transcript, timeline = fixture()
        transcript["segments"][0]["text"] = "Well, my name is Morgan."
        self.assertNotIn("system_speaker_1", match_timeline(transcript, timeline)["matches"])

    def test_missing_or_invalid_transcript_timing_never_creates_proof(self):
        for value in (None, True, -1, 100_000_000):
            transcript, timeline = fixture()
            transcript["segments"][0]["end_ms"] = value
            self.assertEqual(match_timeline(transcript, timeline)["matches"], {})

    def test_invalid_generic_or_overlong_display_names_do_not_break_review(self):
        for value in ("Speaker 2", "X" * 61):
            transcript, timeline = fixture()
            timeline["participants"][1]["name"] = value
            self.assertEqual(match_timeline(transcript, timeline)["matches"], {})

    def test_timeline_rejects_overlapping_stale_or_invented_observations(self):
        _, timeline = fixture()
        variants = []
        for change in ({"start_ms": True}, {"start_ms": -1}, {"end_ms": 2000},
                       {"active": ["unlisted"]}, {"active": ["doc-A:p2", "doc-A:p2"]}):
            changed = copy.deepcopy(timeline)
            changed["intervals"][0].update(change)
            variants.append(changed)
        changed = copy.deepcopy(timeline)
        changed["intervals"].insert(1, copy.deepcopy(changed["intervals"][0]))
        variants.append(changed)
        changed = copy.deepcopy(timeline)
        changed["participants"].append(copy.deepcopy(changed["participants"][0]))
        variants.append(changed)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_timeline(value)


class PlatformReviewTests(unittest.TestCase):
    def test_v1_context_stays_unchanged_and_cannot_carry_new_hidden_fields(self):
        old = {"schema_version": 1, "candidates": []}
        self.assertEqual(validate_context(old), old)
        with self.assertRaises(ValueError):
            validate_context(dict(old, timeline=fixture()[1]))
        with self.assertRaises(ValueError):
            validate_context({"schema_version": 2, "candidates": []})

    def test_matched_name_flows_through_same_validated_identity_layer(self):
        transcript, timeline = fixture()
        evidence = context(timeline)
        bundle = empty_bundle(transcript, evidence)
        bundle["report"] = bind_report(draft(), transcript, evidence)
        from quill_workflow.transcript_sources import speaker_attributions, segment_label
        people = apply_bundle(speaker_attributions(transcript), transcript, bundle)
        self.assertEqual(people["system_speaker_1"]["review_status"], "matched")
        self.assertEqual(segment_label(transcript["segments"][0], people), "Jamie Taylor")
        self.assertEqual(segment_label(transcript["segments"][1], people), "Casey Rivera (suggested)")
        bundle["corrections"]["assignments"]["system_speaker_1"] = {
            "name": "Morgan Lee", "email": None, "status": "user_confirmed"}
        people = apply_bundle(speaker_attributions(transcript), transcript, bundle)
        self.assertEqual(people["system_speaker_1"]["name"], "Morgan Lee")

    def test_model_cannot_invent_or_downgrade_platform_match(self):
        transcript, timeline = fixture()
        for change in ({"name": "Morgan Lee"}, {"evidence": [0]}, {"status": "confirmed"},
                       {"status": "suggested"}, {"status": "unresolved", "name": None, "email": None,
                                                "candidate_indices": []}):
            value = draft()
            value["speakers"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_report(value, transcript, context(timeline))
        value = draft()
        value["speakers"][1]["status"] = "matched"
        with self.assertRaisesRegex(ValueError, "independently validated"):
            bind_report(value, transcript, context(timeline))
        with self.assertRaisesRegex(ValueError, "independently validated"):
            bind_report(draft(), transcript, {"schema_version": 1, "candidates": context(timeline)["candidates"]})

    def test_tampering_timeline_invalidates_source_binding(self):
        transcript, timeline = fixture()
        evidence = context(timeline)
        report = bind_report(draft(), transcript, evidence)
        evidence["timeline"]["participants"][1]["name"] = "Morgan Lee"
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_report(report, transcript, evidence)

    def test_prompt_gets_compact_derived_evidence_not_raw_timeline(self):
        transcript, timeline = fixture()
        evidence = validate_context(context(timeline))
        compact = prompt_context(evidence)
        self.assertNotIn("timeline", compact)
        self.assertEqual(compact["candidates"][0]["candidate_index"], 0)
        self.assertEqual(platform_evidence(transcript, evidence)["matches"]["system_speaker_1"]["name"], "Jamie Taylor")


if __name__ == "__main__":
    unittest.main()
