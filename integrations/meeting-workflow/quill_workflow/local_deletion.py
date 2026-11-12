"""Explicit, reversible local meeting removal. Never calls a cloud connector."""
from contextlib import contextmanager, ExitStack
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import time

from .pipeline import Queue, WorkflowError, atomic_json, load_config, read_json
from .local_naming import session_path
from .recording_safety import CANCEL_MARKER, FILENAME, RecordingSafetyHold, read_safety

MARKER = ".quill-deleted.json"
LEASE = ".meeting-files.lock"
CAPABILITY = ".meeting-files-v1.json"
JOB = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
class ProcessingBusy(WorkflowError):
    """The deletion is accepted; an owned writer has not released its lease."""


@contextmanager
def job_lock(state, job, *, shared=False):
    from .processing_lease import lease_context
    with _lock(Path(state) / ("processing-" + hashlib.sha256(job.encode()).hexdigest() + ".lock"),
               shared=shared) as handle:
        with lease_context(handle.fileno()):
            yield handle


def pending_deletions(state):
    path = Path(state) / "jobs.sqlite3"
    if not path.exists():
        return False
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='local_deletions'").fetchone():
            return False
        return any(json.loads(row[0])["status"] == "cancelling" for row in db.execute("SELECT record FROM local_deletions"))


def _revision(snapshot):
    # Processing may finish between review and confirmation. Bind the exact
    # original and finished capture, not its evolving generated text or tasks.
    original = snapshot["trees"][0]
    core = [entry for entry in original["entries"] if entry[0] in snapshot["capture_files"]]
    return _digest({"job": snapshot["job"], "original": {key: original[key] for key in ("path", "device", "inode")},
                    "capture": core, "safety": snapshot["safety"]})
SCHEMA = """
CREATE TABLE IF NOT EXISTS local_deletions (job TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS local_deletion_source_bindings (
 job TEXT PRIMARY KEY, device INTEGER NOT NULL, inode INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS local_deletion_reviews (
 job TEXT PRIMARY KEY, revision TEXT NOT NULL, confirmation TEXT NOT NULL, prepared REAL NOT NULL
);
"""


def deleted_jobs(state, job=None):
    """Read tombstones without creating a queue or changing its state."""
    path = Path(state) / "jobs.sqlite3"
    if not path.exists():
        return set()
    if path.is_symlink():
        raise WorkflowError("The meeting queue is linked; local review is unavailable")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_deletions'").fetchone():
            return set()
        if job is not None:
            return {row[0] for row in db.execute("SELECT job FROM local_deletions WHERE job=?", (job,))}
        return {row[0] for row in db.execute("SELECT job FROM local_deletions")}


def stopped_jobs(state, job=None):
    """Close processing actions without hiding the kept recording or its receipts."""
    removed = deleted_jobs(state, job)
    arguments = (job,) if job is not None else ()
    path = Path(state) / "jobs.sqlite3"
    if not path.exists():
        return removed
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "processing_cancellations" in tables:
            query = "SELECT job FROM processing_cancellations" + (" WHERE job=?" if arguments else "")
            removed.update(row[0] for row in db.execute(query, arguments))
        if "jobs" in tables:
            query = ("SELECT j.id,j.session,n.destination FROM jobs j LEFT JOIN local_names n ON n.job=j.id"
                     if "local_names" in tables else "SELECT j.id,j.session,NULL FROM jobs j")
            if arguments:
                query += " WHERE j.id=?"
            for candidate, original, destination in db.execute(query, arguments):
                source = destination if destination and os.path.lexists(destination) else original
                if os.path.lexists(Path(source) / CANCEL_MARKER):
                    removed.add(candidate)
    return removed


