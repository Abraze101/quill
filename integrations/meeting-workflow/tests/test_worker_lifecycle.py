"""Synthetic queue and process checks; no models, media devices or cloud calls."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.pipeline import Queue, atomic_json

spec = importlib.util.spec_from_file_location("lifecycle_workflow", ROOT / "workflow.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.session = self.root / "recordings/synthetic"
        self.session.mkdir(parents=True)
        for name in ("mic.caf", "system.caf"):
            (self.session / name).write_bytes(b"synthetic fixture")
        atomic_json(self.session / "meta.json", {
            "started": "2026-01-01T10:00:00Z", "ended": "2026-01-01T10:01:00Z",
            "files": {"mic": "mic.caf", "system": "system.caf"}})
        atomic_json(self.session / "transcript.json", {
            "segments": [{"speaker": "me", "start_ms": 0, "text": "Synthetic fixture."}]})
        self.config = {"recordings_root": str(self.session.parent)}
        self.config_path = self.root / "config.json"
        self.queue = Queue(self.root / "state")
        self.args = ["--config", str(self.config_path), "--state", str(self.queue.state), "work", "--once"]

    def tearDown(self):
        self.queue.close()
        self.temporary.cleanup()

    def enable_stages(self):
        for stage in workflow.WORKFLOW_STAGES:
            self.config[stage] = {"enabled": True, "command": ["synthetic-only"]}
        self.config["route"]["companies"] = [{"id": "one", "name": "One", "drive_folder_id": "folder_123456"}]

    def tasks(self, job):
        return {task["stage"]: task for task in self.queue.tasks(job)}

    def test_worker_reloads_settings_after_notes_and_never_releases_old_holds(self):
        old = self.queue.enqueue(self.session, self.config)
        next_session = self.session.parent / "new-synthetic"
        shutil.copytree(self.session, next_session)
        self.enable_stages()
        job = self.queue.enqueue(next_session, self.config)
        atomic_json(self.config_path, self.config)
        calls = []

        def connector(stage, actual_job, settings):
            self.assertEqual((stage, actual_job), ("notes", job))
            calls.append(stage)
            for setting in workflow.WORKFLOW_STAGES:
                self.config[setting]["enabled"] = False
            atomic_json(self.config_path, self.config)
            return {"job": actual_job, "stage": stage}

        with patch.object(workflow, "ConnectorRunner", return_value=connector):
            workflow.main(self.args)
        self.assertEqual(calls, ["notes"])
        self.assertEqual(self.tasks(job)["notes"]["status"], "succeeded")
        self.assertTrue(all(self.tasks(job)[stage]["status"] == "held"
                            and self.tasks(job)[stage]["attempts"] == 0
                            for stage in ("route", "archive", "publish")))
        self.assertTrue(all(task["status"] == "held" and task["attempts"] == 0
                            for task in self.tasks(old).values()))
        for setting in workflow.WORKFLOW_STAGES:
            self.config[setting]["enabled"] = True
        atomic_json(self.config_path, self.config)
        with patch.object(workflow, "ConnectorRunner", side_effect=lambda *_, **__: lambda *_: self.fail("Held work resumed")):
            workflow.main(self.args)
        self.assertEqual(calls, ["notes"])

    def test_disabling_routing_after_notes_holds_dependent_uploads(self):
        self.enable_stages()
        job = self.queue.enqueue(self.session, self.config)
        atomic_json(self.config_path, self.config)

        def connector(stage, actual_job, settings):
            self.assertEqual(stage, "notes")
            self.config["route"]["enabled"] = False
            atomic_json(self.config_path, self.config)
            return {"job": actual_job, "stage": stage}

        with patch.object(workflow, "ConnectorRunner", return_value=connector):
            workflow.main(self.args)
        tasks = self.tasks(job)
        self.assertEqual(tasks["route"]["error"], "Stage disabled in current configuration")
        for stage in ("archive", "publish"):
            self.assertEqual((tasks[stage]["status"], tasks[stage]["attempts"], tasks[stage]["error"]),
                             ("held", 0, "Required routing is disabled"))

    def test_disabling_uploads_after_route_prevents_both_upload_claims(self):
        self.enable_stages()
        job = self.queue.enqueue(self.session, self.config)
        atomic_json(self.config_path, self.config)
        calls = []

        def connector(stage, actual_job, settings):
            calls.append(stage)
            if stage == "route":
                self.config["archive"]["enabled"] = self.config["publish"]["enabled"] = False
                atomic_json(self.config_path, self.config)
            return {"job": actual_job, "stage": stage}

        with patch.object(workflow, "ConnectorRunner", return_value=connector):
            workflow.main(self.args)
        self.assertEqual(calls, ["notes", "route"])
        for stage in ("archive", "publish"):
            self.assertEqual((self.tasks(job)[stage]["status"], self.tasks(job)[stage]["attempts"]), ("held", 0))

    @staticmethod
    def running(pid):
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="],
                                capture_output=True, text=True, check=False)
        return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")

    def test_one_successor_covers_work_arriving_after_the_previous_workers_last_check(self):
        connector = self.root / "receipt.py"
        connector.write_text("import json,sys\nfrom pathlib import Path\n"
            "Path(sys.argv[1]).write_text(json.dumps({'job':sys.argv[2],'stage':'archive',"
            "'idempotency_key':sys.argv[3],'destination':'synthetic://completed'}))\n")
        self.config["archive"] = {"enabled": True, "command": [sys.executable, str(connector),
                                  "{output}", "{job}", "{idempotency_key}"]}
        atomic_json(self.config_path, self.config)
        workers = []
        with (self.queue.state / "worker.lock").open("a") as previous:
            fcntl.flock(previous, fcntl.LOCK_EX)
            self.assertFalse(self.queue.has_work())  # Previous worker's final empty observation.
            job = self.queue.enqueue(self.session, self.config)
            try:
                first = subprocess.Popen([sys.executable, str(ROOT / "workflow.py"), *self.args],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                workers.append(first)
                wakeup = self.queue.state / "worker-wakeup.lock"
                deadline = time.monotonic() + 5
                waiting = False
                while first.poll() is None and time.monotonic() < deadline:
                    if wakeup.exists():
                        with wakeup.open("a") as elected:
                            try:
                                fcntl.flock(elected, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                waiting = True
                                break
                    time.sleep(0.02)
                self.assertTrue(waiting, "New completion lost its wake-up while the old worker still held the lock")
                # Coalesce another wake-up while the one successor is waiting.
                extra = subprocess.Popen([sys.executable, str(ROOT / "workflow.py"), *self.args],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                workers.append(extra)
                _, error = extra.communicate(timeout=5)
                self.assertEqual(extra.returncode, 0, error.decode())
                self.assertIsNone(first.poll())
                fcntl.flock(previous, fcntl.LOCK_UN)
                _, error = first.communicate(timeout=8)
                self.assertEqual(first.returncode, 0, error.decode())
                self.assertEqual((self.tasks(job)["archive"]["status"], self.tasks(job)["archive"]["attempts"]),
                                 ("succeeded", 1))
            finally:
                for worker in workers:
                    if worker.poll() is None:
                        worker.terminate()
                    worker.communicate(timeout=8)

    def adapter_interruption(self, stage, interrupt):
        marker, child_marker = self.root / "processes.json", self.root / "child.pid"
        child_code = ("import os,signal,time; from pathlib import Path; "
                      "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                      f"Path({str(child_marker)!r}).write_text(str(os.getpid())); time.sleep(60)")
        fake = self.root / "fake-codex"
        fake.write_text(f"#!{sys.executable}\nimport json,os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
            "sys.stdin.read()\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}])\n"
            f"while not Path({str(child_marker)!r}).exists(): time.sleep(0.01)\n"
            f"Path({str(marker)!r}).write_text(json.dumps({{'adapter':os.getppid(),'model':os.getpid(),'child':child.pid,'cwd':os.getcwd()}}))\n"
            "time.sleep(60)\n")
        fake.chmod(0o700)
        adapter = ROOT / "quill_workflow" / ("codex_notes.py" if stage == "notes" else "codex_routing.py")
        command = [sys.executable, str(adapter), "--input", "{input}", "--output", "{output}",
                   "--codex", str(fake), "--timeout", "30"]
        if stage == "notes":
            command.extend(["--skill", "{skill}"])
        else:
            command.extend(["--companies", "{companies}"])
        self.config[stage] = {"enabled": True, "command": command,
                              "timeout_seconds": 30 if interrupt else 2}
        if stage == "route":
            self.config[stage]["companies"] = [{"id": "one", "name": "One", "drive_folder_id": "folder_123456"}]
        atomic_json(self.config_path, self.config)
        job = self.queue.enqueue(self.session, self.config)
        if stage == "route":
            source = self.queue.state / "recordings" / job / "transcript.json"
            atomic_json(self.queue.state / "results" / job / "notes-receipt.json", {
                "job": job, "stage": "notes", "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
            with self.queue.db:
                self.queue.db.execute("UPDATE tasks SET status='succeeded' WHERE job=? AND stage='notes'", (job,))
        worker = subprocess.Popen([sys.executable, str(ROOT / "workflow.py"), *self.args],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        pids = {}
        try:
            deadline = time.monotonic() + 6
            while not marker.exists() and worker.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "Actual adapter did not reach the synthetic model")
            pids = json.loads(marker.read_text())
            self.assertTrue(all(self.running(pids[key]) for key in ("adapter", "model", "child")))
            if interrupt:
                worker.terminate()
            _, error = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 143 if interrupt else 0, error.decode())
            for key in ("adapter", "model", "child"):
                deadline = time.monotonic() + 3
                while self.running(pids[key]) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(self.running(pids[key]), key + " survived the worker's cleanup")
            self.assertFalse(Path(pids["cwd"]).exists(), "Adapter finally did not remove its private model workspace")
            task = self.tasks(job)[stage]
            self.assertEqual((task["status"], task["attempts"]), ("running" if interrupt else "retry", 1))
            self.assertIsNone(task["receipt"])
            self.assertFalse((self.queue.state / "results" / job / (stage + "-receipt.json")).exists())
            with (self.queue.state / "worker.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if interrupt:
                self.queue.recover(now=100)
                self.assertEqual((self.tasks(job)[stage]["status"], self.tasks(job)[stage]["next_attempt"]), ("retry", 100))
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
            for key in ("adapter", "model", "child"):
                if key in pids and self.running(pids[key]):
                    os.kill(pids[key], signal.SIGKILL)

    def test_worker_sigterm_unwinds_actual_notes_adapter(self):
        self.adapter_interruption("notes", True)

    def test_worker_sigterm_unwinds_actual_routing_adapter(self):
        self.adapter_interruption("route", True)

    def test_outer_timeout_unwinds_actual_notes_adapter(self):
        self.adapter_interruption("notes", False)

    def test_outer_timeout_unwinds_actual_routing_adapter(self):
        self.adapter_interruption("route", False)


if __name__ == "__main__":
    unittest.main()
