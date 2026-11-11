"""Synthetic recorder defaults in current and historical local meeting views."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_review_library as fixtures
from test_speaker_timeline import fixture as separated_call
from quill_workflow.pipeline import atomic_json
from quill_workflow.review_library import meeting_detail, meeting_index, meeting_library
from quill_workflow.speaker_corrections import SpeakerCorrections


class ReviewMicrophoneOwnerTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ReviewLibraryTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        home = patch.dict(os.environ, HOME=str(self.f.root))
        home.start()
        self.addCleanup(home.stop)
        self.config = self.f.root / ".config/quill/config.json"
        self.settings = {"transcription": {"remote_microphone_owner": "Casey Rivera"},
                         "completion_status_file": str(self.f.state / "status.json")}
        atomic_json(self.config, self.settings)
        self.context = {"provider": "google-meet", "key": "abc-defg-hij", "coverage": "visible_only",
                        "participants": [{"name": "Jordan Lee", "self": False}]}
        self.meta = dict(self.f.metadata, meeting_context=self.context)
        self.meta_path = self.f.recording / "meta.json"
        self.transcript_path = self.f.recording / "transcript.json"
        atomic_json(self.meta_path, self.meta)
        self.speakers = SpeakerCorrections(self.f.state)

    def detail(self):
        return meeting_detail(self.f.state, self.f.job)

    def test_explicit_capture_names_unseparated_mic_without_current_config_or_roster(self):
        self.config.unlink()
        transcript = {"remote_microphone_owner": "Casey Rivera", "segments": [
            {"speaker": "me", "start_ms": 10, "text": "Local turn"},
            {"speaker": "them", "start_ms": 1000, "text": "Remote turn"}]}
        atomic_json(self.transcript_path, transcript)
        for view in (self.detail(), meeting_library(self.f.state)[self.f.job]):
            self.assertIn("Casey Rivera: Local turn", view["transcript_text"])
            self.assertIn("System audio: Remote turn", view["transcript_text"])
            self.assertEqual(view["participants"], ["Casey Rivera"])
            self.assertEqual(view["microphone_owner_source"], "recording")
        options = self.speakers.options(self.f.job)
        self.assertEqual((options["microphone_owner"], options["microphone_owner_segments"]), ("Casey Rivera", 1))
        self.assertEqual(options["speakers"], [])

    def test_self_badge_on_bound_timeline_relabels_history_only_in_selected_views(self):
        transcript, timeline = separated_call()
        transcript["segments"].append({"source": "mic", "speaker": "unknown", "start_ms": 15000, "text": "Local gap"})
        transcript["segments"].append({"source": "system", "speaker": "unknown", "start_ms": 16000, "text": "Remote gap"})
        self.meta["speaker_activity_file"] = "speaker-activity.json"
        atomic_json(self.meta_path, self.meta)
        atomic_json(self.f.recording / "speaker-activity.json", timeline)
        atomic_json(self.transcript_path, transcript)
        protected = [self.transcript_path, self.f.result / "publish-receipt.json", self.f.result / "notes/notes.json",
                     self.f.result / "drive-publish.json"]
        before = [path.read_bytes() for path in protected]
        detail = self.detail()
        self.assertEqual(detail["microphone_owner_source"], "historical_meeting")
        self.assertEqual(detail["microphone_owner_segments"], 4)
        self.assertIn("Casey Rivera: Local gap", detail["transcript_text"])
        self.assertIn("System audio (unidentified): Remote gap", detail["transcript_text"])
        self.assertEqual(detail["participants"], ["Casey Rivera"])
        options = self.speakers.options(self.f.job)
        mic = next(person for person in options["speakers"] if person["id"] == "mic_speaker_1")
        self.assertEqual((mic["name"], mic["status"]), ("Casey Rivera", "microphone_owner"))
        self.assertTrue(mic["reason"])
        self.assertEqual(options["microphone_owner_segments"], 4)
        with patch("quill_workflow.speaker_timeline.validate_timeline", side_effect=AssertionError("Unselected timeline opened")):
            self.assertNotIn("microphone_owner", meeting_library(self.f.state)[self.f.job])
            self.assertNotIn("microphone_owner", meeting_index(self.f.state)[self.f.job])
        self.assertEqual([path.read_bytes() for path in protected], before)

    def test_historical_labels_require_exact_recorder_and_bound_config_and_no_conflicting_self(self):
        self.context.update(provider="zoom-web", key="12345678901",
                            participants=[{"name": "Casey Rivera", "self": False}])
        atomic_json(self.meta_path, self.meta)
        # The signed-in participant can be absent on an older Zoom call. The
        # exact displayed recorder name supports the configured microphone.
        detail = self.detail()
        self.assertEqual(detail["microphone_owner"], "Casey Rivera")
        self.assertEqual(detail["storage_status"], "saved_in_drive")
        self.assertEqual(self.speakers.options(self.f.job)["microphone_owner_segments"], 1)
        self.context["participants"].append({"name": "Someone Else", "self": True})
        atomic_json(self.meta_path, self.meta)
        self.assertNotIn("microphone_owner", self.detail())
        self.context["participants"] = [{"name": "Casey", "self": False}]
        atomic_json(self.meta_path, self.meta)
        self.assertNotIn("microphone_owner", self.detail())
        self.context["participants"] = [{"name": "Casey Rivera", "self": True}]
        atomic_json(self.meta_path, dict(self.meta, remote_microphone_owner=None))
        self.assertNotIn("microphone_owner", self.detail())
        atomic_json(self.meta_path, self.meta)
        atomic_json(self.config, dict(self.settings, completion_status_file=str(self.f.root / "other/status.json")))
        self.assertNotIn("microphone_owner", self.detail())
        self.assertIsNone(self.speakers.options(self.f.job)["microphone_owner"])
        self.config.unlink()
        self.assertNotIn("microphone_owner", self.detail())

    def test_wrong_call_or_conflicting_timeline_cannot_assign_owner(self):
        _, timeline = separated_call()
        self.meta["speaker_activity_file"] = "speaker-activity.json"
        self.context["participants"].append({"name": "Casey Rivera", "self": True})
        atomic_json(self.meta_path, self.meta)
        sidecar = self.f.recording / "speaker-activity.json"
        for different in (dict(timeline, key="xyz-abcd-efg"), dict(timeline, participants=[
                *timeline["participants"], {"id": "p3", "name": "Other Person", "self": True}])):
            with self.subTest(key=different["key"]):
                atomic_json(sidecar, different)
                self.assertNotIn("microphone_owner", self.detail())

    def test_positive_mic_identification_and_user_correction_override_the_recorder(self):
        transcript, _ = separated_call()
        transcript["remote_microphone_owner"] = "Casey Rivera"
        mic = transcript["speakers"]["mic_speaker_1"]
        mic.update(name="Avery", name_source="self_introduction", evidence=[1])
        transcript["segments"][1]["text"] = "My name is Avery."
        atomic_json(self.transcript_path, transcript)
        detail = self.detail()
        self.assertEqual(detail["participants"], ["Avery"])
        self.assertNotIn("microphone_owner", detail)
        self.assertIn("Avery: My name is Avery.", detail["transcript_text"])
        options = self.speakers.options(self.f.job)
        self.speakers.decision("speaker_save", self.f.job, options["revision"], "mic_speaker_1", name="Jordan")
        self.assertEqual(self.detail()["participants"], ["Jordan"])
        person = next(p for p in self.speakers.options(self.f.job)["speakers"] if p["id"] == "mic_speaker_1")
        self.assertEqual((person["name"], person["status"]), ("Jordan", "user_confirmed"))

    def test_silent_mic_does_not_add_recorder_to_participant_list(self):
        transcript, _ = separated_call()
        transcript["remote_microphone_owner"] = "Casey Rivera"
        transcript["segments"] = [s for s in transcript["segments"] if s["source"] == "system"]
        atomic_json(self.transcript_path, transcript)
        detail = self.detail()
        self.assertEqual(detail["participants"], [])
        self.assertNotIn("microphone_owner", detail)
        options = self.speakers.options(self.f.job)
        self.assertIsNone(options["microphone_owner"])
        mic = next(person for person in options["speakers"] if person["id"] == "mic_speaker_1")
        self.assertEqual(mic["status"], "unresolved")