def _busy(path):
    """Inspect an existing writer lease without creating a file during review."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkflowError("Cannot check the meeting's processing lock")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def _mark_cancelled(source, expected, safety):
    fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".quill-cancel-" + secrets.token_hex(12)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != expected:
            raise WorkflowError("The recording folder changed before cancellation")
        content = {"schema_version": 1, "cancelled_at": time.time(), "reason": "user_requested"}
        if safety:
            content["session_id"] = safety["session_id"]
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd), "w") as file:
            json.dump(content, file)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temporary, CANCEL_MARKER, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        except FileExistsError:
            pass  # A native cancellation of the same recording can win the race.
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _directory(path):
    path = Path(path)
    # Configured roots are canonicalized before this check. Descendants may
    # never redirect an operation through a symlink, including an ancestor.
    if not path.is_absolute() or str(path) != str(path.resolve(strict=True)):
        raise WorkflowError("The local meeting path is linked or unavailable")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise WorkflowError("The local meeting folder is unavailable")
    return info


def _tree(path, changing=False):
    """Bind a bounded inventory to the review; never read media or follow links."""
    _directory(path)
    entries = []
    for parent, directories, files in os.walk(path, followlinks=False):
        for name in sorted(directories + files):
            child = Path(parent) / name
            try:
                info = child.lstat()
            except FileNotFoundError:
                if changing:
                    continue
                raise
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise WorkflowError("The meeting contains linked or special files; review it in Finder")
            # Lease metadata changes when the app opens it, not meeting content.
            if child == path / LEASE:
                continue
            entries.append([str(child.relative_to(path)), info.st_dev, info.st_ino,
                            info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
            if len(entries) > 10000:
                raise WorkflowError("This local folder is too large to review safely here")
    info = path.stat()
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino,
            "entries": entries}


@contextmanager
def _lock(path, *, shared=False):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "a") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or Path(path).name == LEASE and info.st_mode & 0o077):
            raise WorkflowError("The local meeting lock is invalid")
        try:
            fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProcessingBusy("Stopping processing before moving this meeting to Trash.") from error
        yield handle


def lease_capable(session):
    path = session / CAPABILITY
    if not os.path.lexists(path):
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1000 or info.st_nlink != 1:
                raise ValueError("Invalid lease marker")
            data = handle.read(1001)
            if len(data) > 1000:
                raise ValueError("Invalid lease marker")
        value = json.loads(data)
        if (not isinstance(value, dict) or set(value) != {"schema_version", "shared_processing_lease"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["shared_processing_lease"] is not True):
            raise ValueError("Invalid lease marker")
        return True
    except (OSError, ValueError, TypeError) as error:
        raise WorkflowError("The meeting’s processing lease marker needs review before deletion") from error


def trash_directory(path):
    """Use macOS Trash, preserving Finder recovery and external-volume support."""
    if sys.platform != "darwin":
        raise WorkflowError("Moving meetings to Trash is supported only on macOS")
    script = '''ObjC.import("Foundation");
function run(argv) {
  const manager = $.NSFileManager.defaultManager;
  const source = $.NSURL.fileURLWithPath($(argv[0]));
  const trash = manager.URLForDirectoryInDomainAppropriateForURLCreateError(
    $.NSTrashDirectory, $.NSUserDomainMask, source, true, null);
  if (!trash) throw Error("macOS Trash is unavailable");
  const destination = ObjC.unwrap(trash.path) + "/" + ObjC.unwrap(source.lastPathComponent);
  if (manager.fileExistsAtPath($(destination))) throw Error("Trash name collision");
  // JXA does not safely retain this API's autoreleased NSURL out parameter.
  // Use the unique wrapper name and verify its inode at the known Trash URL.
  if (!manager.trashItemAtURLResultingItemURLError(source, null, null))
    throw Error("macOS could not move the meeting to Trash");
  return destination;
}'''
    result = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script, str(path)],
                            capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise WorkflowError("macOS could not move the meeting to Trash. Its files remain recoverable.")
    return result.stdout.strip()


class LocalMeetingDeletion:
    def __init__(self, state, config_path, trash=trash_directory):
        self.queue = Queue(state)
        self.queue.db.executescript(SCHEMA)
        self.config_path = Path(config_path)
        self.trash = trash

    def close(self):
        self.queue.close()

    def _get(self, job):
        if not isinstance(job, str) or not JOB.fullmatch(job):
            raise WorkflowError("Choose a saved meeting")
        row = self.queue.db.execute("SELECT record FROM local_deletions WHERE job=?", (job,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, record):
        saved = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)
        for job in record["snapshot"].get("jobs", [record["job"]]):
            self.queue.db.execute("INSERT INTO local_deletions VALUES (?,?) ON CONFLICT(job) DO UPDATE SET record=excluded.record",
                                  (job, saved))

    @contextmanager
    def _exclusive(self, job, include_session=True, jobs=None):
        with ExitStack() as stack:
            for candidate in sorted(jobs or [job]):
                stack.enter_context(job_lock(self.queue.state, candidate))
                stack.enter_context(_lock(self.queue.state / ("sharing-" + hashlib.sha256(candidate.encode()).hexdigest() + ".lock")))
            if include_session:
                source = self._source(job)
                _directory(source)
                config = load_config(self.config_path)
                root = Path(config.get("recordings_root", str(Path.home() / "Recordings"))).expanduser().resolve(strict=True)
                if source.parent != root:
                    raise WorkflowError("Only a meeting inside the configured recordings folder can be deleted")
                stack.enter_context(_lock(source / LEASE))
            yield

    def _local_sources(self, exclude=()):
        config = load_config(self.config_path)
        root = Path(config.get("recordings_root", str(Path.home() / "Recordings"))).expanduser().resolve(strict=True)
        for source in list(root.iterdir())[:10000]:
            if str(source) in exclude:
                continue
            try:
                info = _directory(source)
                meta = read_json(source / "meta.json", 100000)
                if not isinstance(meta, dict) or not meta.get("started") or not meta.get("ended"):
                    continue
                job = "local-" + _digest([str(source), info.st_dev, info.st_ino, meta["started"]])[:24]
                yield job, source
            except (OSError, ValueError, WorkflowError):
                continue

    def _source(self, job):
        if self.queue.db.execute("SELECT 1 FROM jobs WHERE id=?", (job,)).fetchone():
            return session_path(self.queue, job)
        known = self._known_sources()
        for candidate, source in self._local_sources(exclude=known):
            if job == candidate:
                return source
        raise WorkflowError("This saved meeting is unavailable")

    def _known_sources(self):
        # Reserve saved paths even when a historical identity cannot currently
        # be verified. Do not relabel that source as a fresh prequeue meeting
        # and bypass its saved identity checks. These paths are only exclusions.
        known = set()
        for row in self.queue.db.execute("SELECT session FROM jobs"):
            known.add(str(Path(os.path.abspath(Path(row[0]).expanduser()))))
        for row in self.queue.db.execute("SELECT destination FROM local_names WHERE destination IS NOT NULL"):
            known.add(str(Path(os.path.abspath(Path(row[0]).expanduser()))))
        for row in self.queue.db.execute("SELECT path FROM local_name_aliases"):
            known.add(str(Path(os.path.abspath(Path(row[0]).expanduser()))))
        return known

    def _snapshot(self, job, *, for_cancellation=False, include_related=True):
        if self._get(job):
            raise WorkflowError("This meeting has already been removed from processing")
        tasks = self.queue.tasks(job)
        source = self._source(job)
        config = load_config(self.config_path)
        root = Path(config.get("recordings_root", str(Path.home() / "Recordings"))).expanduser().resolve(strict=True)
        source_info = _directory(source)
        enrolled = self.queue.db.execute("SELECT device,inode FROM local_deletion_source_bindings WHERE job=?", (job,)).fetchone()
        if enrolled and (source_info.st_dev, source_info.st_ino) != tuple(enrolled):
            raise WorkflowError("The original folder was replaced. No replacement folder can be deleted here.")
        if source.parent != root:
            raise WorkflowError("Only a meeting directly inside the configured recordings folder can be deleted")
        if os.path.lexists(source / MARKER):
            raise WorkflowError("This folder was already removed from processing")
        binding = self.queue.db.execute("SELECT * FROM recording_safety_bindings WHERE job=?", (job,)).fetchone()
        safety = read_safety(source, required=bool(binding),
                             expected_session_id=binding["session_id"] if binding else None, require_approval=False)
        if safety and safety["phase"] != "finished":
            raise WorkflowError("Stop the recording before deleting its meeting folder")
        directory_binding = binding or self.queue.db.execute(
            "SELECT device,inode FROM local_names WHERE job=?", (job,)).fetchone()
        if directory_binding:
            from .recording_identity import verify_directory
            verify_directory(self.queue, job, source, directory_binding["device"], directory_binding["inode"])
        meta = read_json(source / "meta.json", 100000)
        if not isinstance(meta, dict) or not meta.get("started") or not meta.get("ended"):
            raise WorkflowError("Stop the recording before deleting its meeting folder")
        # Older installed apps lack the lease. A missing requested transcript
        # is not proof of an idle transcriber, so remain conservative.
        # Publishing a stop marker never moves or unlinks media under an older writer.
        capable = for_cancellation or lease_capable(source)
        if (meta.get("transcription_requested") is not False and not (source / "transcript.json").is_file()
                and not capable):
            raise WorkflowError("Local transcription has not finished. Wait before deleting this meeting.")
        # A raw-only enqueue and a later transcript can have distinct queue
        # IDs for the same original. Include every matching saved source, never
        # a different recording that merely reused an old timestamp path.
        manifest_path = self.queue.state / "recordings" / job / "manifest.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else None
        same_source = [job]
        candidates = self.queue.db.execute("SELECT id FROM jobs WHERE id<>?", (job,)).fetchall() if include_related else ()
        for row in candidates:
            candidate = row["id"]
            if self._get(candidate):
                continue
            try:
                location = session_path(self.queue, candidate)
                info = _directory(location)
                if (location != source or (info.st_dev, info.st_ino) != (source_info.st_dev, source_info.st_ino)):
                    continue
                saved = read_json(self.queue.state / "recordings" / candidate / "manifest.json")
                core = {"meta.json", "mic.caf", "system.caf"}
                screen = meta.get("files", {}).get("screen")
                if screen:
                    core.add(screen)
                if manifest is None or all(saved.get(name) == manifest.get(name) for name in core):
                    same_source.append(candidate)
            except Exception:
                continue
        same_source.sort()
        tasks = [task for candidate in same_source for task in self.queue.tasks(candidate)]
        paths = [source]
        for candidate in same_source:
            for member in ("recordings", "results"):
                path = self.queue.state / member / candidate
                if os.path.lexists(path):
                    paths.append(path)
        trees = [_tree(source, changing=True)]
        for path in paths[1:]:
            info = _directory(path)
            trees.append({"path": str(path), "device": info.st_dev, "inode": info.st_ino, "entries": []})
        original = self.queue.db.execute("SELECT session FROM jobs WHERE id=?", (job,)).fetchone()
        original = original[0] if original else str(source)
        return {"job": job, "jobs": same_source, "title": source.name, "original": original,
                "tasks": tasks, "trees": trees, "safety": safety,
                "capture_files": sorted({"meta.json", "mic.caf", "system.caf", meta.get("files", {}).get("screen", "screen.mp4")})}

    def _bind_legacy_source(self, job, snapshot):
        if (self.queue.db.execute("SELECT 1 FROM recording_safety_bindings WHERE job=?", (job,)).fetchone()
                or self.queue.db.execute("SELECT 1 FROM local_names WHERE job=?", (job,)).fetchone()
                or self.queue.db.execute("SELECT 1 FROM local_deletion_source_bindings WHERE job=?", (job,)).fetchone()):
            return
        # Old jobs predate app session IDs and saved directory inodes. Verify
        # their immutable queued media once before enrolling a deletion lease.
        from .recording_identity import _manifest, _verify_files
        source = Path(snapshot["trees"][0]["path"])
        info = _directory(source)
        if self.queue.db.execute("SELECT 1 FROM jobs WHERE id=?", (job,)).fetchone():
            manifest, _ = _manifest(self.queue, job)
            _verify_files(source, manifest, (info.st_dev, info.st_ino))
        with self.queue.db:
            self.queue.db.execute("INSERT INTO local_deletion_source_bindings VALUES (?,?,?)",
                                  (job, info.st_dev, info.st_ino))

    def _cancel_option(self, snapshot, meta=None):
        job = snapshot["job"]
        source = Path(snapshot["trees"][0]["path"])
        meta = meta or read_json(source / "meta.json", 100000)
        if not isinstance(meta, dict) or not isinstance(meta.get("started"), str) or not meta.get("ended"):
            return None
        item = {"job": job, "revision": _revision(snapshot), "title": source.name}
        try:
            recorded = datetime.fromisoformat(meta["started"].replace("Z", "+00:00"))
            if recorded.tzinfo is not None:
                item["recorded_at"] = recorded.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
        # Both native transcription and a queued connector can still be unwinding.
        if os.path.lexists(source / CANCEL_MARKER) or self.queue.db.execute(
                "SELECT 1 FROM processing_cancellations WHERE job=?", (job,)).fetchone():
            tree = snapshot["trees"][0]
            aliases = self.queue.db.execute("SELECT job FROM processing_cancellations WHERE device=? AND inode=?",
                                            (tree["device"], tree["inode"]))
            locks = [source / LEASE]
            for candidate in set(snapshot["jobs"]).union(row[0] for row in aliases):
                hashed = hashlib.sha256(candidate.encode()).hexdigest()
                locks.extend(self.queue.state / (prefix + hashed + ".lock") for prefix in ("processing-", "sharing-"))
            item["state"] = "cancelling" if any(_busy(path) for path in locks) else "cancelled"
            return item
        transcription = (meta.get("transcription_requested") is True and not (source / "transcript.json").is_file()
                         and (not snapshot["safety"] or snapshot["safety"]["review_status"] == "approved"))
        def unfinished(task):
            if task["status"] in ("running", "pending", "retry", "failed", "safety_held"):
                return True
            settings = json.loads(task["settings"]) if task["status"] == "held" else None
            return isinstance(settings, dict) and settings.get("enabled") is True
        if not (transcription or any(unfinished(task) for task in snapshot["tasks"])):
            return None
        item["state"] = "available"
        if transcription:
            item["phase"] = "transcription"
        return item

    def _cancel_preview(self, job, root, source, tasks):
        """Only stat capture members to bind the same revision as the full confirmation."""
        if self._get(job) or source.parent != root:
            return None
        info = _directory(source)
        meta = read_json(source / "meta.json", 100000)
        if not isinstance(meta, dict) or not isinstance(meta.get("files"), dict) or not meta.get("ended"):
            return None
        safety = read_safety(source, require_approval=False)
        if safety and safety["phase"] != "finished":
            return None
        names = {"meta.json", "mic.caf", "system.caf", meta["files"].get("screen", "screen.mp4")}
        entries = []
        for name in sorted(names):
            if not isinstance(name, str) or Path(name).name != name or name in ("", ".", ".."):
                return None
            member = source / name
            try:
                saved = member.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(saved.st_mode):
                return None
            entries.append([name, saved.st_dev, saved.st_ino, saved.st_mode,
                            saved.st_size, saved.st_mtime_ns, saved.st_ctime_ns])
        tree = {"path": str(source), "device": info.st_dev, "inode": info.st_ino, "entries": entries}
        return self._cancel_option({"job": job, "jobs": [job], "trees": [tree], "capture_files": names,
                                    "tasks": tasks, "safety": safety}, meta)

    def cancellation_option(self, job):
        if self._get(job):
            return None
        root = Path(load_config(self.config_path).get("recordings_root", Path.home() / "Recordings")).expanduser().resolve()
        try:
            return self._cancel_preview(job, root, self._source(job), self.queue.tasks(job))
        except (OSError, ValueError, TypeError, KeyError, WorkflowError, RecordingSafetyHold):
            if self.queue.db.execute("SELECT 1 FROM processing_cancellations WHERE job=?", (job,)).fetchone():
                row = self.queue.db.execute("SELECT session FROM jobs WHERE id=?", (job,)).fetchone()
                return {"job": job, "title": Path(row[0]).name if row else "Local meeting",
                        "revision": "unavailable", "state": "cancelled"}
            return None

    def cancellation_options(self):
        root = Path(load_config(self.config_path).get("recordings_root", Path.home() / "Recordings")).expanduser().resolve()
        tasks = {}
        for task in self.queue.tasks():
            tasks.setdefault(task["job"], []).append(task)
        results, known = [], set()
        def append(job, source):
            try:
                item = self._cancel_preview(job, root, source, tasks.get(job, []))
                if item:
                    results.append(item)
            except (OSError, ValueError, TypeError, KeyError, WorkflowError, RecordingSafetyHold):
                pass  # An unavailable historical source can't hide other recordings.
        for row in self.queue.db.execute("SELECT j.id,j.session,n.destination FROM jobs j "
                                         "LEFT JOIN local_names n ON n.job=j.id ORDER BY j.created DESC"):
            known.add(str(row["session"]))
            if row["destination"]:
                known.add(row["destination"])
            try:
                source = session_path(self.queue, row["id"])
                known.add(str(source))
                append(row["id"], source)
            except (OSError, ValueError, WorkflowError, RecordingSafetyHold):
                continue
        known.update(row[0] for row in self.queue.db.execute("SELECT path FROM local_name_aliases"))
        for job, source in self._local_sources(exclude=known):
            append(job, source)
        listed = {item["job"] for item in results}
        for row in self.queue.db.execute("SELECT j.id, j.session FROM processing_cancellations c JOIN jobs j ON c.job=j.id "
                                         "LEFT JOIN local_deletions d ON d.job=c.job WHERE d.job IS NULL"):
            if row["id"] not in listed:
                results.append({"job": row["id"], "title": Path(row["session"]).name,
                                "revision": "unavailable", "state": "cancelled"})
        return results

    def cancel_processing(self, job, revision):
        """Stop this finished recording at its next processing boundary. Keep all files."""
        with _lock(self.queue.state / "local-deletion-control.lock"):
            snapshot = self._snapshot(job, for_cancellation=True)
            if _revision(snapshot) != revision:
                raise WorkflowError("This recording changed. Reload before cancelling processing.")
            if self._cancel_option(snapshot) is None:
                raise WorkflowError("This recording has no remaining processing to cancel")
            self._bind_legacy_source(job, snapshot)
            self.queue.db.execute("BEGIN IMMEDIATE")
            try:
                # Serialize with enqueue/rename and recheck the exact finished capture,
                # including any raw-only and transcribed queue IDs for the same source.
                snapshot = self._snapshot(job, for_cancellation=True)
                if _revision(snapshot) != revision or self._cancel_option(snapshot) is None:
                    raise WorkflowError("Processing or the recording changed. Reload its review.")
                tree, safety = snapshot["trees"][0], snapshot["safety"]
                source = Path(tree["path"])
                started = read_json(source / "meta.json", 100000)["started"]
                _mark_cancelled(source, (tree["device"], tree["inode"]), safety)
                for candidate in snapshot["jobs"]:
                    self.queue.db.execute("INSERT OR IGNORE INTO processing_cancellations VALUES (?,?,?,?,?)",
                                          (candidate, tree["device"], tree["inode"], started, (safety or {}).get("session_id")))
                    self.queue.stop_remaining(candidate)
                self.queue.db.commit()
            except BaseException:
                self.queue.db.rollback()
                raise
        self.queue.write_status()
        return self._cancel_option(snapshot)

    @staticmethod
    def _pending_removal(job, record):
        if record["job"] != job or record["status"] == "trashed":
            return None
        cancelling = record["status"] == "cancelling"
        return {"job": job, "title": record["title"], "revision": record["revision"], "available": True,
                "interrupted": not cancelling, "cancelling": cancelling,
                "detail": ("Stopping processing… This meeting will move to Trash automatically." if cancelling else
                           "Removal paused. Processing is stopped; the remaining files are still recoverable.")}

    def _option(self, job, session):
        previous = self._get(job)
        if previous:
            return self._pending_removal(job, previous)
        try:
            # The revision binds only the original capture. Prepare and confirm
            # perform the full duplicate-job, queued-copy and writer checks.
            snapshot = self._snapshot(job, include_related=False)
            return {"job": job, "title": snapshot["title"], "revision": _revision(snapshot), "available": True}
        except Exception as error:
            return {"job": job, "title": Path(session).name[:180], "revision": "unavailable",
                    "available": False, "detail": str(error)[:250]}

    def options(self, job=None):
        if job is not None:
            previous = self._get(job)
            if previous:
                option = self._pending_removal(job, previous)
            else:
                row = self.queue.db.execute("SELECT session FROM jobs WHERE id=?", (job,)).fetchone()
                if row:
                    option = self._option(job, row["session"])
                else:
                    try:
                        source = self._source(job)
                    except (OSError, ValueError, WorkflowError):
                        return []
                    option = self._option(job, source)
            return [option] if option else []

        rows = {row["id"]: row["session"] for row in self.queue.db.execute(
            "SELECT id,session FROM jobs ORDER BY created DESC")}
        rows.update(self._local_sources(exclude=self._known_sources()))
        for (removed_job,) in self.queue.db.execute("SELECT job FROM local_deletions"):
            rows.setdefault(removed_job, "")
        return [option for job, session in rows.items() if (option := self._option(job, session))]

    def summaries(self, stopped):
        """List local meetings and stop states, without preparing deletion or reading their content."""
        removals, cancellations = [], []
        removed = set()
        for row in self.queue.db.execute(
                "SELECT job,json_extract(record,'$.job'),json_extract(record,'$.status'),"
                "json_extract(record,'$.title'),json_extract(record,'$.revision') FROM local_deletions"):
            job, primary, status, title, revision = row
            removed.add(job)
            item = self._pending_removal(job, {"job": primary, "title": title, "status": status, "revision": revision})
            if item:
                removals.append(item)
        config = load_config(self.config_path)
        root = Path(config.get("recordings_root", Path.home() / "Recordings")).expanduser().resolve()
        paths = {}
        for row in self.queue.db.execute("SELECT j.id,j.session,n.destination FROM jobs j "
                                         "LEFT JOIN local_names n ON n.job=j.id"):
            job = row["id"]
            if job in removed:
                continue
            source = Path(row["destination"] or row["session"])
            if row["destination"] and not os.path.lexists(source):
                source = Path(row["session"])
            paths[job] = source
        if root.is_dir():
            paths.update(self._local_sources(exclude=self._known_sources()))
        for job, source in paths.items():
            if job in removed:
                continue
            try:
                if source.parent != root:
                    continue
                _directory(source)
                if os.path.lexists(source / MARKER):
                    continue
                # Only finished local folders supply rows. Permission for a
                # particular action is always evaluated on the detail page.
                meta = read_json(source / "meta.json", 100000)
                if not isinstance(meta, dict) or not meta.get("started") or not meta.get("ended"):
                    continue
                removals.append({"job": job, "title": source.name, "available": True})
                if (job in stopped or os.path.lexists(source / CANCEL_MARKER)
                        or meta.get("transcription_requested") is True and not (source / "transcript.json").is_file()):
                    item = self._cancel_preview(job, root, source, [])
                    if item:
                        cancellations.append(item)
            except (OSError, ValueError, TypeError, KeyError, WorkflowError, RecordingSafetyHold):
                continue
        listed = {item["job"] for item in cancellations}
        for job in stopped - removed - listed:
            if job in paths:
                cancellations.append({"job": job, "title": paths[job].name, "state": "cancelled"})
        return removals, cancellations

    def prepare(self, job, revision):
        previous = self._get(job)
        if previous:
            if previous["revision"] != revision or previous["status"] == "trashed":
                raise WorkflowError("This removal confirmation is no longer valid")
            return {"job": job, "revision": revision, "confirmation": previous["confirmation"],
                    "title": previous["title"], "folder": previous["snapshot"]["trees"][0]["path"]}
        with _lock(self.queue.state / "local-deletion-control.lock"):
            snapshot = self._snapshot(job)
            if _revision(snapshot) != revision:
                raise WorkflowError("This meeting changed. Reload its review before deleting.")
            self._bind_legacy_source(job, snapshot)
            # Validate/create the private lease without requiring exclusive
            # access: the running transcriber owns its shared lock until stopped.
            fd = os.open(Path(snapshot["trees"][0]["path"]) / LEASE,
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            os.close(fd)
            token = secrets.token_urlsafe(24)
            with self.queue.db:
                self.queue.db.execute("INSERT INTO local_deletion_reviews VALUES (?,?,?,?) ON CONFLICT(job) DO UPDATE SET "
                                      "revision=excluded.revision,confirmation=excluded.confirmation,prepared=excluded.prepared",
                                      (job, revision, token, time.time()))
            return {"job": job, "revision": revision, "confirmation": token,
                    "title": snapshot["title"], "folder": snapshot["trees"][0]["path"]}

    def confirm(self, job, revision, confirmation):
        previous = self._get(job)
        if previous:
            if previous["revision"] != revision or previous["confirmation"] != confirmation:
                raise WorkflowError("This removal confirmation is no longer valid")
            if previous["status"] == "trashed":
                return previous
            return self.finish(previous)
        with _lock(self.queue.state / "local-deletion-control.lock"):
            self.queue.db.execute("BEGIN IMMEDIATE")
            try:
                request = self.queue.db.execute("SELECT * FROM local_deletion_reviews WHERE job=?", (job,)).fetchone()
                if (not request or request["revision"] != revision or request["confirmation"] != confirmation
                        or time.time() - request["prepared"] > 600):
                    raise WorkflowError("Review this deletion again before confirming")
                snapshot = self._snapshot(job)
                if _revision(snapshot) != revision:
                    raise WorkflowError("This meeting changed. Reload its review before deleting.")
                record = {"job": job, "revision": revision, "confirmation": confirmation,
                          "title": snapshot["title"], "status": "cancelling", "confirmed_at": time.time(),
                          "snapshot": snapshot, "moves": []}
                record["receipt_journals"] = {}
                for candidate in snapshot["jobs"]:
                    result = self.queue.state / "results" / candidate
                    if result.is_dir():
                        for receipt in list(result.glob("*-receipt.json")) + list(result.glob("drive-*.json")):
                            record["receipt_journals"][candidate + "/" + receipt.name] = read_json(receipt, 5_000_000)
                for index, tree in enumerate(snapshot["trees"]):
                    path = Path(tree["path"])
                    label = "Recording" if index == 0 else "Processing copies"
                    wrapper = path.parent / ("Quill - " + label + " - " + secrets.token_hex(12))
                    record["moves"].append({"source": str(path), "wrapper": str(wrapper),
                                             "member": path.name, "device": tree["device"], "inode": tree["inode"],
                                             "state": "planned"})
                self._save(record)
                # Preserve settings, attempts, and receipts as audit evidence.
                for candidate in snapshot["jobs"]:
                    self.queue.db.execute("UPDATE tasks SET status='deleted',error='Local deletion requested; processing cancelled',next_attempt=0 WHERE job=?", (candidate,))
                    self.queue.db.execute("UPDATE local_names SET status='deleted',error=NULL WHERE job=?", (candidate,))
                self.queue.db.commit()
            except BaseException:
                self.queue.db.rollback()
                raise
        return self.finish(record)

    def finish(self, record):
        """Reconcile a durable request after every owned writer has stopped."""
        try:
            with _lock(self.queue.state / "local-deletion-control.lock"), ExitStack() as stack:
                record = self._get(record["job"])
                if record["status"] == "trashed":
                    return record
                first = record["moves"][0]
                original = Path(first["source"])
                staged = Path(first["wrapper"]) / first["member"]
                remaining = (None if first["state"] == "trashed" else staged if os.path.lexists(staged)
                             else original if os.path.lexists(original) else None)
                if remaining is not None:
                    info = _directory(remaining)
                    if (info.st_dev, info.st_ino) != (first["device"], first["inode"]):
                        raise WorkflowError("The source folder changed; no replacement folder was moved")
                    # Publish before waiting for the shared Swift lease. Its
                    # owner stops its own task and releases only after cleanup.
                    atomic_json(remaining / MARKER, {"schema_version": 1, "job": record["job"],
                                                    "deleted_at": record["confirmed_at"], "state": "cancelling"})
                stack.enter_context(self._exclusive(record["job"], include_session=False,
                                                    jobs=record["snapshot"]["jobs"]))
                if remaining is not None:
                    stack.enter_context(_lock(remaining / LEASE))
                # A writer may have created a result folder after confirmation.
                # Now its lease is free, include that final folder and receipts.
                known = {move["source"] for move in record["moves"]}
                for candidate in record["snapshot"]["jobs"]:
                    for category in ("recordings", "results"):
                        path = self.queue.state / category / candidate
                        if os.path.lexists(path) and str(path) not in known:
                            info = _directory(path)
                            record["moves"].append({"source": str(path), "member": path.name,
                                "wrapper": str(path.parent / ("Quill - Processing copies - " + secrets.token_hex(12))),
                                "device": info.st_dev, "inode": info.st_ino, "state": "planned"})
                    result = self.queue.state / "results" / candidate
                    if result.is_dir():
                        for receipt in list(result.glob("*-receipt.json")) + list(result.glob("drive-*.json")):
                            record["receipt_journals"][candidate + "/" + receipt.name] = read_json(receipt, 5_000_000)
                for move in record["moves"]:
                    if move["state"] == "planned":
                        _tree(Path(move["source"]))
                with self.queue.db:
                    self._save(record)
                return self._move(record)
        except ProcessingBusy:
            return record
        except Exception as error:
            record.update(status="needs_attention", error=str(error)[:500])
            with self.queue.db:
                self._save(record)
            raise

    def reconcile(self):
        rows = self.queue.db.execute("SELECT job,record FROM local_deletions").fetchall()
        for row in rows:
            record = json.loads(row["record"])
            if record["job"] == row["job"] and record["status"] == "cancelling":
                try:
                    self.finish(record)
                except WorkflowError:
                    pass  # A concrete filesystem failure stays visible for review.

    def _move(self, record):
        try:
            for move in record["moves"]:
                if move["state"] == "trashed":
                    continue
                source, wrapper = Path(move["source"]), Path(move["wrapper"])
                member = wrapper / move["member"]
                if not os.path.lexists(wrapper):
                    if move["state"] == "staged":
                        raise WorkflowError("The staged folder moved before its Trash receipt was saved. Check Finder Trash; processing stays stopped.")
                    _directory(source.parent)
                    info = _directory(source)
                    if (info.st_dev, info.st_ino) != (move["device"], move["inode"]):
                        raise WorkflowError("The local folder changed; no replacement folder was moved")
                    wrapper.mkdir(mode=0o700)
                    move["wrapper_identity"] = [wrapper.stat().st_dev, wrapper.stat().st_ino]
                    with self.queue.db:
                        self._save(record)
                info = _directory(wrapper)
                if [info.st_dev, info.st_ino] != move.get("wrapper_identity"):
                    raise WorkflowError("The temporary Trash folder changed; processing stays stopped")
                if not os.path.lexists(member):
                    info = _directory(source)
                    if (info.st_dev, info.st_ino) != (move["device"], move["inode"]):
                        raise WorkflowError("The source folder changed; no replacement folder was moved")
                    # The destination is an exclusive private wrapper. Pin both
                    # parents and rename the directory itself; never traverse it.
                    with ExitStack() as stack:
                        parent_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        stack.callback(os.close, parent_fd)
                        wrapper_fd = os.open(wrapper, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        stack.callback(os.close, wrapper_fd)
                        observed = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
                        if (observed.st_dev, observed.st_ino) != (move["device"], move["inode"]):
                            raise WorkflowError("The source folder changed; nothing was moved")
                        os.rename(source.name, move["member"], src_dir_fd=parent_fd, dst_dir_fd=wrapper_fd)
                info = _directory(member)
                if (info.st_dev, info.st_ino) != (move["device"], move["inode"]):
                    raise WorkflowError("The staged meeting folder changed; processing stays stopped")
                move["state"] = "staged"
                with self.queue.db:
                    self._save(record)
                destination = Path(self.trash(wrapper))
                destination_info = _directory(destination)
                if (os.path.lexists(wrapper) or [destination_info.st_dev, destination_info.st_ino]
                        != move["wrapper_identity"]):
                    raise WorkflowError("The Trash location could not be verified; check Finder before retrying")
                move.update(state="trashed", trash_path=str(destination))
                with self.queue.db:
                    self._save(record)
            record.update(status="trashed", completed_at=time.time(), error=None)
        except Exception as error:
            record.update(status="needs_attention", error=str(error)[:500])
            with self.queue.db:
                self._save(record)
            self.queue.write_status()
            raise WorkflowError("The Trash move did not finish. Processing is stopped and the files are recoverable. " + str(error)) from error
        with self.queue.db:
            self._save(record)
        self.queue.write_status()
        return record
