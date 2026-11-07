"""Focused identity review integrates with notes through synthetic local runners."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.codex_notes import SECTIONS, _prompt_transcript, run
from quill_workflow.pipeline import ConnectorRunner, Queue, WorkflowError, atomic_json
from quill_workflow.speaker_identity import empty_bundle, validate_bundle
from quill_workflow.speaker_review import bind_report
from quill_workflow.transcript_sources import effective_attributions
from test_focused_speaker_review import source, context, draft


def prompt_context():
    value = context()
    return dict(value, candidates=[dict(candidate, candidate_index=index)
                                   for index, candidate in enumerate(value["candidates"])])


def notes():
    result = {key: [] for key in SECTIONS}
    result.update(schema_version=1, actions=[], follow_up_draft=None,
                  meeting_title={"text": "Agenda planning", "evidence": [1, 2], "participants": []},
                  speaker_review={"schema_version": 1, "assignments": []})
    result["summary"] = [{"text": "The agenda will be sent this evening.", "evidence": [1, 2]}]
    return result


class SpeakerPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="quill synthetic speaker integration ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input, self.output = self.root / "transcript.json", self.root / "notes.json"
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("Summarize only supplied evidence.")
        self.context_path = self.root / "speaker-context.json"
        atomic_json(self.input, source())
        atomic_json(self.context_path, context())

    def fake(self, responses):
        executable, capture = self.root / "fake codex", self.root / "model calls.json"
        executable.write_text("#!" + sys.executable + "\nimport json, sys\nfrom pathlib import Path\n"
            "capture = Path(" + repr(str(capture)) + ")\n"
            "calls = json.loads(capture.read_text()) if capture.exists() else []\n"
            "index = len(calls)\n"
            "calls.append({'payload':json.loads(sys.stdin.read()),'argv':sys.argv,"
            "'instructions':Path('instructions.txt').read_text()})\n"
            "capture.write_text(json.dumps(calls))\n"
            "responses = " + repr([json.dumps(value) for value in responses]) + "\n"
            "Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(responses[index])\n")
        executable.chmod(0o700)
        return str(executable), capture

    def identity(self, corrections=False):
        identity = empty_bundle(source(), context())
        identity["report"] = bind_report(draft(), source(), context())
        if corrections:
            identity["corrections"]["assignments"]["system_speaker_1"] = {
                "name": "Jamie Taylor", "email": "jamie@example.com", "status": "user_confirmed"}
        path = self.root / "identity.json"
        atomic_json(path, identity)
        return path, identity

    def invoke(self, executable, **kwargs):
        return run(self.input, self.output, self.skill, executable, timeout=30, **kwargs)

    def test_focused_review_precedes_notes_and_attaches_caller_validated_identity(self):
        executable, capture = self.fake([draft(), notes()])
        original = self.input.read_bytes()
        self.invoke(executable, speaker_context_path=self.context_path)
        calls = json.loads(capture.read_text())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["payload"]["evidence_context"], prompt_context())
        bundle = calls[1]["payload"]["focused_speaker_review"]
        validate_bundle(bundle, source())
        self.assertEqual(bundle["context"], context())
        self.assertEqual(bundle["corrections"]["assignments"], {})
        self.assertEqual(bundle["report"]["speakers"], draft()["speakers"])
        self.assertIn("mic_speaker_1", calls[1]["payload"]["validated_speaker_names"])
        self.assertNotIn("system_speaker_1", calls[1]["payload"]["validated_speaker_names"])
        saved = json.loads(self.output.read_text())
        self.assertEqual(saved["_speaker_identity"], bundle)
        people = effective_attributions(source(), saved)
        self.assertEqual(people["mic_speaker_1"]["name"], "Anna")
        self.assertEqual(people["system_speaker_1"]["suggested_name"], "Jamie Taylor")
        self.assertNotIn("name", people["system_speaker_1"])
        self.assertEqual(self.input.read_bytes(), original)

    def test_supplied_correction_survives_notes_validation_retry_without_another_identity_review(self):
        identity_path, identity = self.identity(corrections=True)
        good = notes()
        good["actions"] = [{"text": "Send the agenda", "owner": "Jamie Taylor", "due": None, "evidence": [2]}]
        bad = copy.deepcopy(good)
        bad["summary"][0]["evidence"] = [99]
        executable, capture = self.fake([bad, good])
        self.invoke(executable, identity_path=identity_path)
        calls = json.loads(capture.read_text())
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call["payload"]["focused_speaker_review"], identity)
            self.assertEqual(call["payload"]["transcript"], _prompt_transcript(source()))
            person = call["payload"]["validated_speaker_names"]["system_speaker_1"]
            self.assertEqual(person["name"], "Jamie Taylor")
            self.assertEqual(person["review_status"], "user_confirmed")
        self.assertEqual(calls[1]["payload"]["rejected_draft"], json.dumps(bad))
        self.assertIn("Invalid transcript segment reference", calls[1]["payload"]["validation_error"])
        saved = json.loads(self.output.read_text())
        self.assertEqual(saved["_speaker_identity"], identity)
        self.assertEqual(saved["actions"][0]["owner"], "Jamie Taylor")

    def test_model_cannot_supply_a_forged_trusted_identity_envelope(self):
        identity_path, identity = self.identity()
        forged = notes()
        forged["_speaker_identity"] = copy.deepcopy(identity)
        forged["_speaker_identity"]["corrections"]["assignments"]["system_speaker_1"] = {
            "name": "Invented Person", "email": "invented@example.com", "status": "user_confirmed"}
        executable, capture = self.fake([forged, forged])
        with self.assertRaisesRegex(WorkflowError, "cannot supply trusted speaker corrections"):
            self.invoke(executable, identity_path=identity_path)
        self.assertFalse(self.output.exists())
        calls = json.loads(capture.read_text())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["payload"]["focused_speaker_review"], identity)
        self.assertEqual(calls[1]["payload"]["focused_speaker_review"]["corrections"]["assignments"], {})

    def test_focused_review_cannot_be_overridden_through_legacy_assignments(self):
        identity_path, identity = self.identity()
        identity["report"]["speakers"][0] = {"speaker_id": "mic_speaker_1", "status": "unresolved",
            "name": None, "email": None, "evidence": [0], "candidate_indices": [],
            "reason": "The introduction is uncertain and needs a person to review it."}
        atomic_json(identity_path, identity)
        invalid = notes()
        invalid["speaker_review"]["assignments"] = [{"speaker_id": "mic_speaker_1", "name": "Anna",
            "status": "confirmed", "evidence": [0], "context_participants": []}]
        executable, capture = self.fake([invalid, notes()])
        self.invoke(executable, identity_path=identity_path)
        calls = json.loads(capture.read_text())
        self.assertEqual(len(calls), 2)
        self.assertIn("focused", calls[1]["payload"]["validation_error"].casefold())
        saved = json.loads(self.output.read_text())
        self.assertEqual(saved["speaker_review"]["assignments"], [])
        self.assertNotIn("name", effective_attributions(source(), saved)["mic_speaker_1"])

    def test_legacy_notes_runner_without_speaker_context_remains_one_model_call(self):
        executable, capture = self.fake([notes()])
        self.invoke(executable)
        calls = json.loads(capture.read_text())
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["payload"].get("focused_speaker_review"))
        saved = json.loads(self.output.read_text())
        self.assertNotIn("_speaker_identity", saved)
        self.assertEqual(saved["summary"], notes()["summary"])

    def test_malformed_legacy_review_gets_normal_correction_with_focused_identity(self):
        identity_path, identity = self.identity()
        malformed = notes()
        malformed["speaker_review"] = None
        executable, capture = self.fake([malformed, notes()])
        self.invoke(executable, identity_path=identity_path)
        calls = json.loads(capture.read_text())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["payload"]["focused_speaker_review"], identity)
        self.assertEqual(json.loads(self.output.read_text())["_speaker_identity"], identity)

    def pipeline_fixture(self, settings, transcript=None):
        recordings = self.root / "recordings"
        recording = recordings / "synthetic meeting"
        recording.mkdir(parents=True)
        (recording / "mic.caf").write_bytes(b"synthetic transport fixture; not real audio")
        (recording / "system.caf").write_bytes(b"synthetic transport fixture; not real audio")
        atomic_json(recording / "meta.json", {"started": "2026-01-01T10:00:00Z", "ended": "2026-01-01T10:01:00Z",
                    "files": {"mic": "mic.caf", "system": "system.caf"}})
        atomic_json(recording / "transcript.json", transcript or source())
        config = {"recordings_root": str(recordings), "notes": settings}
        config_path = self.root / "workflow config.json"
        atomic_json(config_path, config)
        queue = Queue(self.root / "state")
        self.addCleanup(queue.close)
        job = queue.enqueue(recording, config)
        return job, queue, ConnectorRunner(queue.state, self.skill, config_path=config_path)

    def test_known_pipeline_injects_context_and_saves_focused_output_without_changing_source(self):
        executable, capture = self.fake([draft(), notes()])
        settings = {"enabled": True, "command": [sys.executable, "-B", str(ROOT / "quill_workflow/codex_notes.py"),
                    "--input", "{input}", "--output", "{output}", "--skill", "{skill}",
                    "--context", "{context}", "--codex", executable, "--timeout", "30"]}
        job, queue, runner = self.pipeline_fixture(settings)
        seen = []
        execute = runner.execute
        def capture_execute(argv, *args, **kwargs):
            seen.append(list(argv))
            path = Path(argv[argv.index("--speaker-context") + 1])
            self.assertEqual(json.loads(path.read_text()), context())
            return execute(argv, *args, **kwargs)
        runner.execute = capture_execute
        source_path = queue.state / "recordings" / job / "transcript.json"
        original = source_path.read_bytes()
        with patch("quill_workflow.speaker_context.build_context", return_value=(context(), "matched_invitation")) as lookup:
            receipt = runner("notes", job, settings)
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(lookup.call_args.args[0], job)
        self.assertEqual(lookup.call_args.args[2], source())
        self.assertEqual(seen[0].count("--speaker-context"), 1)
        folder = queue.state / "results" / job / "notes"
        saved = json.loads((folder / "notes.json").read_text())
        validate_bundle(saved["_speaker_identity"], source())
        self.assertIn("Jamie Taylor (suggested)", (folder / "transcript-reviewed.md").read_text())
        self.assertEqual(source_path.read_bytes(), original)
        self.assertEqual((folder / "source-transcript.json").read_bytes(), original)
        self.assertTrue(receipt["context_sha256"])
        self.assertEqual(json.loads(capture.read_text())[0]["payload"]["evidence_context"], prompt_context())

    def test_non_diarized_browser_call_files_visible_guest_without_voice_review(self):
        transcript = {"segments": [{"start_ms": 0, "speaker": "me", "text": "We covered logistics."}]}
        candidate = notes()
        del candidate["speaker_review"]
        candidate["meeting_title"]["evidence"] = [0]
        candidate["summary"][0]["evidence"] = [0]
        executable, capture = self.fake([candidate])
        settings = {"enabled": True, "command": [sys.executable, "-B", str(ROOT / "quill_workflow/codex_notes.py"),
                    "--input", "{input}", "--output", "{output}", "--skill", "{skill}",
                    "--context", "{context}", "--codex", executable, "--timeout", "30"]}
        job, queue, runner = self.pipeline_fixture(settings, transcript)
        metadata = queue.state / "recordings" / job / "meta.json"
        value = json.loads(metadata.read_text())
        value["meeting_context"] = {"provider": "google-meet", "key": "abc-defg-hij", "coverage": "visible_only",
                                    "participants": [{"name": "Mira", "self": True}, {"name": "Lin", "self": False},
                                                     {"name": "Ada", "self": False}]}
        atomic_json(metadata, value)
        context = {"schema_version": 1, "organizer": {"name": "Lin", "email": "lin@example.test"},
                   "candidates": [{"name": "Mira", "email": "mira@example.test", "aliases": [], "source": "calendar"},
                                  {"name": "Ada", "email": "ada@example.test", "aliases": [], "source": "calendar"}]}
        with patch("quill_workflow.speaker_context.build_context", return_value=(context, "matched_invitation")):
            runner("notes", job, settings)
        self.assertEqual(len(json.loads(capture.read_text())), 1)
        folder = queue.state / "results" / job / "notes"
        self.assertTrue((folder / "notes.md").read_text().startswith("# Ada - Agenda planning\n"))
        self.assertNotIn("_speaker_identity", json.loads((folder / "notes.json").read_text()))

    def test_custom_notes_adapter_receives_no_new_flags_or_calendar_access(self):
        script, capture = self.root / "legacy adapter.py", self.root / "legacy argv.json"
        legacy_notes = notes()
        del legacy_notes["speaker_review"]
        script.write_text("import json,sys\nfrom pathlib import Path\n"
            "Path(" + repr(str(capture)) + ").write_text(json.dumps(sys.argv))\n"
            "Path(sys.argv[2]).write_text(" + repr(json.dumps(legacy_notes)) + ")\n")
        settings = {"enabled": True, "command": [sys.executable, "-B", str(script), "{input}", "{output}"]}
        job, queue, runner = self.pipeline_fixture(settings)
        with patch("quill_workflow.speaker_context.build_context", side_effect=AssertionError("Legacy adapter looked up calendar")):
            runner("notes", job, settings)
        argv = json.loads(capture.read_text())
        self.assertEqual(len(argv), 3)
        self.assertNotIn("--speaker-context", argv)
        result = json.loads((queue.state / "results" / job / "notes/notes.json").read_text())
        self.assertNotIn("_speaker_identity", result)


if __name__ == "__main__":
    unittest.main()
