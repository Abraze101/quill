"""Derived notes refresh is local, source-bound and cancellation aware."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow import codex_notes
from quill_workflow.local_deletion import _lock
from quill_workflow.pipeline import WorkflowError, atomic_json
from quill_workflow.review_library import _Sources, meeting_detail
from quill_workflow.speaker_notes import (_lock_path, load_reviewed_notes, read_status,
                                         refresh, start_refresh)
import test_speaker_corrections as corrections_tests


class SpeakerNotesTests(unittest.TestCase):
    options = corrections_tests.SpeakerCorrectionsTests.options
    decide = corrections_tests.SpeakerCorrectionsTests.decide
    saved_bundle = corrections_tests.SpeakerCorrectionsTests.saved_bundle
    assert_originals_unchanged = corrections_tests.SpeakerCorrectionsTests.assert_originals_unchanged

    def setUp(self):
        corrections_tests.SpeakerCorrectionsTests.setUp(self)
        self.config = self.root / "workflow.json"
        self.settings = {"notes": {"enabled": True, "timeout_seconds": 960, "command": [
            sys.executable, str(Path(codex_notes.__file__)), "--input", "{input}",
            "--output", "{output}", "--skill", "{skill}", "--context", "{context}",
            "--codex", "/synthetic/codex", "--model", "synthetic-model", "--timeout", "1200"]}}
        atomic_json(self.config, self.settings)
        self.decide(name="Jamie Taylor")
        self.calls = []

    def generate(self, input_path, output_path, **kwargs):
        self.calls.append((input_path, output_path, kwargs))
        self.assertEqual(json.loads(Path(input_path).read_text()), self.transcript)
        self.assertNotEqual(Path(input_path), self.recording / "transcript.json")
        self.assertEqual(json.loads(Path(kwargs["context_path"]).read_text()), None)
        identity = json.loads(Path(kwargs["identity_path"]).read_text())
        self.assertEqual(identity, self.saved_bundle())
        result = copy.deepcopy(self.notes)
        result["_speaker_identity"] = identity
        result["summary"] = [{"text": "Jamie Taylor will send the agenda.", "evidence": [2]}]
        atomic_json(output_path, result)

    def loaded(self):
        sources = _Sources(self.state)
        try:
            return load_reviewed_notes(sources, self.job, self.transcript, self.notes, self.saved_bundle())
        finally:
            sources.close()

    def test_refresh_uses_copied_inputs_and_publishes_only_bound_local_notes(self):
        with patch("quill_workflow.speaker_notes.codex_notes.run", side_effect=self.generate):
            result = refresh(self.state, self.job, self.config)
        self.assertEqual(result, self.loaded())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2]["timeout"], 900)
        self.assertEqual(self.calls[0][2]["executable"], "/synthetic/codex")
        self.assertEqual(self.calls[0][2]["model"], "synthetic-model")
        self.assertFalse(Path(self.calls[0][0]).exists())
        self.assertIn("Jamie Taylor will send", (self.result / "speakers" / "notes-reviewed.md").read_text())
        self.assertEqual(read_status(self.state, self.job)["state"], "succeeded")
        self.assertIn("Jamie Taylor will send", meeting_detail(self.state, self.job)["notes_text"])
        self.assert_originals_unchanged()

    def test_matching_completed_notes_are_reused_without_duplicate_model_work(self):
        with patch("quill_workflow.speaker_notes.codex_notes.run", side_effect=self.generate):
            first = refresh(self.state, self.job, self.config)
            second = refresh(self.state, self.job, self.config)
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)

    def test_changed_correction_hides_previous_revision_until_new_notes_are_ready(self):
        with patch("quill_workflow.speaker_notes.codex_notes.run", side_effect=self.generate):
            refresh(self.state, self.job, self.config)
        self.decide(name="James Taylor")
        self.assertIsNone(self.loaded())
        self.assert_originals_unchanged()

    def test_failure_preserves_previous_revision_and_stores_bounded_status(self):
        with patch("quill_workflow.speaker_notes.codex_notes.run", side_effect=self.generate):
            refresh(self.state, self.job, self.config)
        self.decide(name="James Taylor")
        paths = [self.result / "speakers" / name for name in ("notes.json", "notes-binding.json")]
        before = [path.read_bytes() for path in paths]
        with patch("quill_workflow.speaker_notes.codex_notes.run", side_effect=WorkflowError("generation failed " * 60)):
            with self.assertRaises(WorkflowError):
                refresh(self.state, self.job, self.config)
        self.assertEqual([path.read_bytes() for path in paths], before)
        status = read_status(self.state, self.job)
        self.assertEqual(status["state"], "failed")
        self.assertLessEqual(len(status["error"]), 500)
        self.assertIsNone(self.loaded())
        self.assert_originals_unchanged()

    def test_changed_correction_during_generation_cannot_publish_stale_names(self):
        def generate_and_change(*args, **kwargs):
            self.generate(*args, **kwargs)
            self.decide(name="James Taylor")

        with patch("quill_workflow.speaker_notes.codex_notes.run", side_effect=generate_and_change):
            with self.assertRaisesRegex(WorkflowError, "changed during"):
                refresh(self.state, self.job, self.config)
        self.assertFalse((self.result / "speakers" / "notes.json").exists())
        self.assertEqual(read_status(self.state, self.job)["state"], "failed")

    def test_cancelled_and_deleted_meeting_cannot_be_recreated_after_model_finishes(self):
        def generate_and_delete(*args, **kwargs):
            self.generate(*args, **kwargs)
            self.db.execute("INSERT INTO local_deletions VALUES (?,?)", (self.job, '{"status":"deleted"}'))
            self.db.commit()
            shutil.rmtree(self.result)

        with patch("quill_workflow.speaker_notes.codex_notes.run", side_effect=generate_and_delete):
            with self.assertRaisesRegex(ValueError, "stopped"):
                refresh(self.state, self.job, self.config)
        self.assertFalse(self.result.exists())
        self.assertEqual((self.recording / "transcript.json").read_bytes(), self.original)

    def test_output_without_exact_correction_bundle_is_rejected(self):
        def wrong_identity(_input, output, **_kwargs):
            atomic_json(output, self.notes)

        with patch("quill_workflow.speaker_notes.codex_notes.run", side_effect=wrong_identity):
            with self.assertRaisesRegex(WorkflowError, "confirmed speaker review"):
                refresh(self.state, self.job, self.config)
        self.assertFalse((self.result / "speakers" / "notes.json").exists())
        self.assertEqual(read_status(self.state, self.job)["state"], "failed")

    def test_corrupted_or_linked_derived_notes_are_not_exposed(self):
        with patch("quill_workflow.speaker_notes.codex_notes.run", side_effect=self.generate):
            refresh(self.state, self.job, self.config)
        output = self.result / "speakers" / "notes.json"
        notes = json.loads(output.read_text())
        notes["summary"][0]["text"] = "Unverified replacement"
        atomic_json(output, notes)
        self.assertIsNone(self.loaded())
        outside = self.root / "external-notes.json"
        output.rename(outside)
        output.symlink_to(outside)
        self.assertIsNone(self.loaded())

    def test_invalid_or_disabled_adapter_never_runs_model(self):
        for change in ({"enabled": False}, {"command": ["arbitrary-command"]},
                       {"command": self.settings["notes"]["command"] + ["--timeout", "30"]}):
            settings = copy.deepcopy(self.settings)
            settings["notes"].update(change)
            atomic_json(self.config, settings)
            with self.subTest(change=change), patch("quill_workflow.speaker_notes.codex_notes.run") as model:
                with self.assertRaises(WorkflowError):
                    refresh(self.state, self.job, self.config)
                model.assert_not_called()
        self.assertFalse((self.result / "speakers" / "notes.json").exists())

    def test_owned_background_process_uses_file_arguments_and_private_lock(self):
        child = Mock(pid=123)
        with patch("quill_workflow.speaker_notes.subprocess.Popen", return_value=child) as launch:
            self.assertIs(start_refresh(self.state, self.job, self.config), child)
        args, kwargs = launch.call_args
        argv = args[0]
        self.assertIn("--lock-fd", argv)
        self.assertEqual(argv[argv.index("--job") + 1], self.job)
        self.assertEqual(argv[argv.index("--config") + 1], str(self.config))
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(len(kwargs["pass_fds"]), 1)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(read_status(self.state, self.job)["state"], "pending")

    def test_active_worker_can_have_only_one_waiting_successor(self):
        with _lock(_lock_path(self.state, self.job)):
            with patch("quill_workflow.speaker_notes.subprocess.Popen", return_value=Mock(pid=123)) as launch:
                start_refresh(self.state, self.job, self.config)
                self.assertIn("--wakeup", launch.call_args.args[0])
            with _lock(_lock_path(self.state, self.job, True)):
                with patch("quill_workflow.speaker_notes.subprocess.Popen") as launch:
                    self.assertIsNone(start_refresh(self.state, self.job, self.config))
                    launch.assert_not_called()

    def test_worker_start_failure_is_visible_and_does_not_leave_pending_status(self):
        with patch("quill_workflow.speaker_notes.subprocess.Popen", side_effect=OSError("synthetic failure")):
            with self.assertRaisesRegex(WorkflowError, "could not be started"):
                start_refresh(self.state, self.job, self.config)
        self.assertEqual(read_status(self.state, self.job)["state"], "failed")
        self.assertFalse((self.result / "speakers" / "notes.json").exists())

    def test_malformed_status_is_not_trusted(self):
        atomic_json(self.result / "speakers" / "notes-status.json", {
            "schema_version": 1, "state": "succeeded", "error": None, "updated": True})
        self.assertIsNone(read_status(self.state, self.job))

    def test_stalled_worker_becomes_retryable_without_read_time_writes(self):
        path = self.result / "speakers" / "notes-status.json"
        for state in ("pending", "running"):
            with self.subTest(state=state):
                atomic_json(path, {"schema_version": 1, "state": state, "error": None, "updated": 100})
                before = path.read_bytes()
                with patch("quill_workflow.speaker_notes.time.time", return_value=120):
                    status = read_status(self.state, self.job)
                self.assertEqual(status["state"], "failed")
                self.assertIn("Retry", status["error"])
                self.assertEqual(path.read_bytes(), before)

    def test_active_or_waiting_worker_and_fresh_start_do_not_look_stalled(self):
        path = self.result / "speakers" / "notes-status.json"
        atomic_json(path, {"schema_version": 1, "state": "running", "error": None, "updated": 100})
        for wakeup in (False, True):
            with self.subTest(wakeup=wakeup), _lock(_lock_path(self.state, self.job, wakeup)):
                with patch("quill_workflow.speaker_notes.time.time", return_value=120):
                    self.assertEqual(read_status(self.state, self.job)["state"], "running")
        with patch("quill_workflow.speaker_notes.time.time", return_value=105):
            self.assertEqual(read_status(self.state, self.job)["state"], "running")

    def test_cancellation_stops_the_owned_model_promptly_and_rejects_its_answer(self):
        executable, started = self.root / "sleeping fake codex", self.root / "model-started"
        executable.write_text("#!" + sys.executable + "\nimport sys, time\nfrom pathlib import Path\n"
            "sys.stdin.read()\n"
            "Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text("
            + repr(json.dumps(self.notes)) + ")\n"
            "Path(" + repr(str(started)) + ").touch()\n"
            "time.sleep(30)\n")
        executable.chmod(0o700)
        source, output, skill = self.recording / "transcript.json", self.root / "cancelled-notes.json", self.root / "SKILL.md"
        skill.write_text("Summarize supplied evidence only.")
        original = source.read_bytes()
        processes = []
        real_popen = subprocess.Popen

        def capture(argv, **kwargs):
            process = real_popen(argv, **kwargs)
            if argv[0] == str(executable):
                processes.append(process)
            return process

        began = time.monotonic()
        with patch("quill_workflow.codex_notes.subprocess.Popen", side_effect=capture):
            with self.assertRaisesRegex(WorkflowError, "cancelled"):
                codex_notes.run(source, output, skill, str(executable), timeout=30,
                    cancelled=lambda: started.exists() and time.monotonic() - began >= 0.1)
        self.assertLess(time.monotonic() - began, 3.5)
        self.assertTrue(started.exists())
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        self.assertEqual(processes[0].wait(timeout=0), processes[0].returncode)
        self.assertFalse(output.exists())
        self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
