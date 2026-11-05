"""Refresh local notes after an explicit speaker correction, without filing.

Original notes, transcripts, task receipts and Drive documents stay untouched.
Only source-bound derived notes are exposed; stale or cancelled work is dropped.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quill_workflow import codex_notes
from quill_workflow.local_deletion import _busy, _lock, job_lock, stopped_jobs, ProcessingBusy
from quill_workflow.pipeline import WorkflowError, atomic_json, load_config, read_json, render_notes
from quill_workflow.speaker_corrections import SpeakerCorrections
from quill_workflow.speaker_identity import validate_bundle
from quill_workflow.transcript_sources import transcript_digest, validate_meeting_context


BINDING_KEYS = {"schema_version", "transcript_sha256", "bundle_sha256", "original_notes_sha256", "notes_sha256"}
STATUS_KEYS = {"schema_version", "state", "error", "updated"}


def _job(job):
    if not isinstance(job, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job):
        raise ValueError("Choose a saved meeting")
    return job


def _lock_path(state, job, wakeup=False):
    return Path(state) / ("speaker-notes-" + hashlib.sha256(_job(job).encode()).hexdigest()
                          + ("-wakeup" if wakeup else "") + ".lock")


def _bindings(transcript, original, bundle, notes):
    return {"schema_version": 1, "transcript_sha256": transcript_digest(transcript),
            "bundle_sha256": transcript_digest(bundle), "original_notes_sha256": transcript_digest(original),
            "notes_sha256": transcript_digest(notes)}


def load_reviewed_notes(sources, job, transcript, originalnotes, bundle):
    """Read only a complete derived revision matching all current evidence."""
    try:
        _job(job)
        if job in stopped_jobs(sources.state, job):
            return None
        notes = sources.json(f"results/{job}/speakers/notes.json", 5_000_000)
        binding = sources.json(f"results/{job}/speakers/notes-binding.json", 5000)
        if (not isinstance(notes, dict) or not isinstance(binding, dict) or set(binding) != BINDING_KEYS
                or type(binding.get("schema_version")) is not int or binding["schema_version"] != 1
                or binding != _bindings(transcript, originalnotes, bundle, notes)
                or notes.get("_speaker_identity") != bundle):
            return None
        validate_bundle(bundle, transcript)
        codex_notes.validate_result(notes, transcript, require_title=True)
        render_notes(notes, transcript)
        return notes
    except (OSError, ValueError, TypeError, KeyError, WorkflowError):
        return None


def read_status(state, job):
    from quill_workflow.review_library import _Sources
    try:
        _job(job)
        sources = _Sources(state)
        try:
            value = sources.json(f"results/{job}/speakers/notes-status.json", 5000)
        finally:
            sources.close()
        if (not isinstance(value, dict) or set(value) != STATUS_KEYS
                or type(value.get("schema_version")) is not int or value["schema_version"] != 1
                or value.get("state") not in ("pending", "running", "succeeded", "failed")
                or type(value.get("updated")) not in (int, float) or not math.isfinite(value["updated"])
                or value["updated"] <= 0
                or value.get("error") is not None and (
                    not isinstance(value["error"], str) or len(value["error"]) > 500)):
            return None
        if (value["state"] in ("pending", "running") and time.time() - value["updated"] > 10
                and not _busy(_lock_path(state, job)) and not _busy(_lock_path(state, job, True))):
            return dict(value, state="failed",
                        error="Notes refresh stopped before finishing. Retry with the current speaker names.")
        return value
    except (OSError, ValueError, WorkflowError):
        return None


def _status(store, job, state, error=None):
    directory = store._directory(job)
    error = " ".join(str(error).split())[:500] if error is not None else None
    atomic_json(directory / "notes-status.json", {"schema_version": 1, "state": state,
                "error": error, "updated": time.time()})


def _failure_status(store, job, error):
    try:
        with job_lock(store.state, job):
            store._snapshot(job)  # Never recreate a cancelled or removed meeting.
            _status(store, job, "failed", error)
    except (OSError, ValueError, WorkflowError):
        pass


def _settings(config_path):
    config = load_config(config_path)
    settings = config.get("notes", {})
    command = settings.get("command")
    if (settings.get("enabled") is not True or not isinstance(command, list)
            or not all(isinstance(item, str) for item in command)):
        raise WorkflowError("Configure local Codex notes before refreshing speaker names")
    adapters = [i for i, item in enumerate(command) if Path(item).name == "codex_notes.py"
                or item == "quill_workflow.codex_notes" and i > 0 and command[i - 1] == "-m"]
    if len(adapters) != 1:
        raise WorkflowError("Speaker note refresh requires the configured local Codex notes adapter")
    options = {}
    remaining = command[adapters[0] + 1:]
    allowed = {"--input", "--output", "--skill", "--context", "--codex", "--model", "--timeout",
               "--speaker-context", "--speaker-identity"}
    if len(remaining) % 2:
        raise WorkflowError("The Codex notes configuration needs review")
    for index in range(0, len(remaining), 2):
        key, value = remaining[index:index + 2]
        if key not in allowed or key in options:
            raise WorkflowError("The Codex notes configuration needs review")
        options[key] = value
    skill = options.get("--skill", "{skill}")
    if skill == "{skill}":
        skill = Path(__file__).resolve().parents[1] / "skills/call-summary/SKILL.md"
    else:
        skill = Path(skill).expanduser()
        if not skill.is_absolute():
            raise WorkflowError("Use an absolute Call Summary skill path")
    try:
        timeout = float(options.get("--timeout", 900))
    except ValueError as error:
        raise WorkflowError("The notes timeout needs review") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise WorkflowError("The notes timeout must be positive")
    return {"skill_path": skill, "executable": options.get("--codex", "codex"),
            "model": options.get("--model"), "timeout": min(900, timeout)}


def _visible_context(state, job, original):
    review = (original or {}).get("speaker_review")
    if isinstance(review, dict) and "context" in review:
        return validate_meeting_context(review["context"])
    from quill_workflow.review_library import _Sources
    sources = _Sources(state)
    try:
        metadata = sources.json(f"recordings/{job}/meta.json", 100_000)
    finally:
        sources.close()
    value = metadata.get("meeting_context") if isinstance(metadata, dict) else None
    return validate_meeting_context(value)


def _atomic_text(path, text):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Reviewed notes must be a local file")
    fd, temporary = tempfile.mkstemp(prefix=".speaker-notes-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _worker_lock(state, job, inherited_fd=None, wakeup=False):
    """An inherited private lock belongs only to this spawned worker."""
    if inherited_fd is None:
        with _lock(_lock_path(state, job)):
            yield
        return
    expected = _lock_path(state, job, wakeup)
    info, path_info = os.fstat(inherited_fd), expected.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(path_info.st_mode)
            or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
            or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1):
        raise WorkflowError("The speaker notes worker lock is invalid")
    try:
        if wakeup:
            # One successor waits; later requests are covered by its fresh snapshot.
            path = _lock_path(state, job)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "a") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.getuid() or info.st_mode & 0o077):
                    raise WorkflowError("The speaker notes worker lock is invalid")
                fcntl.flock(handle, fcntl.LOCK_EX)
                os.close(inherited_fd)
                inherited_fd = None
                yield
        else:
            yield
    finally:
        if inherited_fd is not None:
            os.close(inherited_fd)


def start_refresh(state, job, config_path):
    """Start one owned worker, or retain one successor to cover a new correction."""
    _job(job)
    state = Path(state).expanduser().absolute()
    store = SpeakerCorrections(state)
    # Check availability and configuration before creating a background child.
    with job_lock(state, job):
        store._snapshot(job)
        _settings(config_path)
    for wakeup in (False, True):
        try:
            with _lock(_lock_path(state, job, wakeup)) as handle:
                if not wakeup:
                    with job_lock(state, job):
                        store._snapshot(job)
                        _status(store, job, "pending")
                return subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                    "--state", str(state), "--job", job, "--config", str(Path(config_path).expanduser().absolute()),
                    "--lock-fd", str(handle.fileno()), *( ["--wakeup"] if wakeup else [])],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, pass_fds=(handle.fileno(),))
        except ProcessingBusy:
            continue
        except OSError as error:
            diagnostic = "Speaker notes worker could not be started"
            _failure_status(store, job, diagnostic)
            raise WorkflowError(diagnostic) from error
    return None


def refresh(state, job, config_path, *, inherited_fd=None, wakeup=False):
    _job(job)
    state = Path(state).expanduser().absolute()
    store = SpeakerCorrections(state)
    with _worker_lock(state, job, inherited_fd, wakeup):
        try:
            with job_lock(state, job):
                transcript, original, bundle, revision = store._snapshot(job)
                settings = _settings(config_path)
                visible = _visible_context(state, job, original)
                from quill_workflow.review_library import _Sources
                sources = _Sources(state)
                try:
                    existing = load_reviewed_notes(sources, job, transcript, original, bundle)
                finally:
                    sources.close()
                if existing is not None:
                    _status(store, job, "succeeded")
                    return existing
                _status(store, job, "running")
            # Temporary inputs survive a concurrent local deletion without reading
            # or holding any original media. Commit still requires a live source.
            with tempfile.TemporaryDirectory(prefix="quill-speaker-notes-") as temporary:
                directory = Path(temporary)
                source_path, identity_path, context_path, output = [directory / name for name in
                    ("transcript.json", "identity.json", "context.json", "notes.json")]
                atomic_json(source_path, transcript)
                atomic_json(identity_path, bundle)
                atomic_json(context_path, visible)
                codex_notes.run(source_path, output, context_path=context_path, identity_path=identity_path,
                                cancelled=lambda: job in stopped_jobs(state, job), **settings)
                notes = read_json(output, limit=5_000_000)
                if notes.get("_speaker_identity") != bundle:
                    raise WorkflowError("Refreshed notes do not match the confirmed speaker review")
                codex_notes.validate_result(notes, transcript, require_title=True)
                rendered = render_notes(notes, transcript)
                binding = _bindings(transcript, original, bundle, notes)
                with job_lock(state, job):
                    current_transcript, current_original, current_bundle, current_revision = store._snapshot(job)
                    if (revision != current_revision or binding != _bindings(
                            current_transcript, current_original, current_bundle, notes)):
                        raise WorkflowError("Speaker names changed during notes refresh; retry with the latest names")
                    directory = store._directory(job)
                    atomic_json(directory / "notes.json", notes)
                    atomic_json(directory / "notes-binding.json", binding)
                    _atomic_text(directory / "notes-reviewed.md", rendered)
                    _status(store, job, "succeeded")
                return notes
        except (OSError, ValueError, TypeError, WorkflowError) as error:
            _failure_status(store, job, error)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--lock-fd", type=int)
    parser.add_argument("--wakeup", action="store_true")
    args = parser.parse_args(argv)

    def interrupted(_signum, _frame):
        raise WorkflowError("Speaker notes refresh was interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        refresh(args.state, args.job, args.config, inherited_fd=args.lock_fd, wakeup=args.wakeup)
    except (OSError, ValueError, WorkflowError):
        return 1  # A bounded diagnostic is in the local status, never stdout.
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
