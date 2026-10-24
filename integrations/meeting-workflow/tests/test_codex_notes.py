import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.codex_notes import SECTIONS, output_schema, run, validate_result
from quill_workflow.pipeline import WorkflowError, atomic_json
from test_transcript_sources import diarized_transcript


class CodexNotesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.transcript = self.root / "transcript.json"
        atomic_json(self.transcript, {"segments": [
            {"start_ms": 0, "speaker": "me", "text": "We agreed to send the agenda."}]})
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("Use supplied evidence only.")
        self.output = self.root / "notes.json"
        self.notes = {key: [] for key in SECTIONS}
        self.notes.update(schema_version=1, actions=[], follow_up_draft=None)
        self.notes["meeting_title"] = {"text": "Agenda planning", "evidence": [0], "participants": []}
        self.notes["summary"] = [{"text": "An agenda was agreed.", "evidence": [0]}]

    def tearDown(self):
        self.temp.cleanup()

    def fake(self, code):
        path = self.root / "fake codex"
        path.write_text("#!" + sys.executable + "\nimport sys, json, os, time\nfrom pathlib import Path\n" + code)
        path.chmod(0o700)
        return str(path)

    def invoke(self, executable, **options):
        return run(self.transcript, self.output, self.skill, executable, **options)

    def sequence(self, responses, exits=None):
        """A real subprocess for each attempt, with a durable call count."""
        calls = self.root / "calls.json"
        executable = self.fake(
            "payload = json.loads(sys.stdin.read())\n"
            "capture = Path(" + repr(str(calls)) + ")\n"
            "calls = json.loads(capture.read_text()) if capture.exists() else []\n"
            "index = len(calls)\n"
            "answer = Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"
            "calls.append({'payload':payload, 'cwd':os.getcwd(), 'argv':sys.argv, "
            "'answer_existed':answer.exists()})\n"
            "capture.write_text(json.dumps(calls))\n"
            "responses = " + repr(responses) + "\n"
            "exits = " + repr(exits or [0] * len(responses)) + "\n"
            "if responses[index] is not None: answer.write_text(responses[index])\n"
            "sys.exit(exits[index])\n")
        return executable, calls

    def test_stdin_isolated_command_and_private_validated_output(self):
        capture = self.root / "call.json"
        executable = self.fake(
            "payload = json.loads(sys.stdin.read())\n"
            "Path(" + repr(str(capture)) + ").write_text(json.dumps({'argv':sys.argv, 'cwd':os.getcwd(), "
            "'payload':payload, 'api_key_inherited':'OPENAI_API_KEY' in os.environ}))\n"
            "Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(" + repr(json.dumps(self.notes)) + ")\n")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-a-real-key"}):
            self.invoke(executable, model="user-model")
        call = json.loads(capture.read_text())
        self.assertEqual(json.loads(self.output.read_text()), self.notes)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertNotEqual(call["cwd"], str(self.root))
        self.assertFalse(Path(call["cwd"]).exists())
        self.assertFalse(call["api_key_inherited"])
        self.assertEqual(call["payload"]["transcript"], json.loads(self.transcript.read_text()))
        self.assertNotIn(str(self.transcript), call["argv"])
        for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config"):
            self.assertIn(flag, call["argv"])
        self.assertIn("user-model", call["argv"])
        for setting in ('forced_login_method="chatgpt"', 'web_search="disabled"',
                        "project_doc_max_bytes=0", "skills.include_instructions=false"):
            self.assertIn(setting, call["argv"])
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent"):
            i = call["argv"].index(feature)
            self.assertEqual(call["argv"][i-1], "--disable")

    def test_invalid_json_and_evidence_never_publish_output(self):
        bad_ref = dict(self.notes, summary=[{"text": "Invented", "evidence": [99]}])
        for payload in ("not json", json.dumps(bad_ref), json.dumps({"schema_version": 1})):
            with self.subTest(payload=payload):
                executable = self.fake("sys.stdin.read()\nPath(sys.argv[sys.argv.index('--output-last-message')+1])"
                                       ".write_text(" + repr(payload) + ")\n")
                with self.assertRaises(WorkflowError):
                    self.invoke(executable)
                self.assertFalse(self.output.exists())

    def test_incomplete_capture_is_grounded_and_duplicate_notice_gets_one_correction(self):
        from quill_workflow.capture_quality import NOTICE
        transcript = json.loads(self.transcript.read_text())
        transcript["capture_quality"] = {"status": "incomplete", "affected_sources": ["system", "mic"]}
        atomic_json(self.transcript, transcript)
        duplicate = dict(self.notes, summary=[{"text": "An agenda was agreed. " + NOTICE, "evidence": [0]}])
        executable, captured = self.sequence([json.dumps(duplicate), json.dumps(self.notes)])
        self.invoke(executable)
        calls = json.loads(captured.read_text())
        self.assertEqual(len(calls), 2)
        for call in calls:
            payload = call["payload"]
            self.assertEqual(payload["capture_quality"], {"status": "incomplete", "affected_sources": ["mic", "system"]})
            self.assertEqual(payload["transcript"]["segments"], transcript["segments"])
            self.assertIn("never infer or invent missing speech", payload["capture_quality_instruction"])
        self.assertIn("exporter adds it once", calls[1]["payload"]["validation_error"])
        self.assertEqual(json.loads(self.output.read_text()), self.notes)

    def test_malformed_capture_quality_does_not_launch_model(self):
        transcript = json.loads(self.transcript.read_text())
        transcript["capture_quality"] = {"status": "incomplete", "affected_sources": ["system"], "error": "PRIVATE DEVICE ERROR"}
        atomic_json(self.transcript, transcript)
        executable, captured = self.sequence([json.dumps(self.notes)])
        with self.assertRaisesRegex(WorkflowError, "Capture quality"):
            self.invoke(executable)
        self.assertFalse(captured.exists())
        self.assertFalse(self.output.exists())

    def test_new_generation_requires_a_title_but_historical_notes_still_validate(self):
        historical = dict(self.notes)
        del historical["meeting_title"]
        transcript = json.loads(self.transcript.read_text())
        validate_result(historical, transcript)
        with self.assertRaises(WorkflowError):
            validate_result(historical, transcript, require_title=True)
        self.assertIn("meeting_title", output_schema()["required"])
        executable = self.fake("sys.stdin.read()\nPath(sys.argv[sys.argv.index('--output-last-message')+1])"
                               ".write_text(" + repr(json.dumps(historical)) + ")\n")
        with self.assertRaises(WorkflowError):
            self.invoke(executable)
        self.assertFalse(self.output.exists())

    def test_invalid_title_is_never_accepted_from_the_model(self):
        notes = dict(self.notes, meeting_title={"text": "Agenda planning", "evidence": [0],
                                               "participants": [{"name": "Max", "evidence": [0]}]})
        executable = self.fake("sys.stdin.read()\nPath(sys.argv[sys.argv.index('--output-last-message')+1])"
                               ".write_text(" + repr(json.dumps(notes)) + ")\n")
        with self.assertRaisesRegex(WorkflowError, "absent from its cited evidence"):
            self.invoke(executable)
        self.assertFalse(self.output.exists())

    def test_invalid_participant_is_corrected_once_with_same_source_and_draft(self):
        rejected = dict(self.notes, meeting_title={"text": "Agenda planning", "evidence": [0],
                         "participants": [{"name": "Max", "evidence": [0]}]})
        raw = json.dumps(rejected)
        executable, capture = self.sequence([raw, json.dumps(self.notes)])
        original = self.transcript.read_bytes()
        self.invoke(executable)
        calls = json.loads(capture.read_text())
        self.assertEqual(len(calls), 2)
        correction = calls[1]["payload"]
        self.assertEqual(correction["transcript"], calls[0]["payload"]["transcript"])
        self.assertEqual(correction["call_summary_skill"], self.skill.read_text())
        self.assertEqual(correction["rejected_draft"], raw)
        self.assertIn("absent from its cited evidence", correction["validation_error"])
        self.assertIn("untrusted data", correction["task"])
        self.assertEqual(self.transcript.read_bytes(), original)
        self.assertEqual(json.loads(self.output.read_text()), self.notes)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertNotEqual(calls[0]["cwd"], calls[1]["cwd"])
        for call in calls:
            self.assertFalse(call["answer_existed"])
            self.assertFalse(Path(call["cwd"]).exists())
            self.assertIn("--ignore-user-config", call["argv"])
            self.assertIn("--ephemeral", call["argv"])

    def test_invalid_json_schema_and_evidence_can_be_corrected(self):
        invalid = ("not json", json.dumps({"schema_version": 1}), json.dumps(
            dict(self.notes, summary=[{"text": "An agenda was agreed.", "evidence": [99]}])))
        for raw in invalid:
            with self.subTest(raw=raw):
                executable, capture = self.sequence([raw, json.dumps(self.notes)])
                self.invoke(executable)
                calls = json.loads(capture.read_text())
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1]["payload"]["rejected_draft"], raw)
                self.assertEqual(json.loads(self.output.read_text()), self.notes)
                self.output.unlink()
                capture.unlink()

    def test_second_invalid_draft_fails_without_a_third_call_or_output(self):
        invalid = dict(self.notes, summary=[{"text": "An agenda was agreed.", "evidence": [99]}])
        executable, capture = self.sequence([json.dumps(invalid), json.dumps(invalid)])
        with self.assertRaisesRegex(WorkflowError,
                                   "after one correction: Invalid transcript segment reference") as raised:
            self.invoke(executable)
        self.assertEqual(len(json.loads(capture.read_text())), 2)
        self.assertLessEqual(len(str(raised.exception)), 560)
        self.assertFalse(self.output.exists())

    def test_valid_first_draft_is_accepted_unchanged_without_correction(self):
        executable, capture = self.sequence([json.dumps(self.notes)])
        self.invoke(executable)
        self.assertEqual(len(json.loads(capture.read_text())), 1)
        self.assertEqual(json.loads(self.output.read_text()), self.notes)

    def test_cli_error_is_not_retried_even_with_a_valid_answer_file(self):
        executable, capture = self.sequence([json.dumps(self.notes)], exits=[9])
        with self.assertRaisesRegex(WorkflowError, "status 9"):
            self.invoke(executable)
        self.assertEqual(len(json.loads(capture.read_text())), 1)
        self.assertFalse(self.output.exists())

    def test_missing_correction_output_never_reuses_first_candidate(self):
        executable, capture = self.sequence(["not json", None])
        with self.assertRaisesRegex(WorkflowError, "regular Codex answer file"):
            self.invoke(executable)
        calls = json.loads(capture.read_text())
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["cwd"], calls[1]["cwd"])
        self.assertTrue(all(not call["answer_existed"] for call in calls))
        self.assertFalse(self.output.exists())

    def test_correction_uses_remaining_timeout_and_cleans_up_each_process(self):
        clock = [100.0]
        processes, allowed_times = [], []

        def start(argv, **options):
            process = Mock(returncode=0)
            index = len(processes)
            answer = Path(argv[argv.index("--output-last-message") + 1])

            def communicate(payload, timeout):
                allowed_times.append(timeout)
                if index == 0:
                    answer.write_text("not json")
                    clock[0] += 6
                else:
                    # A complete-looking file from a timed-out run is never accepted.
                    answer.write_text(json.dumps(self.notes))
                    clock[0] += timeout
                    raise subprocess.TimeoutExpired(argv, timeout)

            process.communicate.side_effect = communicate
            processes.append(process)
            return process

        with patch("quill_workflow.codex_notes.subprocess.Popen", side_effect=start), \
                patch("quill_workflow.codex_notes.time.monotonic", side_effect=lambda: clock[0]), \
                patch("quill_workflow.codex_notes.stop_process_group") as cleanup:
            with self.assertRaisesRegex(WorkflowError, "timed out"):
                self.invoke(sys.executable, timeout=10)
        self.assertEqual(allowed_times, [10.0, 4.0])
        self.assertEqual([call.args[0] for call in cleanup.call_args_list], processes)
        self.assertFalse(self.output.exists())

    def test_failure_and_missing_executable_preserve_existing_files(self):
        original = self.transcript.read_bytes()
        with self.assertRaisesRegex(WorkflowError, "not installed"):
            self.invoke(str(self.root / "missing-codex"))
        with self.assertRaisesRegex(WorkflowError, "status 9"):
            self.invoke(self.fake("sys.stdin.read()\nsys.exit(9)\n"))
        self.assertEqual(self.transcript.read_bytes(), original)
        self.assertFalse(self.output.exists())
        self.output.write_text("keep this revision")
        with self.assertRaisesRegex(WorkflowError, "already exists"):
            self.invoke(self.fake("raise AssertionError('must not launch')\n"))
        self.assertEqual(self.output.read_text(), "keep this revision")

    def test_timeout_cleans_up_child_process_group(self):
        marker = self.root / "child-survived"
        executable = self.fake("import subprocess\nsys.stdin.read()\n"
            "subprocess.Popen([sys.executable, '-c', " + repr(
                "import time; from pathlib import Path; time.sleep(0.8); Path(" + repr(str(marker)) + ").touch()") + "])\n"
            "time.sleep(10)\n")
        with self.assertRaisesRegex(WorkflowError, "timed out"):
            self.invoke(executable, timeout=0.2)
        time.sleep(1)
        self.assertFalse(marker.exists())
        self.assertFalse(self.output.exists())

    def test_empty_transcript_does_not_start_harness(self):
        atomic_json(self.transcript, {"segments": []})
        with self.assertRaisesRegex(WorkflowError, "no segments"):
            self.invoke(self.fake("raise AssertionError('must not launch')\n"))

    def test_anonymous_action_owners_require_null_but_named_owners_are_allowed(self):
        transcript = json.loads(self.transcript.read_text())
        for owner in ("me", "them", "Other Speaker", " participant ", "speaker 2", "Participant_A",
                      "Microphone", "System audio", "Other participants", "You", "host"):
            with self.subTest(owner=owner):
                notes = dict(self.notes, actions=[{"text": "Send the agenda", "evidence": [0],
                                                   "owner": owner, "due": None}])
                with self.assertRaisesRegex(WorkflowError, "anonymous speaker label"):
                    validate_result(notes, transcript)
        for owner in (None, "Alex", "Mei Chen", "Themis Jones"):
            with self.subTest(owner=owner):
                transcript["segments"][0]["text"] = f"{owner or 'We'} agreed to send the agenda."
                notes = dict(self.notes, actions=[{"text": "Send the agenda", "evidence": [0],
                                                   "owner": owner, "due": None}])
                validate_result(notes, transcript)

    def test_owner_name_requires_its_own_cited_text_not_audio_source_or_other_segment(self):
        transcript = {"segments": [
            {"start_ms": 0, "speaker": "them", "text": "I'm Max."},
            {"start_ms": 1000, "speaker": "them", "text": "I will send the agenda."}]}
        notes = dict(self.notes, actions=[{"text": "Send the agenda", "evidence": [1],
                                           "owner": "Max", "due": None}])
        with self.assertRaisesRegex(WorkflowError, "absent from its cited transcript evidence"):
            validate_result(notes, transcript)
        notes["actions"][0]["evidence"] = [0, 1]
        # Textual support is necessary but does not prove that a mixed track's
        # later statement was made by this person. The runner also instructs
        # the model to leave the owner unresolved when attribution is unclear.
        validate_result(notes, transcript)

    def test_owner_names_cannot_be_invented_extended_or_substring_matched(self):
        transcript = {"segments": [{"start_ms": 0, "speaker": "Max",
                                    "text": "Mei Chen will send the agenda."}]}
        for owner in ("Max", "Mei Chen Jones", "Cheney", "", "  ", "Mei\nChen"):
            with self.subTest(owner=owner), self.assertRaisesRegex(WorkflowError, "Action owner"):
                validate_result(dict(self.notes, actions=[{
                    "text": "Send the agenda", "evidence": [0], "owner": owner, "due": None}]), transcript)
        transcript["segments"][0]["text"] = "Themis will send the agenda."
        with self.assertRaisesRegex(WorkflowError, "absent from its cited transcript evidence"):
            validate_result(dict(self.notes, actions=[{
                "text": "Send the agenda", "evidence": [0], "owner": "Themi", "due": None}]), transcript)

    def test_owner_name_spelling_supports_unicode_normalization(self):
        transcript = {"segments": [{"start_ms": 0, "speaker": "me",
                                    "text": "Jose\u0301 will send the agenda."}]}
        validate_result(dict(self.notes, actions=[{
            "text": "Send the agenda", "evidence": [0], "owner": "José", "due": None}]), transcript)

    def test_named_separated_speaker_can_own_a_later_cited_commitment(self):
        transcript = diarized_transcript()
        notes = dict(self.notes, summary=[{"text": "The agenda will be sent.", "evidence": [2]}],
                     actions=[{"text": "Send the agenda", "evidence": [2], "owner": "Jamie", "due": None}])
        validate_result(notes, transcript)
        notes["actions"][0]["owner"] = "Anna"
        with self.assertRaisesRegex(WorkflowError, "absent from its cited transcript evidence"):
            validate_result(notes, transcript)
        notes["actions"][0].update(owner="Jamie", evidence=[4])
        with self.assertRaisesRegex(WorkflowError, "absent from its cited transcript evidence"):
            validate_result(notes, transcript)

    def test_new_separated_labels_are_never_action_owner_names(self):
        for owner in ("Other speaker 1", "Microphone speaker 1", "mic_speaker_1", "system_speaker_1",
                      "Remote speaker 2", "System audio (unidentified)"):
            with self.subTest(owner=owner), self.assertRaisesRegex(WorkflowError, "anonymous speaker label"):
                validate_result(dict(self.notes, actions=[{"text": "Send agenda", "evidence": [2],
                                 "owner": owner, "due": None}]), diarized_transcript())

    def test_diarized_harness_receives_only_names_with_validated_provenance(self):
        transcript = diarized_transcript()
        atomic_json(self.transcript, transcript)
        notes = dict(self.notes, actions=[{"text": "Send agenda", "evidence": [2], "owner": "Jamie", "due": None}])
        executable, capture = self.sequence([json.dumps(notes)])
        self.invoke(executable)
        payload = json.loads(capture.read_text())[0]["payload"]
        self.assertEqual(payload["validated_speaker_names"]["system_speaker_1"]["name"], "Jamie")
        self.assertEqual(payload["validated_speaker_names"]["system_speaker_1"]["evidence"], [1])
        self.assertEqual(payload["transcript"], transcript)
        self.assertEqual(json.loads(self.transcript.read_text()), transcript)

    def test_forged_speaker_name_is_rejected_before_model_invocation(self):
        transcript = diarized_transcript()
        transcript["speakers"]["system_speaker_1"].update(name="Max", name_source="calendar")
        atomic_json(self.transcript, transcript)
        executable, capture = self.sequence([json.dumps(self.notes)])
        with self.assertRaisesRegex(WorkflowError, "self-introduction"):
            self.invoke(executable)
        self.assertFalse(capture.exists())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
