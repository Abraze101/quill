"""Synthetic recovery and visibility checks. No media, models or cloud calls."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.pipeline import Queue, WorkflowError, NeedsConfiguration, atomic_json
from quill_workflow.queue_status import compact_status, meetings, retry_revision
from quill_workflow.routing import RoutingReviews
from quill_workflow.worker_supervision import (BACKOFF, MAX_RESTARTS, Supervisor, health,
                                              reset_recovery, supervise, worker_event)

spec = importlib.util.spec_from_file_location("health_workflow", ROOT / "workflow.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


class Child:
    def __init__(self, pid):
        self.pid, self.code = pid, None

    def poll(self):
        return self.code


class WorkerHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = self.root / "recordings/synthetic"
        self.session.mkdir(parents=True)
        for name in ("mic.caf", "system.caf"):
            (self.session / name).write_bytes(b"synthetic fixture")
        atomic_json(self.session / "meta.json", {"started": "2026-01-01T10:00:00Z",
                    "ended": "2026-01-01T10:01:00Z", "files": {"mic": "mic.caf", "system": "system.caf"}})
        atomic_json(self.session / "transcript.json", {"segments": [{"text": "Synthetic meeting"}]})
        self.config = {"recordings_root": str(self.session.parent),
                       "notes": {"enabled": True, "command": ["synthetic-only"]}}
        self.queue = Queue(self.root / "state")
        self.job = self.queue.enqueue(self.session, self.config)
        self.config_path = self.root / "config.json"
        atomic_json(self.config_path, self.config)
        self.now, self.children = 100.0, []

        def launch(*args):
            child = Child(10000 + len(self.children))
            self.children.append(child)
            return child
        self.supervisor = Supervisor(self.queue, self.config_path, launch, lambda: self.now)

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def set_task(self, status, stage="notes", attempts=0, error=None, receipt=None):
        with self.queue.db:
            self.queue.db.execute("UPDATE tasks SET status=?,attempts=?,error=?,receipt=? WHERE job=? AND stage=?",
                                  (status, attempts, error, receipt, self.job, stage))

    def test_actual_lock_protects_long_connector_despite_old_status(self):
        self.set_task("running", attempts=1)
        atomic_json(self.queue.state / "status.json", {"updated": 0})
        with (self.queue.state / "worker.lock").open("a") as active:
            fcntl.flock(active, fcntl.LOCK_EX)
            for _ in range(5):
                self.now += 10000
                self.supervisor.tick()
            self.assertEqual(self.children, [])
            self.assertEqual(health(self.queue.state)["state"], "running")

    def test_template_hold_is_visible_without_becoming_retryable(self):
        self.set_task("succeeded", receipt='{"saved":true}')
        reason = "The saved native template changed; create an explicit template revision"
        self.set_task("held", stage="publish", attempts=1, error=reason)
        before = self.queue.tasks(self.job)
        status = compact_status(self.queue)["processing"]
        self.assertEqual(status["state"], "needs_attention")
        self.assertEqual(status["attention_detail"], reason)
        self.assertTrue(status["alert_revision"])
        self.assertFalse(meetings(self.queue)[0]["retryable"])
        self.assertEqual(self.queue.tasks(self.job), before)

    def test_deliberately_disabled_hold_does_not_generate_failure_alert(self):
        self.set_task("held", error="Stage disabled in current configuration")
        status = compact_status(self.queue)["processing"]
        self.assertIsNone(status["alert_revision"])
        self.assertFalse(meetings(self.queue)[0]["retryable"])

    def test_empty_lock_file_does_not_block_work(self):
        (self.queue.state / "worker.lock").touch()
        self.supervisor.tick()
        self.assertEqual(len(self.children), 1)
        self.supervisor.tick()
        self.assertEqual(len(self.children), 1)

    def test_three_crash_restarts_then_attention_and_scoped_retry(self):
        self.supervisor.tick()
        for failure in range(MAX_RESTARTS + 1):
            self.set_task("running", attempts=failure + 1)
            worker_event(self.queue.state, "running")
            self.children[-1].code = -9
            self.supervisor.tick()
            if failure < MAX_RESTARTS:
                self.assertEqual(health(self.queue.state)["state"], "recovering")
                before = len(self.children)
                self.supervisor.tick()
                self.assertEqual(len(self.children), before)
                self.now += BACKOFF[failure]
                self.supervisor.tick()
        self.assertEqual(len(self.children), MAX_RESTARTS + 1)
        self.assertEqual(health(self.queue.state)["state"], "needs_attention")
        self.assertEqual(next(task for task in self.queue.tasks(self.job) if task["stage"] == "notes")["status"], "retry")
        self.now += 10000
        self.supervisor.tick()
        self.assertEqual(len(self.children), MAX_RESTARTS + 1)
        with patch.object(workflow, "start_worker") as launch:
            workflow.retry_meeting(self.job, retry_revision(self.queue.tasks(self.job)), self.config_path, self.queue.state)
            launch.assert_called_once()
        self.assertEqual(health(self.queue.state)["state"], "idle")

    def test_immediate_child_crash_without_event_is_bounded(self):
        self.supervisor.tick()
        for index in range(4):
            self.children[-1].code = 1
            self.supervisor.tick()
            self.now += 100
            self.supervisor.tick()
        self.assertEqual(len(self.children), 4)
        self.assertEqual(health(self.queue.state)["state"], "needs_attention")

    def test_held_work_does_not_launch_and_success_resets_crash_budget(self):
        self.set_task("held")
        worker_event(self.queue.state, "failed", "Synthetic fault")
        self.supervisor.tick()
        self.assertEqual(self.children, [])
        worker_event(self.queue.state, "succeeded")
        self.supervisor.tick()
        self.assertEqual(health(self.queue.state)["state"], "idle")

    def test_external_worker_disappearance_uses_released_lock(self):
        self.set_task("running", attempts=1)
        worker_event(self.queue.state, "running")
        self.supervisor.tick()
        self.assertEqual(self.children, [])
        self.supervisor.tick()
        self.assertEqual(health(self.queue.state)["state"], "recovering")
        self.now += 5
        self.supervisor.tick()
        self.assertEqual(len(self.children), 1)

    def test_parent_exit_stops_supervisor_without_terminating_worker(self):
        with patch("quill_workflow.worker_supervision.os.getppid", side_effect=[123, 123, 123, 1]), \
                patch("quill_workflow.worker_supervision.time.sleep"):
            supervise(self.queue, self.config_path, self.supervisor.launch, 123, interval=0.01)
        self.assertEqual(len(self.children), 1)
        self.assertIsNone(self.children[0].poll())

    def test_waiting_supervisor_stops_when_its_own_app_exits(self):
        with (self.queue.state / "supervisor.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch("quill_workflow.worker_supervision.os.getppid", side_effect=[123, 123, 1]), \
                    patch("quill_workflow.worker_supervision.time.sleep"):
                supervise(self.queue, self.config_path, self.supervisor.launch, 123)
        self.assertEqual(self.children, [])

    def test_reopened_app_takes_supervision_after_previous_app_exits(self):
        with (self.queue.state / "supervisor.lock").open("a") as previous:
            fcntl.flock(previous, fcntl.LOCK_EX)
            def finish_previous(_):
                fcntl.flock(previous, fcntl.LOCK_UN)
            with patch("quill_workflow.worker_supervision.os.getppid", side_effect=[123, 123, 123, 123, 1]), \
                    patch("quill_workflow.worker_supervision.time.sleep", side_effect=finish_previous):
                supervise(self.queue, self.config_path, self.supervisor.launch, 123, interval=0.01)
        self.assertEqual(len(self.children), 1)

    def test_status_is_written_before_long_connector_and_no_settings_leak(self):
        def connector(stage, job, settings):
            state = json.loads((self.queue.state / "status.json").read_text())
            self.assertEqual(state["processing"]["title"], "Writing notes")
            self.assertEqual(state["processing"]["state"], "running")
            self.assertEqual(state["active"], {"job": job, "stage": "notes", "title": "synthetic"})
            self.assertNotIn("synthetic-only", json.dumps(state))
            self.assertLess((self.queue.state / "status.json").stat().st_size, 65536)
            return {"job": job, "stage": stage}
        self.queue.run_ready(connector)
        self.assertEqual(json.loads((self.queue.state / "status.json").read_text())["processing"]["state"], "idle")

    def test_attention_revision_is_stable_until_reason_changes(self):
        self.set_task("failed", error="Synthetic offline")
        one = compact_status(self.queue)["processing"]["alert_revision"]
        self.assertTrue(one)
        self.assertEqual(one, compact_status(self.queue)["processing"]["alert_revision"])
        self.set_task("failed", error="Synthetic login expired")
        self.assertNotEqual(one, compact_status(self.queue)["processing"]["alert_revision"])

    def test_running_meeting_does_not_hide_another_meetings_failure_detail(self):
        self.set_task("failed", error="Synthetic login expired")
        with self.queue.db:
            self.queue.db.execute("INSERT INTO jobs VALUES ('other','/synthetic/other',1)")
            self.queue.db.execute("INSERT INTO tasks(job,stage,status,settings) VALUES ('other','notes','running','{}')")
        status = compact_status(self.queue)["processing"]
        self.assertEqual(status["title"], "Writing notes")
        self.assertEqual(status["attention_detail"], "Synthetic login expired")
        self.assertTrue(status["alert_revision"])

    def test_retry_is_exact_revision_bounded_and_preserves_receipts_and_holds(self):
        self.set_task("failed", attempts=6, error="Synthetic offline")
        self.set_task("succeeded", stage="archive", attempts=1, receipt='{"stage":"archive"}')
        before = self.queue.tasks(self.job)
        with self.assertRaises(WorkflowError):
            self.queue.retry_failed(self.job, "stale", self.config)
        self.assertEqual(self.queue.tasks(self.job), before)
        self.assertEqual(self.queue.retry_failed(self.job, retry_revision(before), self.config), ["notes"])
        tasks = {task["stage"]: task for task in self.queue.tasks(self.job)}
        self.assertEqual((tasks["notes"]["status"], tasks["notes"]["attempts"]), ("pending", 0))
        self.assertEqual(tasks["archive"]["receipt"], '{"stage":"archive"}')
        self.assertEqual(tasks["publish"]["status"], "held")
        with self.assertRaises(WorkflowError):
            self.queue.retry_failed(self.job, retry_revision(before), self.config)
        for now in range(0, 7000, 1000):
            self.queue.run_ready(lambda *args: (_ for _ in ()).throw(OSError("Synthetic failure")), now=now)
        tasks = {task["stage"]: task for task in self.queue.tasks(self.job)}
        self.assertEqual((tasks["notes"]["status"], tasks["notes"]["attempts"]), ("failed", 6))

    def test_retry_cannot_enable_disabled_running_safety_or_held_work(self):
        for status in ("held", "safety_held", "running", "succeeded"):
            self.set_task(status)
            with self.subTest(status=status), self.assertRaises(WorkflowError):
                self.queue.retry_failed(self.job, retry_revision(self.queue.tasks(self.job)), self.config)
        self.set_task("failed")
        disabled = dict(self.config, notes={"enabled": False})
        with self.assertRaises(NeedsConfiguration):
            self.queue.retry_failed(self.job, retry_revision(self.queue.tasks(self.job)), disabled)

    def test_skipped_company_is_not_waiting_or_retryable(self):
        reviews = RoutingReviews(self.queue.state)
        try:
            record = reviews.propose(self.job, (self.session / "transcript.json").read_bytes(),
                [{"id": "one", "name": "One", "drive_folder_id": "folder_123456"}],
                {"schema_version": 1, "company_id": "one", "confidence": "high", "reason": "Synthetic",
                 "evidence": [0], "ambiguous": False})
            reviews.skip(self.job, record["revision"])
        finally:
            reviews.close()
        self.set_task("failed")
        summary = meetings(self.queue)[0]
        self.assertEqual((summary["state"], summary["retryable"]), ("idle", False))
        with self.assertRaises(WorkflowError):
            self.queue.retry_failed(self.job, summary["revision"], self.config)

    def test_repeated_crash_at_sixth_attempt_does_not_allow_seventh(self):
        self.set_task("running", attempts=6)
        self.queue.recover()
        self.queue.run_ready(lambda *args: self.fail("Seventh attempt ran"))
        self.assertFalse(self.queue.has_work())
        self.assertEqual(compact_status(self.queue)["processing"]["state"], "needs_attention")

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("Synthetic subprocess did not reach its expected state")

    def supervisor_harness(self, child_source):
        child = self.root / "synthetic-worker.py"
        child.write_text("import sys\nsys.path.insert(0," + repr(str(ROOT)) + ")\n" + child_source)
        harness = self.root / "synthetic-supervisor.py"
        harness.write_text("import os,subprocess,sys\nsys.path.insert(0," + repr(str(ROOT)) + ")\n"
            "from quill_workflow import worker_supervision as s\nfrom quill_workflow.pipeline import Queue\n"
            "s.BACKOFF=(0.03,0.04,0.05)\n"
            "q=Queue(" + repr(str(self.queue.state)) + ")\n"
            "s.supervise(q,'unused',lambda *_: subprocess.Popen([sys.executable,"
            + repr(str(child)) + "]),os.getppid(),interval=0.02)\n")
        return subprocess.Popen([sys.executable, str(harness)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def test_real_worker_exits_are_recovered_three_times_then_stop(self):
        marker = self.root / "launches"
        source = ("import fcntl,os\nfrom pathlib import Path\nfrom quill_workflow.pipeline import Queue\n"
            "from quill_workflow.worker_supervision import worker_event\nq=Queue(" + repr(str(self.queue.state)) + ")\n"
            "lock=(q.state/'worker.lock').open('a')\nfcntl.flock(lock,fcntl.LOCK_EX)\n"
            "with Path(" + repr(str(marker)) + ").open('a') as f: f.write(str(os.getpid())+'\\n')\n"
            "q.db.execute(\"UPDATE tasks SET status='running',attempts=attempts+1 WHERE stage='notes'\")\nq.db.commit()\n"
            "worker_event(q.state,'running')\nos._exit(87)\n")
        process = self.supervisor_harness(source)
        try:
            self.wait_for(lambda: health(self.queue.state)["state"] == "needs_attention")
            self.assertEqual(len(marker.read_text().splitlines()), MAX_RESTARTS + 1)
            time.sleep(0.15)
            self.assertEqual(len(marker.read_text().splitlines()), MAX_RESTARTS + 1)
            self.assertIsNone(process.poll())
            self.assertEqual(next(task for task in self.queue.tasks(self.job) if task["stage"] == "notes")["status"], "retry")
        finally:
            process.terminate()
            _, error = process.communicate(timeout=5)
            self.assertNotIn(b"Traceback", error)

    def test_real_supervisor_takes_over_after_prior_supervisor_releases_lock(self):
        marker = self.root / "new-supervisor-started"
        source = ("from pathlib import Path\nfrom quill_workflow.pipeline import Queue\n"
                  "q=Queue(" + repr(str(self.queue.state)) + ")\n"
                  "q.db.execute(\"UPDATE tasks SET status='held'\")\nq.db.commit()\n"
                  "Path(" + repr(str(marker)) + ").write_text('started')\n")
        with (self.queue.state / "supervisor.lock").open("a") as old:
            fcntl.flock(old, fcntl.LOCK_EX)
            process = self.supervisor_harness(source)
            try:
                time.sleep(0.1)
                self.assertIsNone(process.poll())
                self.assertFalse(marker.exists())
                fcntl.flock(old, fcntl.LOCK_UN)
                self.wait_for(marker.exists)
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                _, error = process.communicate(timeout=5)
                self.assertNotIn(b"Traceback", error)


if __name__ == "__main__":
    unittest.main()
