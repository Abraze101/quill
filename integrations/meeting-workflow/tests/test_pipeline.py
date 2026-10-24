import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.pipeline import (ConnectorRunner, NeedsConfiguration, Queue, RecoverableUploadError,
                                     WorkflowError, atomic_json, render_notes)

spec = importlib.util.spec_from_file_location("native_host", ROOT / "native-host.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def receipt(stage, job, settings):
    return {"job": job, "stage": stage, "idempotency_key": job + ":" + stage}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.recordings = self.root / "recordings"
        self.session = self.recordings / "synthetic-session"
        self.session.mkdir(parents=True)
        # These bytes test transport only; no audio is captured or processed.
        (self.session / "mic.caf").write_bytes(b"synthetic microphone test fixture")
        (self.session / "system.caf").write_bytes(b"synthetic system test fixture")
        atomic_json(self.session / "meta.json", {"started": "2026-01-01T10:00:00Z",
                    "ended": "2026-01-01T10:01:00Z", "files": {"mic": "mic.caf", "system": "system.caf"}})
        self.config = {"recordings_root": str(self.recordings)}
        self.queue = Queue(self.root / "state")

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def enable(self, stage):
        self.config[stage] = {"enabled": True, "command": ["test-connector", "{input}", "{output}"]}

    def test_defaults_hold_every_external_stage(self):
        job = self.queue.enqueue(self.session, self.config)
        self.assertTrue(all(task["status"] == "held" for task in self.queue.tasks(job)))
        self.assertEqual(self.queue.run_ready(lambda *args: self.fail("External action ran")), 0)

    def test_duplicate_hook_is_idempotent_and_originals_unchanged(self):
        before = {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in self.session.iterdir()}
        first = self.queue.enqueue(self.session, self.config)
        self.assertEqual(self.queue.enqueue(self.session, self.config), first)
        self.assertEqual(len(self.queue.tasks()), 3)
        after = {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in self.session.iterdir()}
        self.assertEqual(before, after)

    def test_active_recordings_outside_roots_and_symlinks_are_rejected(self):
        metadata = json.loads((self.session / "meta.json").read_text())
        del metadata["ended"]
        atomic_json(self.session / "meta.json", metadata)
        with self.assertRaises(WorkflowError):
            self.queue.enqueue(self.session, self.config)
        metadata["ended"] = "2026-01-01T10:01:00Z"
        atomic_json(self.session / "meta.json", metadata)
        with self.assertRaises(WorkflowError):
            self.queue.enqueue(self.session, {"recordings_root": str(self.root)})
        link = self.recordings / "link"
        link.symlink_to(self.session, target_is_directory=True)
        with self.assertRaises(WorkflowError):
            self.queue.enqueue(link, self.config)

    def test_linked_tracks_and_path_traversal_are_rejected(self):
        (self.session / "mic.caf").unlink()
        (self.session / "mic.caf").symlink_to(self.session / "system.caf")
        with self.assertRaises(WorkflowError):
            self.queue.enqueue(self.session, self.config)
        metadata = json.loads((self.session / "meta.json").read_text())
        metadata["files"]["system"] = "../../private.txt"
        atomic_json(self.session / "meta.json", metadata)
        with self.assertRaises(WorkflowError):
            self.queue.enqueue(self.session, self.config)

    def test_retry_waits_for_backoff_and_never_repeats_success(self):
        self.enable("archive")
        job = self.queue.enqueue(self.session, self.config)
        calls = []
        def flaky(stage, job, settings):
            calls.append(stage)
            if len(calls) == 1:
                raise OSError("Synthetic offline state")
            return receipt(stage, job, settings)
        self.queue.run_ready(flaky, now=100)
        self.assertEqual(self.queue.tasks(job)[0]["status"], "retry")
        self.queue.run_ready(flaky, now=129)
        self.assertEqual(len(calls), 1)
        self.queue.run_ready(flaky, now=130)
        self.queue.run_ready(flaky, now=500)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.queue.tasks(job)[0]["status"], "succeeded")

    def test_retries_stop_after_six_attempts(self):
        self.enable("archive")
        job = self.queue.enqueue(self.session, self.config)
        def fail(*args):
            raise OSError("Synthetic failure")
        for now in range(0, 7000, 1000):
            self.queue.run_ready(fail, now=now)
        archive = next(task for task in self.queue.tasks(job) if task["stage"] == "archive")
        self.assertEqual((archive["status"], archive["attempts"]), ("failed", 6))
        self.assertFalse(self.queue.has_work())

    def test_reserved_upload_keeps_retrying_after_six_and_completes_after_restart(self):
        self.enable("archive")
        job = self.queue.enqueue(self.session, self.config)
        settings = self.queue.tasks(job)[0]["settings"]
        calls = []
        def offline(*args):
            calls.append(args)
            raise RecoverableUploadError("Reserved Drive upload temporarily unavailable")
        now = 100
        for attempt, delay in enumerate((30, 60, 120, 240, 480, 960, 1920, 3600), 1):
            self.queue.run_ready(offline, now=now)
            archive = self.queue.tasks(job)[0]
            self.assertEqual((archive["status"], archive["attempts"], archive["next_attempt"]),
                             ("retry", attempt, now + delay))
            self.assertEqual(archive["settings"], settings)
            self.assertIsNone(archive["receipt"])
            self.assertTrue(self.queue.has_work())
            now = archive["next_attempt"]
        self.assertEqual(len(calls), 8)

        # Waiting retries survive a normal app/worker restart without resetting
        # the saved attempt counter, destination, or backoff.
        self.queue.close()
        self.queue = Queue(self.root / "state")
        self.queue.recover(now=now - 1)
        self.queue.run_ready(lambda *args: self.fail("Retry ran before its deadline"), now=now - 1)
        self.queue.run_ready(receipt, now=now)
        archive = self.queue.tasks(job)[0]
        self.assertEqual((archive["status"], archive["attempts"]), ("succeeded", 9))
        self.assertIsNotNone(archive["receipt"])
        self.queue.run_ready(lambda *args: self.fail("Successful upload repeated"), now=now + 10000)
        self.assertFalse(self.queue.has_work())

    def test_reserved_upload_retry_delay_is_capped_for_large_attempt_count(self):
        self.enable("archive")
        job = self.queue.enqueue(self.session, self.config)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET attempts=1000000 WHERE job=? AND stage='archive'", (job,))
        def offline(*args):
            raise RecoverableUploadError("Reserved upload still offline")
        self.queue.run_ready(offline, now=100)
        archive = self.queue.tasks(job)[0]
        self.assertEqual((archive["status"], archive["attempts"], archive["next_attempt"]),
                         ("retry", 1000001, 3700))

    def test_reserved_upload_retry_does_not_override_authorization_hold(self):
        self.enable("archive")
        job = self.queue.enqueue(self.session, self.config)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='retry',attempts=8 WHERE job=? AND stage='archive'", (job,))
        def reconnect(*args):
            raise NeedsConfiguration("Google Drive upload authorization expired")
        self.queue.run_ready(reconnect, now=100)
        archive = self.queue.tasks(job)[0]
        self.assertEqual((archive["status"], archive["attempts"]), ("held", 9))
        self.assertIsNone(archive["receipt"])
        self.queue.run_ready(lambda *args: self.fail("Authorization hold retried"), now=10000)
        self.assertFalse(self.queue.has_work())

    def test_interrupted_worker_recovery_keeps_its_existing_attempt_cap(self):
        self.enable("archive")
        job = self.queue.enqueue(self.session, self.config)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='running',attempts=6 WHERE job=? AND stage='archive'", (job,))
        self.queue.recover(now=100)
        archive = self.queue.tasks(job)[0]
        self.assertEqual((archive["status"], archive["attempts"], archive["error"]),
                         ("failed", 6, "Worker interrupted"))
        self.queue.run_ready(lambda *args: self.fail("Exhausted worker restarted"), now=10000)

    def test_reopen_recovers_interrupted_jobs(self):
        self.enable("archive")
        job = self.queue.enqueue(self.session, self.config)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='running' WHERE stage='archive'")
        self.queue.close()
        self.queue = Queue(self.root / "state")
        self.queue.recover(now=100)
        self.queue.run_ready(receipt, now=100)
        self.assertEqual(self.queue.tasks(job)[0]["status"], "succeeded")

    def test_config_change_does_not_release_existing_held_jobs(self):
        job = self.queue.enqueue(self.session, self.config)
        self.enable("archive")
        self.assertEqual(self.queue.run_ready(receipt), 0)
        self.queue.release(job, "archive", self.config)
        self.queue.run_ready(receipt)
        with self.assertRaises(WorkflowError):
            self.queue.release(job, "archive", self.config)

    def test_destination_settings_are_pinned_to_each_job(self):
        self.config["archive"] = {"enabled": True, "type": "rclone", "remote": "chosen", "folder": "first"}
        self.queue.enqueue(self.session, self.config)
        self.config["archive"]["folder"] = "different"
        seen = []
        def observe(stage, job, settings):
            seen.append(settings["folder"])
            return receipt(stage, job, settings)
        self.queue.run_ready(observe)
        self.assertEqual(seen, ["first"])

    def test_disabling_a_stage_holds_previously_approved_queued_work(self):
        self.enable("archive")
        job = self.queue.enqueue(self.session, self.config)
        self.queue.run_ready(lambda *args: self.fail("Disabled stage ran"), enabled={"archive": False})
        self.assertEqual(self.queue.tasks(job)[0]["status"], "held")
        self.queue.run_ready(lambda *args: self.fail("Old held job silently resumed"), enabled={"archive": True})
        self.assertEqual(self.queue.tasks(job)[0]["attempts"], 0)

    def test_disabling_during_retry_backoff_stops_worker_until_explicit_release(self):
        self.enable("archive")
        job = self.queue.enqueue(self.session, self.config)
        def offline(*args):
            raise OSError("Synthetic offline state")
        self.queue.run_ready(offline, now=100)
        retry = self.queue.tasks(job)[0]
        self.assertEqual((retry["status"], retry["attempts"], retry["next_attempt"]), ("retry", 1, 130))
        self.assertTrue(self.queue.has_work())

        self.queue.run_ready(lambda *args: self.fail("Disabled retry ran"),
                             now=101, enabled={"archive": False})
        held = self.queue.tasks(job)[0]
        self.assertEqual((held["status"], held["attempts"], held["next_attempt"]), ("held", 1, 130))
        self.assertFalse(self.queue.has_work())
        self.queue.run_ready(lambda *args: self.fail("Held retry resumed without release"),
                             now=1000, enabled={"archive": True})
        self.assertFalse(self.queue.has_work())
        self.queue.release(job, "archive", self.config)
        self.queue.run_ready(receipt, now=1000, enabled={"archive": True})
        self.assertEqual(self.queue.tasks(job)[0]["status"], "succeeded")

        # Disabling only withdraws work that has not started. It must neither
        # rewrite a receipt nor pretend an active connector has been stopped.
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='running' WHERE job=? AND stage='notes'", (job,))
        before = self.queue.tasks(job)
        self.queue.run_ready(lambda *args: self.fail("Disabled stage ran"), now=1001, enabled={})
        self.assertEqual(self.queue.tasks(job), before)

    def test_publishing_waits_for_notes_and_does_not_spin_on_held_dependency(self):
        self.enable("publish")
        job = self.queue.enqueue(self.session, self.config)
        self.assertFalse(self.queue.has_work())
        self.assertEqual(self.queue.run_ready(receipt), 0)
        self.enable("notes")
        self.queue.release(job, "notes", self.config)
        self.assertTrue(self.queue.has_work())
        self.queue.run_ready(receipt)
        status = {t["stage"]: t["status"] for t in self.queue.tasks(job)}
        self.assertEqual(status["notes"], "succeeded")
        self.assertEqual(status["publish"], "succeeded")

    def test_missing_executable_holds_job_without_claiming_upload(self):
        self.config["archive"] = {"enabled": True, "type": "rclone", "remote": "chosen", "folder": "meetings",
                                  "executable": str(self.root / "not-installed")}
        job = self.queue.enqueue(self.session, self.config)
        self.queue.run_ready(ConnectorRunner(self.queue.state, ROOT / "skills/call-summary/SKILL.md"))
        self.assertEqual(self.queue.tasks(job)[0]["status"], "held")
        self.assertIsNone(self.queue.tasks(job)[0]["receipt"])

    def test_missing_transcript_never_runs_notes(self):
        self.enable("notes")
        job = self.queue.enqueue(self.session, self.config)
        runner = ConnectorRunner(self.queue.state, ROOT / "skills/call-summary/SKILL.md")
        runner.execute = lambda *args: self.fail("Processing ran without a transcript")
        self.queue.run_ready(runner)
        task = next(t for t in self.queue.tasks(job) if t["stage"] == "notes")
        self.assertEqual(task["status"], "held")

    def test_explicit_transcript_attachment_keeps_originals_and_archive_receipt(self):
        self.enable("archive")
        job = self.queue.enqueue(self.session, self.config)
        self.queue.run_ready(receipt)
        archive_before = next(t for t in self.queue.tasks(job) if t["stage"] == "archive")
        transcript = {"segments": [{"speaker": "me", "start_ms": 0, "text": "A synthetic test."}]}
        atomic_json(self.session / "transcript.json", transcript)
        self.queue.attach_transcript(job, self.session / "transcript.json")
        self.assertEqual(len(self.queue.tasks()), 3)
        self.assertEqual(next(t for t in self.queue.tasks(job) if t["stage"] == "archive"), archive_before)
        self.assertEqual(next(t for t in self.queue.tasks(job) if t["stage"] == "notes")["status"], "held")
        self.assertEqual(json.loads((self.session / "transcript.json").read_text()), transcript)
        self.assertFalse((self.queue.state / "recordings" / job / "transcript.json").exists())
        self.queue.run_ready(lambda *args: self.fail("Attachment released a job"))

    def test_transcript_attachment_rejects_wrong_session_and_changed_recordings(self):
        job = self.queue.enqueue(self.session, self.config)
        transcript = {"segments": [{"speaker": "me", "start_ms": 0, "text": "Synthetic."}]}
        atomic_json(self.root / "unrelated.json", transcript)
        with self.assertRaises(WorkflowError):
            self.queue.attach_transcript(job, self.root / "unrelated.json")
        atomic_json(self.session / "transcript.json", transcript)
        (self.session / "mic.caf").write_bytes(b"Changed recording")
        with self.assertRaises(WorkflowError):
            self.queue.attach_transcript(job, self.session / "transcript.json")

    def test_notes_can_use_explicitly_attached_transcript_without_archiving_twice(self):
        job = self.queue.enqueue(self.session, self.config)
        atomic_json(self.session / "transcript.json", {"segments": [{"speaker": "me", "start_ms": 0, "text": "I will send it."}]})
        self.queue.attach_transcript(job, self.session / "transcript.json")
        runner_script = self.root / "fake_attached_notes.py"
        runner_script.write_text('import json,sys\nfrom pathlib import Path\n'
                                'assert json.loads(Path(sys.argv[1]).read_text())["segments"]\n'
                                'Path(sys.argv[2]).write_text(json.dumps({"schema_version":1,"actions":[{"text":"Send it","owner":"me","due":None,"evidence":[0]}]}))\n')
        self.config["notes"] = {"enabled": True, "command": [sys.executable, str(runner_script), "{input}", "{output}"]}
        self.queue.release(job, "notes", self.config)
        self.queue.run_ready(ConnectorRunner(self.queue.state, ROOT / "skills/call-summary/SKILL.md"))
        self.assertEqual(next(t for t in self.queue.tasks(job) if t["stage"] == "notes")["status"], "succeeded")
        self.assertEqual(next(t for t in self.queue.tasks(job) if t["stage"] == "archive")["attempts"], 0)
        self.assertTrue((self.queue.state / "results" / job / "notes/source-transcript.json").is_file())

    def test_real_subprocess_notes_contract_and_recovery(self):
        transcript = {"segments": [{"speaker": "me", "start_ms": 1200, "text": "I will send the agenda."}]}
        atomic_json(self.session / "transcript.json", transcript)
        script = self.root / "fake_notes.py"
        script.write_text('import json,sys\nfrom pathlib import Path\n'
                          'Path(sys.argv[1]).write_text(json.dumps({"schema_version":1,"actions":[{"text":"Send the agenda","owner":"me","due":None,"evidence":[0]}]}))\n')
        self.config["notes"] = {"enabled": True, "command": [sys.executable, str(script), "{output}"]}
        job = self.queue.enqueue(self.session, self.config)
        runner = ConnectorRunner(self.queue.state, ROOT / "skills/call-summary/SKILL.md")
        self.queue.run_ready(runner)
        markdown = (self.queue.state / "results" / job / "notes/notes.md").read_text()
        self.assertIn("[me] Send the agenda", markdown)
        self.assertNotIn("Due:", markdown)
        self.assertNotIn("00:01, segment 0", markdown)
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status='running' WHERE stage='notes'")
        self.queue.recover()
        runner.execute = lambda *args: self.fail("Completed skill reran after crash")
        self.queue.run_ready(runner)
        self.assertEqual(next(t for t in self.queue.tasks(job) if t["stage"] == "notes")["status"], "succeeded")

    def test_notes_need_valid_evidence_and_explicit_unresolved_fields(self):
        transcript = {"segments": [{"speaker": "them", "start_ms": 0, "text": "Someone should send it."}]}
        notes = {"schema_version": 1, "actions": [{"text": "Send it", "owner": None, "due": None, "evidence": [0]}]}
        self.assertIn("[Unassigned] Send it", render_notes(notes, transcript))
        notes["actions"][0]["evidence"] = [9]
        with self.assertRaises(WorkflowError):
            render_notes(notes, transcript)
        notes["actions"][0]["evidence"] = [0]
        del notes["actions"][0]["owner"]
        with self.assertRaisesRegex(WorkflowError, "owner and due"):
            render_notes(notes, transcript)

    def test_clean_notes_keep_findings_actions_and_follow_up_without_source_clutter(self):
        transcript = {"segments": [{"speaker": "me", "start_ms": 1200,
                                    "text": "I will send the agenda on Friday."}]}
        notes = {"schema_version": 1,
                 "summary": [{"text": "The agenda will be sent Friday.", "evidence": [0]}],
                 "actions": [{"text": "Send the agenda", "owner": "me", "due": "Friday", "evidence": [0]}],
                 "follow_up_draft": {"subject": "Our next steps", "body": "I will send the agenda on Friday.",
                                     "evidence": [0]}}
        before = json.dumps(notes, sort_keys=True)
        self.assertEqual(render_notes(notes, transcript),
                         "# Meeting notes\n\n## Summary\n\nThe agenda will be sent Friday.\n\n"
                         "## Decisions\n\nNo decisions recorded.\n\n"
                         "## Next steps\n\n- [me] Send the agenda Due: Friday\n\n"
                         "## Details\n\n- **Follow-up draft:** Our next steps\n\n"
                         "I will send the agenda on Friday.\n")
        self.assertEqual(json.dumps(notes, sort_keys=True), before)
        legacy = render_notes(notes, transcript, include_sources=True)
        self.assertIn("- The agenda will be sent Friday. (00:01, segment 0)", legacy)
        self.assertIn("due: Friday (00:01, segment 0)", legacy)
        self.assertTrue(legacy.endswith("Source: 00:01, segment 0\n"))

        for item in (notes["summary"][0], notes["actions"][0], notes["follow_up_draft"]):
            with self.subTest(item=item):
                item["evidence"] = [99]
                with self.assertRaisesRegex(WorkflowError, "Invalid transcript segment reference"):
                    render_notes(notes, transcript)
                item["evidence"] = [0]
        transcript["segments"][0]["start_ms"] = -1
        with self.assertRaisesRegex(WorkflowError, "valid timestamp"):
            render_notes(notes, transcript)

    def test_complete_cli_starts_worker_and_duplicate_completion_runs_once(self):
        connector = self.root / "local connector.py"
        count = self.root / "connector-count"
        connector.write_text(
            'import json,sys\nfrom pathlib import Path\n'
            'counter=Path(sys.argv[2])\n'
            'counter.write_text(str(int(counter.read_text())+1) if counter.exists() else "1")\n'
            'Path(sys.argv[1]).write_text(json.dumps({"job":sys.argv[3],"stage":"archive",'
            '"idempotency_key":sys.argv[4],"destination":"local-test://verified"}))\n')
        self.config["archive"] = {"enabled": True, "command": [sys.executable, str(connector),
                                  "{output}", str(count), "{job}", "{idempotency_key}"]}
        config = self.root / "workflow.json"
        atomic_json(config, self.config)
        argv = [sys.executable, str(ROOT / "workflow.py"), "--state", str(self.queue.state),
                "--config", str(config), "complete", str(self.session)]
        first = subprocess.run(argv, check=True, text=True, capture_output=True, timeout=5).stdout.strip()
        second = subprocess.run(argv, check=True, text=True, capture_output=True, timeout=5).stdout.strip()
        self.assertEqual(first, second)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            archive = next(t for t in self.queue.tasks(first) if t["stage"] == "archive")
            if archive["status"] == "succeeded":
                break
            time.sleep(0.05)
        self.assertEqual(archive["status"], "succeeded")
        self.assertEqual(count.read_text(), "1")
        self.assertTrue(all(t["status"] == "held" for t in self.queue.tasks(first) if t["stage"] != "archive"))


