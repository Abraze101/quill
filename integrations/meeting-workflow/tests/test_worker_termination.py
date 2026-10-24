"""Signal handling tests use private synthetic workers and no real connectors."""
from contextlib import redirect_stdout
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
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

spec = importlib.util.spec_from_file_location("termination_workflow", ROOT / "workflow.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


class WorkerTerminationTests(unittest.TestCase):
    def test_handler_is_scoped_and_repeated_term_does_not_interrupt_cleanup(self):
        previous = signal.getsignal(signal.SIGTERM)
        cleaned = []
        with self.assertRaises(SystemExit) as stopped:
            with workflow.worker_termination():
                handler = signal.getsignal(signal.SIGTERM)
                self.assertTrue(callable(handler))
                try:
                    handler(signal.SIGTERM, None)
                except Exception:
                    self.fail("Termination was swallowable by pipeline retry handling")
                finally:
                    self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
                    os.kill(os.getpid(), signal.SIGTERM)
                    cleaned.append(True)
        self.assertEqual(stopped.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(cleaned, [True])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_normal_worker_exit_restores_handler_and_status_does_not_install_it(self):
        previous = signal.getsignal(signal.SIGTERM)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            atomic_json(config, {})
            args = ["--config", str(config), "--state", str(root / "state")]
            self.assertEqual(workflow.main(args + ["work", "--once"]), 0)
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
            with patch.object(workflow, "worker_termination", side_effect=AssertionError("Only workers install signals")):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(workflow.main(args + ["status"]), 0)

    def test_sigterm_unwinds_real_transport_and_preserves_recoverable_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "recordings/synthetic"
            session.mkdir(parents=True)
            for name in ("mic.caf", "system.caf"):
                (session / name).write_bytes(b"synthetic fixture")
            atomic_json(session / "meta.json", {"started": "2026-01-01T10:00:00Z",
                        "ended": "2026-01-01T10:01:00Z",
                        "files": {"mic": "mic.caf", "system": "system.caf"}})
            state, config_path = root / "state", root / "config.json"
            config = {"recordings_root": str(session.parent),
                      "archive": {"enabled": True, "command": ["synthetic-only"]}}
            atomic_json(config_path, config)
            queue = Queue(state)
            job = queue.enqueue(session, config)
            ready, child_ready, cleaned = root / "ready.json", root / "child.pid", root / "cleaned"
            fake = root / "fake-codex"
            # A child ignores TERM to exercise the transport's whole-group KILL
            # cleanup too. Neither process uses credentials, network or models.
            child_code = ("import os,signal,time; from pathlib import Path; "
                          "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                          f"Path({str(child_ready)!r}).write_text(str(os.getpid())); time.sleep(60)")
            fake.write_text(f"#!{sys.executable}\n"
                "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
                "for line in sys.stdin:\n"
                "    request = json.loads(line)\n"
                "    method = request.get('method')\n"
                "    if method == 'initialized': continue\n"
                "    if method == 'initialize':\n"
                "        response = {'id':request['id'], 'result':{}}\n"
                "    elif method == 'thread/start':\n"
                "        assert request['params']['ephemeral'] is True\n"
                "        response = {'id':request['id'], 'result':{'thread':{'id':'fake-thread'}}}\n"
                "    elif method == 'mcpServer/tool/call':\n"
                "        assert request['params'] == {'threadId':'fake-thread', 'server':'codex_apps', "
                "'tool':'google_drive.get_file_metadata', 'arguments':{'fileId':'synthetic-folder', 'fields':'id,name'}}\n"
                f"        child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
                f"        while not Path({str(child_ready)!r}).exists(): time.sleep(0.01)\n"
                f"        Path({str(ready)!r}).write_text(json.dumps({{'pid':os.getpid(), 'child':child.pid}}))\n"
                "        time.sleep(60)\n"
                "        break\n"
                "    else:\n"
                "        raise AssertionError('Unexpected RPC method: ' + str(method))\n"
                "    print(json.dumps(response), flush=True)\n")
            fake.chmod(0o700)
            harness = root / "worker.py"
            harness.write_text(
                "import fcntl, importlib.util, sys\nfrom pathlib import Path\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "from quill_workflow.codex_drive_transport import CodexDriveTransport\n"
                f"spec = importlib.util.spec_from_file_location('workflow_under_test', {str(ROOT / 'workflow.py')!r})\n"
                "workflow = importlib.util.module_from_spec(spec); spec.loader.exec_module(workflow)\n"
                "class BlockingRunner:\n"
                "    def __init__(self, state, *_, **__): self.state = state\n"
                "    def __call__(self, *_):\n"
                "        with (self.state / 'nested.lock').open('a') as lock:\n"
                "            fcntl.flock(lock, fcntl.LOCK_EX)\n"
                "            try:\n"
                f"                CodexDriveTransport({str(fake)!r}, timeout=30).invoke('get_file_metadata', "
                "{'fileId':'synthetic-folder', 'fields':'id,name'})\n"
                "            finally:\n"
                f"                Path({str(cleaned)!r}).write_text('cleanup ran')\n"
                "workflow.ConnectorRunner = BlockingRunner\n"
                f"raise SystemExit(workflow.main(['--config',{str(config_path)!r},'--state',{str(state)!r},'work']))\n")
            process = subprocess.Popen([sys.executable, str(harness)], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            pids = {}
            try:
                deadline = time.monotonic() + 8
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not ready.exists():
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=5)
                    self.fail("Synthetic transport did not become ready: " + stderr.decode(errors="replace"))
                pids = json.loads(ready.read_text())
                task = next(task for task in queue.tasks(job) if task["stage"] == "archive")
                self.assertEqual((task["status"], task["attempts"]), ("running", 1))
                process.terminate()
                _, stderr = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 143, stderr.decode(errors="replace"))
                self.assertEqual(cleaned.read_text(), "cleanup ran")
                for pid in pids.values():
                    deadline = time.monotonic() + 3
                    while self.process_running(pid) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertFalse(self.process_running(pid), "Transport process survived worker termination")
                for name in ("worker.lock", "nested.lock"):
                    with (state / name).open("a") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                task = next(task for task in queue.tasks(job) if task["stage"] == "archive")
                self.assertEqual((task["status"], task["attempts"], task["receipt"]), ("running", 1, None))
                self.assertFalse((state / "results" / job / "archive-receipt.json").exists())
                queue.recover(now=100)
                task = next(task for task in queue.tasks(job) if task["stage"] == "archive")
                self.assertEqual((task["status"], task["attempts"], task["next_attempt"]), ("retry", 1, 100))
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                if pids:
                    try:
                        os.killpg(pids["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                for stream in (process.stdout, process.stderr):
                    stream.close()
                queue.close()

    @staticmethod
    def process_running(pid):
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


if __name__ == "__main__":
    unittest.main()
