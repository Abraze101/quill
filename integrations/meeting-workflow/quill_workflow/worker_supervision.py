"""Bounded recovery for the completion worker, tied to the running Quill app."""
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

MAX_RESTARTS = 3
BACKOFF = (5, 15, 60)


def _read(path):
    try:
        if path.is_symlink() or path.stat().st_size > 8192:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def lock_held(state, name="worker.lock"):
    """A live lock, never a timestamp or mere lock-file existence, is evidence."""
    with (Path(state) / name).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def worker_event(state, outcome, error=None):
    from .pipeline import atomic_json
    atomic_json(Path(state) / "worker-event.json", {
        "id": uuid.uuid4().hex, "outcome": outcome, "pid": os.getpid(),
        "updated": time.time(), "error": str(error)[:500] if error else None})


def recovery_state(state):
    saved = _read(Path(state) / "worker-supervision.json")
    return {"failures": max(0, min(100, saved.get("failures", 0)))
            if type(saved.get("failures", 0)) is int else 0,
            "last_event": saved.get("last_event"),
            "retry_at": saved.get("retry_at", 0)
            if type(saved.get("retry_at", 0)) in (int, float) else 0,
            "error": saved.get("error") if isinstance(saved.get("error"), str) else None}


def reset_recovery(state):
    """Only an explicit scoped retry resets an exhausted recovery budget."""
    from .pipeline import atomic_json
    event = _read(Path(state) / "worker-event.json")
    atomic_json(Path(state) / "worker-supervision.json", {
        "failures": 0, "last_event": event.get("id"), "retry_at": 0, "error": None})


def health(state):
    saved = recovery_state(state)
    running = lock_held(state)
    status = ("running" if running else "needs_attention" if saved["failures"] > MAX_RESTARTS
              else "recovering" if saved["failures"] else "idle")
    return {"state": status, "error": saved["error"],
            "restarts": min(saved["failures"], MAX_RESTARTS),
            "retry_at": saved["retry_at"] or None}


class Supervisor:
    def __init__(self, queue, config_path, launch, clock=time.time):
        self.queue, self.config_path, self.launch, self.clock = queue, config_path, launch, clock
        self.child = None
        self.child_event = None

    def _save(self, saved):
        from .pipeline import atomic_json
        atomic_json(self.queue.state / "worker-supervision.json", saved)

    def _failed(self, saved, error, now):
        saved["failures"] += 1
        saved["error"] = str(error)[:500]
        saved["retry_at"] = now + BACKOFF[min(saved["failures"] - 1, len(BACKOFF) - 1)]
        self._save(saved)

    def tick(self):
        now = self.clock()
        saved = recovery_state(self.queue.state)
        event = _read(self.queue.state / "worker-event.json")
        exit_code = self.child.poll() if self.child is not None else None
        if self.child is not None and exit_code is not None:
            child_pid = self.child.pid
            self.child = None
            # SIGKILL and interpreter failures can leave no worker event.
            if exit_code and (event.get("id") == self.child_event or event.get("pid") != child_pid
                              or event.get("outcome") == "running"):
                worker_event(self.queue.state, "failed", "Processing worker exited unexpectedly (" + str(exit_code) + ")")
                event = _read(self.queue.state / "worker-event.json")
        if event.get("id") and event.get("id") != saved["last_event"]:
            saved["last_event"] = event["id"]
            if event.get("outcome") == "failed":
                self._failed(saved, event.get("error") or "Processing worker stopped unexpectedly", now)
            elif event.get("outcome") == "succeeded":
                saved.update(failures=0, retry_at=0, error=None)
                self._save(saved)
            else:
                self._save(saved)
        # Never interrupt or duplicate a healthy long connector. An inherited
        # worker lock also protects cleanup if its original parent has exited.
        if lock_held(self.queue.state) or self.child is not None:
            self.queue.write_status()
            return
        if saved["failures"] > MAX_RESTARTS:
            # Expose interrupted stages as retryable only while owning the
            # released worker lock; never alter a live connector's claim.
            with (self.queue.state / "worker.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                if any(task["status"] == "running" for task in self.queue.tasks()):
                    self.queue.recover(now=now)
            self.queue.write_status()
            return
        if now < saved["retry_at"] or not self.queue.has_work():
            self.queue.write_status()
            return
        # A non-supervised completion hook may have been killed without an
        # exit event. The released real lock proves recovery is safe.
        if event.get("outcome") == "running":
            worker_event(self.queue.state, "failed", "Processing worker disappeared before finishing")
            self.queue.write_status()
            return
        try:
            self.child_event = event.get("id")
            self.child = self.launch(self.config_path, self.queue.state)
        except (OSError, ValueError) as error:
            self._failed(saved, "Could not start processing: " + str(error), now)
        self.queue.write_status()


def supervise(queue, config_path, launch, parent_pid, interval=5):
    """Monitor until the app exits. Leave an active owned worker to finish."""
    if parent_pid <= 1 or os.getppid() != parent_pid:
        raise ValueError("The processing supervisor must be started by the running Quill app")
    with (queue.state / "supervisor.lock").open("a") as lock:
        # A newly reopened app can arrive during its predecessor's final poll.
        # Wait for that ownership handoff while our own app remains alive.
        while os.getppid() == parent_pid:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(interval, 1))
        else:
            return
        supervisor = Supervisor(queue, config_path, launch)
        while os.getppid() == parent_pid:
            supervisor.tick()
            time.sleep(interval)