class NativeHostTests(unittest.TestCase):
    def test_native_window_geometry_is_optional_bounded_and_content_free(self):
        with tempfile.TemporaryDirectory() as directory:
            geometry = {"id": 7, "left": -1200, "top": 30, "width": 1200, "height": 800}
            message = {"source": "tab-1", "meeting": "call", "state": "joined", "provider": "google-meet",
                       "browserWindow": dict(geometry, title="Private title", url="https://example.invalid")}
            native.accept(message, directory)
            stored = json.loads(next(Path(directory).glob("*.json")).read_text())
            self.assertEqual(stored["browserWindow"], geometry)
            self.assertNotIn("Private title", json.dumps(stored))
            self.assertNotIn("https://", json.dumps(stored))
            message["state"] = "left"
            native.accept(message, directory)
            self.assertNotIn("browserWindow", json.loads(next(Path(directory).glob("*.json")).read_text()))

    def test_native_rejects_invalid_window_geometry_without_writing_a_target(self):
        geometry = {"id": 7, "left": 0, "top": 30, "width": 1200, "height": 800}
        invalid = [None, [], {}, dict(geometry, id=0), dict(geometry, id=2147483648),
                   dict(geometry, left=-131073), dict(geometry, top=131073),
                   dict(geometry, width=0), dict(geometry, height=65537),
                   dict(geometry, width=1.5), dict(geometry, width=float("inf")),
                   dict(geometry, width=float("nan")), dict(geometry, width=True),
                   dict(geometry, width="1200")]
        for value in invalid:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                message = {"source": "tab-1", "meeting": "call", "state": "joined", "provider": "google-meet", "browserWindow": value}
                with self.assertRaises(ValueError):
                    native.accept(message, directory)
                self.assertEqual(list(Path(directory).glob("*.json")), [])

    def test_native_protocol_round_trip_and_no_content_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            message = {"source": "test-tab-1", "meeting": "call-1", "state": "joined", "provider": "google-meet",
                       "chat": "This must never be stored", "url": "https://example.invalid/private"}
            payload = json.dumps(message).encode()
            output = io.BytesIO()
            native.serve(io.BytesIO(struct.pack("=I", len(payload)) + payload), output, directory)
            response = output.getvalue()
            self.assertEqual(json.loads(response[4:]), {"accepted": True, "recordingStatus": {"state": "unavailable"}})
            stored = json.loads(next(Path(directory).glob("*.json")).read_text())
            self.assertEqual(set(stored), {"source", "meeting", "state", "provider", "timestamp"})

    def test_native_protocol_rejects_traversal_and_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                native.accept({"source": "../../bad", "meeting": "call", "state": "joined", "provider": "google-meet"}, directory)
            with self.assertRaises(ValueError):
                native.serve(io.BytesIO(struct.pack("=I", 5000)), io.BytesIO(), directory)
            with self.assertRaises(ValueError):
                native.serve(io.BytesIO(struct.pack("=I", 5) + b"{}"), io.BytesIO(), directory)

    def test_rejoin_keeps_both_sessions_end_and_start_signals(self):
        with tempfile.TemporaryDirectory() as directory:
            native.accept({"source": "tab-1", "meeting": "old", "state": "left", "provider": "google-meet"}, directory)
            native.accept({"source": "tab-1", "meeting": "new", "state": "joined", "provider": "google-meet"}, directory)
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
