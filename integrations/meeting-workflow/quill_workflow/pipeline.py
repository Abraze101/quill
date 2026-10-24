"""Durable, resumable post-recording pipeline; never scans old recordings."""
import hashlib
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time

from quill_workflow.routing import (RoutingError, RoutingReviews, preserves_company_map, transcript_identity,
                                    validate_companies)
from quill_workflow.meeting_title import meeting_title
from quill_workflow.meeting_dates import recording_timezone
from quill_workflow.recording_safety import FILENAME, RecordingSafetyHold, read_safety, require_approved

STAGES = ("archive", "notes", "publish")
ALL_STAGES = STAGES + ("route",)
MAX_ATTEMPTS = 6
SCHEMA = """
CREATE TABLE IF NOT EXISTS local_deletions (job TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, session TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
 job TEXT NOT NULL REFERENCES jobs(id), stage TEXT NOT NULL,
 status TEXT NOT NULL, settings TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, error TEXT, receipt TEXT,
 PRIMARY KEY(job, stage)
);
CREATE TABLE IF NOT EXISTS recording_safety_bindings (
 job TEXT PRIMARY KEY REFERENCES jobs(id), session_id TEXT,
 device INTEGER NOT NULL, inode INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recording_identities (
 job TEXT PRIMARY KEY REFERENCES jobs(id), identity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_names (
 job TEXT PRIMARY KEY REFERENCES jobs(id), settings TEXT NOT NULL,
 destination TEXT, device INTEGER NOT NULL, inode INTEGER NOT NULL,
 status TEXT NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS local_name_aliases (
 job TEXT NOT NULL REFERENCES jobs(id), path TEXT NOT NULL,
 PRIMARY KEY(job,path)
);
"""


class WorkflowError(Exception):
    pass


class RecoverableUploadError(WorkflowError):
    """Transient media failure with a durable, validated reserved Drive file ID.

    Only the resumable upload boundary can establish that another attempt will
    continue the same remote file. Other connector errors keep their retry cap.
    """


class NeedsConfiguration(WorkflowError):
    pass


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".quill-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, limit=20_000_000):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise WorkflowError("Expected a bounded, regular JSON file: " + path.name)
    try:
        with path.open() as handle:
            return json.load(handle)
    except (ValueError, OSError) as error:
        raise WorkflowError("Cannot read " + path.name) from error


def load_config(path):
    if not Path(path).exists():
        return {"recordings_root": str(Path.home() / "Recordings")}
    config = read_json(path, 100_000)
    if not isinstance(config, dict):
        raise WorkflowError("Workflow config must be an object")
    for stage in ALL_STAGES:
        settings = config.get(stage, {})
        if not isinstance(settings, dict) or not isinstance(settings.get("enabled", False), bool):
            raise WorkflowError(stage + " settings must contain a boolean enabled value")
    if "route" in config:
        route_settings(config["route"])
    automatic_calendar_suggestions_since(config)
    from quill_workflow.local_naming import enabled
    enabled(config)
    return config


def automatic_calendar_suggestions_since(config):
    """Return the explicit activation boundary, never an inferred backfill date."""
    settings = config.get("sharing", {})
    if not isinstance(settings, dict):
        raise WorkflowError("sharing settings must be an object")
    enabled = settings.get("automatic_calendar_suggestions", False)
    if type(enabled) is not bool:
        raise WorkflowError("sharing.automatic_calendar_suggestions must be a boolean")
    since = settings.get("automatic_calendar_suggestions_since")
    if since is not None and (type(since) not in (int, float) or not math.isfinite(since) or since <= 0):
        raise WorkflowError("Automatic calendar suggestions need a positive finite activation timestamp")
    if enabled and since is None:
        raise WorkflowError("Set an explicit activation timestamp before enabling automatic calendar suggestions")
    return float(since) if enabled else None


def route_settings(settings):
    """Validate and freeze only user-configured destination mappings."""
    try:
        companies = validate_companies(settings.get("companies"))
    except RoutingError as error:
        raise NeedsConfiguration(str(error)) from error
    if type(settings.get("review_required", True)) is not bool:
        raise WorkflowError("route.review_required must be a boolean")
    if type(settings.get("auto_file_explicit_companies", False)) is not bool:
        raise WorkflowError("route.auto_file_explicit_companies must be a boolean")
    return dict(settings, companies=companies, review_required=settings.get("review_required", True),
                auto_file_explicit_companies=settings.get("auto_file_explicit_companies", False))


def large_files_configured(settings, stage):
    value = settings.get("large_files")
    if value is None:
        return True
    return (stage == "archive" and isinstance(value, dict)
            and set(value) == {"type", "credentials_file"}
            and value.get("type") == "google_drive_resumable"
            and isinstance(value.get("credentials_file"), str)
            and Path(value["credentials_file"]).is_absolute())


def configured(settings, stage):
    if settings.get("enabled") is not True:
        return False
    if settings.get("type") == "codex_drive":
        root = settings.get("meetings_root_id")
        return (stage in ("archive", "publish") and isinstance(root, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{10,200}", root) is not None
                and ("codex" not in settings or isinstance(settings["codex"], str)
                     and bool(settings["codex"].strip()))
                and (settings.get("model") is None or isinstance(settings["model"], str)
                     and bool(settings["model"].strip()))
                and settings.get("text_format", "legacy") in ("legacy", "combined_google_doc_v1", "native_template_google_doc_v1")
                and (settings.get("text_format") != "native_template_google_doc_v1" or stage != "publish"
                     or isinstance(settings.get("template_document_id"), str)
                     and re.fullmatch(r"[A-Za-z0-9_-]{10,200}", settings["template_document_id"]) is not None
                     and isinstance(settings.get("template_sha256"), str)
                     and re.fullmatch(r"[a-f0-9]{64}", settings["template_sha256"]) is not None)
                and settings.get("notes_layout", "sources") in ("sources", "clean")
                and large_files_configured(settings, stage)
                and ("document_timezone" not in settings
                     or isinstance(settings["document_timezone"], str)
                     and bool(settings["document_timezone"].strip())))
    if settings.get("type") == "rclone" and stage in ("archive", "publish"):
        return bool(settings.get("remote") and settings.get("folder"))
    command = settings.get("command")
    return (isinstance(command, list) and bool(command)
            and all(isinstance(part, str) and part for part in command))


def safe_member(directory, name):
    if not isinstance(name, str) or name in ("", ".", "..") or Path(name).name != name:
        raise WorkflowError("Session members must be plain filenames")
    path = directory / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise WorkflowError("Missing, empty, or linked session file: " + name)
    return path


def snapshot_session(session, recordings_root, destination):
    """Copy only explicitly completed session files; leave originals untouched."""
    raw = Path(session).expanduser()
    if raw.is_symlink():
        raise WorkflowError("A session cannot be a symlink")
    session = raw.resolve(strict=True)
    root = Path(recordings_root).expanduser().resolve(strict=True)
    if session.parent != root or not session.is_dir():
        raise WorkflowError("Session must be a direct child of the configured recordings root")
    read_safety(session)
    metadata = read_json(safe_member(session, "meta.json"), 100_000)
    if not isinstance(metadata, dict) or not metadata.get("ended") or not metadata.get("started"):
        raise WorkflowError("Session has no completed recording metadata")
    files = metadata.get("files")
    if not isinstance(files, dict) or not files.get("mic") or not files.get("system"):
        raise WorkflowError("Both microphone and system tracks must be finalized")
    names = ["meta.json", files["mic"], files["system"]]
    # Optional video must be finalized and copied into this same session first.
    if files.get("screen"):
        names.append(files["screen"])
    for name in ("transcript.json", "transcript.md"):
        if (session / name).exists():
            names.append(name)
    members = [safe_member(session, name) for name in dict.fromkeys(names)]
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for member in members:
        read_safety(session)
        before = member.stat()
        digest = hashlib.sha256()
        # O_NOFOLLOW closes the symlink race between checking and opening.
        fd = os.open(member, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source, (destination / member.name).open("xb") as target:
            if os.fstat(source.fileno()).st_ino != before.st_ino:
                raise WorkflowError("Session changed while being queued")
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
            after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise WorkflowError("Session is still changing; queue after finalization")
        manifest[member.name] = {"sha256": digest.hexdigest(), "size": before.st_size}
    read_safety(session)
    atomic_json(destination / "manifest.json", manifest)
    return session, manifest


class Queue:
    def __init__(self, state):
        self.state = Path(state).expanduser().resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.state / "jobs.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def require_processing(self, job):
        """Check the live original, following only recorded local-name moves."""
        if self.db.execute("SELECT 1 FROM local_deletions WHERE job=?", (job,)).fetchone():
            raise RecordingSafetyHold("This local meeting was deleted; processing stays stopped")
        from quill_workflow.local_naming import session_path
        from quill_workflow.recording_identity import remember_identity, verify_directory
        try:
            session = session_path(self, job)
        except (WorkflowError, OSError) as error:
            # A missing or changed named folder blocks this meeting, not every
            # later completion when the worker reconciles its saved queue.
            # Keep the original identity binding; never silently trust a new one.
            raise RecordingSafetyHold("Original recording location needs review: " + str(error)) from error
        binding = self.db.execute("SELECT * FROM recording_safety_bindings WHERE job=?", (job,)).fetchone()
        marker_exists = os.path.lexists(session / FILENAME)
        if binding or marker_exists:
            if session.is_symlink() or not session.is_dir():
                raise RecordingSafetyHold("Original recording is unavailable; processing stays local")
            identity = session.stat()
            if binding:
                identity = verify_directory(self, job, session, binding["device"], binding["inode"])
            # Remember presence even if the journal is malformed or not yet
            # approved. Deleting it later must not create a legacy exemption.
            if not binding:
                active_transaction = self.db.in_transaction
                self.db.execute("INSERT INTO recording_safety_bindings VALUES (?,NULL,?,?)",
                                (job, identity.st_dev, identity.st_ino))
                if not active_transaction:
                    self.db.commit()
            data = read_safety(session, required=True,
                               expected_session_id=binding["session_id"] if binding else None, require_approval=False)
            if not binding or binding["session_id"] is None:
                active_transaction = self.db.in_transaction
                self.db.execute("UPDATE recording_safety_bindings SET session_id=? WHERE job=?",
                                (data["session_id"], job))
                if not active_transaction:
                    self.db.commit()
            require_approved(data, session)
            try:
                remember_identity(self, job, session, data)
            except (WorkflowError, OSError, ValueError, TypeError) as error:
                raise RecordingSafetyHold("Original recording identity needs review: " + str(error)) from error
            return data
        return None

    def reconcile_safety(self):
        """Withdraw queued work, or resume only this distinct safety hold."""
        jobs = self.db.execute("SELECT DISTINCT job FROM tasks WHERE status IN ('pending','retry','safety_held')").fetchall()
        for row in jobs:
            try:
                self.require_processing(row["job"])
            except RecordingSafetyHold as error:
                with self.db:
                    self.db.execute("UPDATE tasks SET status='safety_held',error=? WHERE job=? "
                                    "AND status IN ('pending','retry','safety_held')", (str(error), row["job"]))
            else:
                with self.db:
                    self.db.execute("UPDATE tasks SET status='retry',error=NULL WHERE job=? AND status='safety_held'",
                                    (row["job"],))

    def enqueue(self, session, config, now=None):
        from quill_workflow.local_deletion import MARKER
        if os.path.lexists(Path(session).expanduser() / MARKER):
            raise WorkflowError("This local meeting was deleted; completion cannot enqueue it again")
        read_safety(Path(session).expanduser())
        # Relocation and repeated hooks share the same database lock. Keep it
        # through snapshotting so a completed folder cannot move mid-copy.
        from quill_workflow.local_naming import relocated_job
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = relocated_job(self, session, config.get("recordings_root", str(Path.home() / "Recordings")))
            if previous:
                self.db.rollback()
                return previous
            info = Path(session).expanduser().stat()
            for row in self.db.execute("SELECT record FROM local_deletions"):
                tree = json.loads(row[0])["snapshot"]["trees"][0]
                if (info.st_dev, info.st_ino) == (tree["device"], tree["inode"]):
                    raise WorkflowError("This local meeting is being deleted; completion cannot enqueue it again")
            return self._enqueue_locked(session, config, now)
        except BaseException:
            self.db.rollback()
            raise

    def _enqueue_locked(self, session, config, now=None):
        now = time.time() if now is None else now
        routing = route_settings(config["route"]) if "route" in config else None
        staging_root = self.state / "staging"
        staging_root.mkdir(exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="enqueue-", dir=staging_root)) / "recording"
        try:
            session, manifest = snapshot_session(
                session, config.get("recordings_root", str(Path.home() / "Recordings")), temporary)
            identity = str(session) + "\n" + json.dumps(manifest, sort_keys=True)
            job = hashlib.sha256(identity.encode()).hexdigest()[:24]
            spool = self.state / "recordings" / job
            spool.parent.mkdir(exist_ok=True)
            # Serialize both publication of the immutable snapshot and rows.
            try:
                if self.db.execute("SELECT id FROM jobs WHERE id=?", (job,)).fetchone():
                    relocation = self.db.execute("SELECT device,inode FROM local_names WHERE job=? AND destination IS NOT NULL",
                                                  (job,)).fetchone()
                    if relocation and (session.stat().st_dev, session.stat().st_ino) != tuple(relocation):
                        raise WorkflowError("The original path was replaced after local naming; review is required")
                    self.require_processing(job)
                    self.db.rollback()
                    return job
                if spool.exists():
                    if read_json(spool / "manifest.json") != manifest:
                        raise WorkflowError("Existing queue snapshot does not match its identity")
                else:
                    os.replace(temporary, spool)
                try:
                    date_zone = recording_timezone(read_json(spool / "meta.json", 100_000))
                except ValueError as error:
                    raise WorkflowError(str(error)) from error
                self.db.execute("INSERT INTO jobs VALUES (?, ?, ?)", (job, str(session), now))
                self.require_processing(job)
                for stage in (ALL_STAGES if routing is not None else STAGES):
                    settings = dict(routing if stage == "route" else config.get(stage, {}))
                    if stage in ("archive", "publish"):
                        # Managed from recording metadata, not a mutable config
                        # default. Both folder and document use this snapshot.
                        settings.pop("meeting_timezone", None)
                        if date_zone is not None:
                            settings["meeting_timezone"] = date_zone
                    if routing is not None and stage in ("archive", "publish"):
                        settings["requires_route"] = True
                    status = "pending" if configured(settings, stage) else "held"
                    self.db.execute(
                        "INSERT INTO tasks(job,stage,status,settings) VALUES (?,?,?,?)",
                        (job, stage, status, json.dumps(settings, sort_keys=True)))
                from quill_workflow.local_naming import enabled, request_naming
                if enabled(config):
                    request_naming(self, job, config)
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            return job
        finally:
            # Only the private temporary directory created above is removed.
            shutil.rmtree(temporary.parent, ignore_errors=True)

    def tasks(self, job=None):
        if job:
            rows = self.db.execute("SELECT * FROM tasks WHERE job=? ORDER BY stage", (job,))
        else:
            rows = self.db.execute("SELECT * FROM tasks ORDER BY job,stage")
        return [dict(row) for row in rows]

    def release(self, job, stage, config):
        self.require_processing(job)
        stored = self.db.execute("SELECT settings FROM tasks WHERE job=? AND stage=?", (job, stage)).fetchone()
        if not stored:
            raise WorkflowError("Unknown task; release cannot add stages to legacy jobs")
        previous = json.loads(stored["settings"])
        settings = dict(config.get(stage, {}))
        if stage in ("archive", "publish"):
            # A normal retry/release is not a content-format migration. Preserve
            # the exact frozen presence/value, including absent legacy fields,
            # so old upload inventories and receipts remain resumable after a
            # new default format or timezone is configured for future meetings.
            for key in ("text_format", "document_timezone", "meeting_timezone", "notes_layout", "template_document_id", "template_sha256"):
                if key in previous:
                    settings[key] = previous[key]
                else:
                    settings.pop(key, None)
        frozen_route = self.db.execute("SELECT settings FROM tasks WHERE job=? AND stage='route'", (job,)).fetchone()
        if frozen_route:
            frozen_route = route_settings(json.loads(frozen_route["settings"]))
            if "route" in config and not preserves_company_map(
                    route_settings(config["route"])["companies"], frozen_route["companies"]):
                raise NeedsConfiguration("Company folders changed; create an explicit revision and a new routing review")
            if stage == "route":
                settings.update(companies=frozen_route["companies"], review_required=frozen_route["review_required"],
                                auto_file_explicit_companies=frozen_route["auto_file_explicit_companies"])
        if stage in ("archive", "publish") and (previous.get("requires_route") is True or frozen_route):
            settings["requires_route"] = True
        if not configured(settings, stage):
            raise NeedsConfiguration("Configure and enable " + stage + " before releasing a job")
        with self.db:
            changed = self.db.execute(
                "UPDATE tasks SET status='pending',settings=?,attempts=0,next_attempt=0,error=NULL "
                "WHERE job=? AND stage=? AND status IN ('held','failed','safety_held')",
                (json.dumps(settings, sort_keys=True), job, stage)).rowcount
        if not changed:
            raise WorkflowError("No held or failed task matched; completed tasks cannot be released again")

    def attach_transcript(self, job, transcript_path):
        """Explicit repair of a raw-only job. Does not release or rerun a stage."""
        row = self.db.execute("SELECT session FROM jobs WHERE id=?", (job,)).fetchone()
        if not row:
            raise WorkflowError("Unknown job")
        self.require_processing(job)
        from quill_workflow.local_naming import session_path
        session = session_path(self, job)
        source = Path(transcript_path).expanduser()
        if source.is_symlink() or source.resolve() != (session / "transcript.json").resolve():
            raise WorkflowError("Attach the transcript from this job's original session directory")
        transcript = read_json(source)
        if not isinstance(transcript, dict) or not isinstance(transcript.get("segments"), list) or not transcript["segments"]:
            raise WorkflowError("A nonempty completed transcript is required")
        snapshot = self.state / "recordings" / job
        manifest = read_json(snapshot / "manifest.json")
        # Verify provenance before accepting a later transcript. This only
        # hashes the explicitly selected job's source files; it never decodes audio.
        for name, expected in manifest.items():
            if name.startswith("transcript."):
                continue
            member = safe_member(session, name)
            digest = hashlib.sha256()
            with member.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if member.stat().st_size != expected["size"] or digest.hexdigest() != expected["sha256"]:
                raise WorkflowError("Original recording changed; transcript provenance needs review")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            notes = self.db.execute("SELECT status FROM tasks WHERE job=? AND stage='notes'", (job,)).fetchone()
            if not notes or notes["status"] not in ("held", "failed"):
                raise WorkflowError("Only a held or failed notes job can receive a transcript")
            target = self.state / "results" / job / "input/transcript.json"
            if target.exists():
                if read_json(target) != transcript:
                    raise WorkflowError("A different transcript is already attached; create an explicit revision")
            else:
                atomic_json(target, transcript)
            self.db.execute("UPDATE tasks SET status='held',error='Transcript attached; release required' "
                            "WHERE job=? AND stage='notes'", (job,))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        self.write_status()

    def recover(self, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("UPDATE tasks SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'retry' END,"
                            "next_attempt=?,error='Worker interrupted' WHERE status='running'", (MAX_ATTEMPTS, now))
        self.reconcile_safety()

    def retry_failed(self, job, revision, config):
        """Explicitly retry one unchanged meeting without releasing any hold."""
        from quill_workflow.queue_status import retry_revision
        self.db.execute("BEGIN IMMEDIATE")
        try:
            tasks = self.tasks(job)
            if not tasks or retry_revision(tasks) != revision:
                raise WorkflowError("Processing changed; reload the meeting review before retrying")
            if any(task["status"] in ("running", "safety_held") for task in tasks):
                raise WorkflowError("This meeting is running or needs a recording review; it cannot be retried here")
            self.require_processing(job)
            if (self.state / "reviews.sqlite3").exists():
                reviews = RoutingReviews(self.state)
                try:
                    review = reviews.get(job)
                finally:
                    reviews.close()
                if review and review["status"] == "skipped":
                    raise WorkflowError("This meeting was kept local; retry cannot release it")
            selected = [task for task in tasks if task["status"] in ("failed", "retry") and task["receipt"] is None]
            if not selected:
                raise WorkflowError("No failed processing matched this meeting")
            for task in selected:
                stage, frozen = task["stage"], json.loads(task["settings"])
                current = config.get(stage, {})
                if not configured(current, stage) or not configured(frozen, stage):
                    raise NeedsConfiguration("Configure and enable " + stage + " before retrying")
                # Repairing a connection never silently selects a different
                # destination or opts an old meeting into new processing.
                for key in ("type", "remote", "folder", "meetings_root_id", "requires_route"):
                    if key in frozen and key in current and frozen[key] != current[key]:
                        raise NeedsConfiguration("Processing destination changed; review this meeting before retrying")
                route = next((item for item in tasks if item["stage"] == "route"), None)
                if route:
                    frozen_route = route_settings(json.loads(route["settings"]))
                    if (not configured(config.get("route", {}), "route")
                            or not preserves_company_map(route_settings(config["route"])["companies"],
                                                         frozen_route["companies"])):
                        raise NeedsConfiguration("Company routing changed or is disabled; review it before retrying")
                self.db.execute("UPDATE tasks SET status='pending',attempts=0,next_attempt=0,error=NULL "
                                "WHERE job=? AND stage=? AND status IN ('failed','retry') AND receipt IS NULL",
                                (job, stage))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        self.write_status()
        return [task["stage"] for task in selected]

    def run_ready(self, runner, now=None, enabled=None, on_completed=None):
        now = time.time() if now is None else now
        def refresh_enabled():
            current = enabled() if callable(enabled) else enabled
            if current is None:
                return None
            # Hold disabled stages even during retry backoff. Otherwise the
            # worker keeps polling until their old retry deadlines arrive.
            with self.db:
                self.db.executemany(
                    "UPDATE tasks SET status='held',error='Stage disabled in current configuration' "
                    "WHERE stage=? AND status IN ('pending','retry','safety_held')",
                    ((stage,) for stage in ALL_STAGES if current.get(stage) is not True))
            return current

        refresh_enabled()
        self.reconcile_safety()
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status IN ('pending','retry') AND next_attempt<=? "
            "ORDER BY CASE stage WHEN 'notes' THEN 0 WHEN 'route' THEN 1 WHEN 'archive' THEN 2 ELSE 3 END,job", (now,)).fetchall()
        processed = 0
        for row in rows:
            row = dict(row)
            # A previous connector can take minutes. Re-read settings before
            # claiming the next stage, never unholding withdrawn work.
            current = refresh_enabled()
            if current is not None and current.get(row["stage"]) is not True:
                with self.db:
                    self.db.execute("UPDATE tasks SET status='held',error='Stage disabled in current configuration' "
                                    "WHERE job=? AND stage=? AND status IN ('pending','retry')",
                                    (row["job"], row["stage"]))
                continue
            dependencies = self.dependencies(row)
            if "route" in dependencies and current is not None and current.get("route") is not True:
                with self.db:
                    self.db.execute("UPDATE tasks SET status='held',error='Required routing is disabled' "
                                    "WHERE job=? AND stage=? AND status IN ('pending','retry')", (row["job"], row["stage"]))
                continue
            dependency_states = [self.db.execute("SELECT status FROM tasks WHERE job=? AND stage=?",
                                                 (row["job"], dependency)).fetchone() for dependency in dependencies]
            if any(state is None or state[0] != "succeeded" for state in dependency_states):
                continue
            with self.db:
                changed = self.db.execute(
                    "UPDATE tasks SET status='running',attempts=attempts+1 WHERE job=? AND stage=? "
                    "AND status IN ('pending','retry')", (row["job"], row["stage"])).rowcount
            if not changed:
                continue
            processed += 1
            # A scoped Retry can reset an unclaimed task while another
            # meeting's connector runs. Use the committed claim's counter.
            attempt = self.db.execute("SELECT attempts FROM tasks WHERE job=? AND stage=?",
                                      (row["job"], row["stage"])).fetchone()[0]
            self.write_status()
            leases = ExitStack()
            try:
                from .local_deletion import job_lock
                leases.enter_context(job_lock(self.state, row["job"]))
                self.require_processing(row["job"])
                receipt = runner(row["stage"], row["job"], json.loads(row["settings"]))
                if not isinstance(receipt, dict) or receipt.get("job") != row["job"] or receipt.get("stage") != row["stage"]:
                    raise WorkflowError("Connector did not return a matching completion receipt")
                with self.db:
                    self.db.execute("UPDATE tasks SET status=CASE WHEN status='deleted' THEN 'deleted' ELSE 'succeeded' END,receipt=?,error=NULL "
                                    "WHERE job=? AND stage=?", (json.dumps(receipt), row["job"], row["stage"]))
            except RecordingSafetyHold as error:
                with self.db:
                    self.db.execute("UPDATE tasks SET status='safety_held',error=? WHERE job=? AND stage=? AND status<>'deleted'",
                                    (str(error), row["job"], row["stage"]))
            except NeedsConfiguration as error:
                with self.db:
                    self.db.execute("UPDATE tasks SET status='held',error=? WHERE job=? AND stage=? AND status<>'deleted'",
                                    (str(error), row["job"], row["stage"]))
            except Exception as error:
                recoverable_upload = row["stage"] == "archive" and isinstance(error, RecoverableUploadError)
                failed = attempt >= MAX_ATTEMPTS and not recoverable_upload
                # Reserved-ID media uploads can outlast an offline period.
                # Clamp before exponentiation even after arbitrarily many retries.
                delay = min(3600, 30 * (2 ** min(7, max(0, attempt - 1))))
                with self.db:
                    self.db.execute("UPDATE tasks SET status=?,next_attempt=?,error=? WHERE job=? AND stage=? AND status<>'deleted'",
                                    ("failed" if failed else "retry", now + delay,
                                     str(error)[:500], row["job"], row["stage"]))
            else:
                # Completion observers run only after the receipt commits.
                # A failed optional follow-up must never undo successful
                # filing or cause the connector to upload a second copy.
                if on_completed is not None and not self.db.execute("SELECT 1 FROM local_deletions WHERE job=?", (row["job"],)).fetchone():
                    try:
                        on_completed(row["stage"], row["job"], dict(receipt))
                    except Exception:
                        # A durable observer records its own failed attempt;
                        # later startup reconciliation can recover a missed
                        # dispatch without reopening this completed stage.
                        pass
            finally:
                leases.close()
            self.write_status()
        refresh_enabled()
        self.write_status()
        return processed

    def write_status(self):
        from quill_workflow.queue_status import compact_status
        counts = dict(self.db.execute("SELECT status,COUNT(*) FROM tasks GROUP BY status"))
        pending_reviews = 0
        if (self.state / "reviews.sqlite3").exists():
            reviews = RoutingReviews(self.state)
            try:
                pending_reviews = len(reviews.pending())
            finally:
                reviews.close()
        atomic_json(self.state / "status.json", dict(compact_status(self), **{"updated": time.time(), "tasks": counts,
                                                "pending_reviews": pending_reviews,
                                                "local_naming": dict(self.db.execute(
                                                    "SELECT status,COUNT(*) FROM local_names GROUP BY status"))}))

    def dependencies(self, task):
        dependencies = ["notes"] if task["stage"] in ("publish", "route") else []
        if task["stage"] in ("archive", "publish"):
            settings = json.loads(task["settings"])
            if settings.get("requires_route") is True or self.db.execute(
                    "SELECT 1 FROM tasks WHERE job=? AND stage='route'", (task["job"],)).fetchone():
                dependencies.append("route")
        return dependencies

    def has_work(self):
        tasks = {(task["job"], task["stage"]): task for task in self.tasks()}

        def possible(task, seen):
            key = (task["job"], task["stage"])
            if key in seen or task["status"] not in ("pending", "retry", "running", "succeeded"):
                return False
            if task["status"] in ("running", "succeeded"):
                return True
            for dependency in self.dependencies(task):
                target = tasks.get((task["job"], dependency))
                if target is None or not possible(target, seen | {key}):
                    return False
            return True

        return any(task["status"] in ("pending", "retry", "running") and possible(task, set())
                   for task in tasks.values())


def _render_historical_notes(data, transcript, include_sources=False):
    """Validate evidence and preserve both historical Markdown formats exactly."""
    from .capture_quality import notice, validate_note_notice
    try:
        validate_note_notice(data, transcript)
        capture_notice = notice(transcript)
    except ValueError as error:
        raise WorkflowError(str(error)) from error
    segments = transcript.get("segments", [])
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise WorkflowError("Notes must use schema_version 1")
    if not isinstance(segments, list) or not segments:
        raise WorkflowError("Transcript has no segments; do not generate notes from empty audio")
    try:
        display_title = meeting_title(data, transcript)
    except ValueError as error:
        raise WorkflowError(str(error)) from error
    # Escape Markdown punctuation in names/topics while keeping Drive names plain.
    heading = re.sub(r"([\\`*_\[\]()#])", r"\\\1", display_title) if display_title else "Meeting notes"
    lines = ["# " + heading, ""]
    if capture_notice:
        lines.extend([capture_notice, ""])
    sections = [("summary", "Summary"), ("topics", "Discussion points"),
                ("customer_priorities", "Customer priorities"), ("objections", "Concerns and objections"),
                ("competitive_intel", "Competitor mentions"), ("decisions", "Decisions"),
                ("next_steps", "Agreed next steps"), ("deal_implications", "Deal implications (assessment)"),
                ("open_questions", "Open questions")]

    def evidence(item):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
            raise WorkflowError("Each finding needs text")
        refs = item.get("evidence")
        if not isinstance(refs, list) or not refs:
            raise WorkflowError("Every finding needs transcript segment references")
        labels = []
        for index in refs:
            if type(index) is not int or not 0 <= index < len(segments):
                raise WorkflowError("Invalid transcript segment reference")
            segment = segments[index]
            start = segment.get("start_ms")
            if type(start) is not int or start < 0:
                raise WorkflowError("Transcript segment has no valid timestamp")
            seconds = start // 1000
            labels.append(f"{seconds // 60:02d}:{seconds % 60:02d}, segment {index}")
        return "; ".join(labels)

    if display_title:
        title = data["meeting_title"]
        refs = evidence(title)
        if include_sources:
            lines.extend(["Title source: " + refs, ""])
        if title["participants"]:
            lines.extend(["Participants identified in the transcript:", ""])
            for person in title["participants"]:
                refs = evidence({"text": person["name"], "evidence": person["evidence"]})
                lines.append("- " + person["name"] + (" (" + refs + ")" if include_sources else ""))
            lines.append("")

    for key, title in sections:
        items = data.get(key, [])
        if not isinstance(items, list):
            raise WorkflowError(key + " must be an array")
        if items:
            lines.extend(["## " + title, ""])
            for item in items:
                refs = evidence(item)
                lines.append("- " + item["text"] + (" (" + refs + ")" if include_sources else ""))
            lines.append("")
    actions = data.get("actions", [])
    if not isinstance(actions, list):
        raise WorkflowError("actions must be an array")
    if actions:
        lines.extend(["## Action items", ""])
        for item in actions:
            refs = evidence(item)
            for key in ("owner", "due"):
                if key not in item or (item[key] is not None and not isinstance(item[key], str)):
                    raise WorkflowError("Action owner and due must be strings or null")
            lines.append("- [ ] " + item["text"] + " — Owner: " + (item["owner"] or "Unresolved")
                         + "; due: " + (item["due"] or "Not stated")
                         + (" (" + refs + ")" if include_sources else ""))
        lines.append("")
    if not any(data.get(key) for key, _ in sections) and not actions:
        raise WorkflowError("Notes contain no findings")
    draft = data.get("follow_up_draft")
    if draft is not None:
        if not isinstance(draft, dict) or not all(isinstance(draft.get(key), str) and draft[key].strip()
                                                for key in ("subject", "body")):
            raise WorkflowError("Follow-up draft needs a subject and body")
        refs = evidence({"text": draft["body"], "evidence": draft.get("evidence")})
        lines.extend(["## Customer follow-up draft", "", "Subject: " + draft["subject"], "",
                      draft["body"], ""])
        if include_sources:
            lines.extend(["Source: " + refs, ""])
    return "\n".join(lines)


def render_notes(data, transcript, include_sources=False):
    """Render the meeting template after checking all evidence, including hidden refs."""
    historical = _render_historical_notes(data, transcript, include_sources=include_sources)
    if include_sources:
        return historical
    # The native document uses these same sections. Import here so this module
    # remains available to the native builder's evidence-validation step.
    from quill_workflow.native_template import note_sections
    from quill_workflow.capture_quality import notice
    sections = note_sections(data)

    def escaped(value):
        return re.sub(r"([\\`*_\[\]()#])", r"\\\1", value)

    title = meeting_title(data, transcript) or "Meeting notes"
    lines = ["# " + escaped(title), ""]
    capture_notice = notice(transcript)
    if capture_notice:
        lines.extend([capture_notice, ""])
    participants = data.get("meeting_title", {}).get("participants", [])
    if participants:
        lines.extend(["Participants: " + ", ".join(escaped(person["name"]) for person in participants), ""])
    lines.extend(["## Summary", "", sections["SUMMARY"], "", "## Decisions", ""])
    if data.get("decisions"):
        lines.extend(["**Aligned**", ""])
        lines.extend("- " + line for line in sections["DECISIONS"].splitlines())
    else:
        lines.append(sections["DECISIONS"])
    lines.extend(["", "## Next steps", ""])
    if data.get("actions") or data.get("next_steps"):
        lines.extend("- " + line for line in sections["ACTIONS"].splitlines())
    else:
        lines.append(sections["ACTIONS"])
    lines.extend(["", "## Details", ""])
    for line in sections["DETAILS"].splitlines():
        label = re.match(r"^([^:\n]{1,100}):\s(.*)$", line)
        if label:
            lines.append("- **" + escaped(label[1]) + ":** " + label[2])
        else:
            # In particular, preserve every paragraph of a follow-up draft.
            lines.extend(["", line])
    lines.append("")
    return "\n".join(lines)


def notes_markdown_matches(markdown, data, transcript):
    """Accept only exact template, former clean, or legacy-source renderings."""
    return (markdown == render_notes(data, transcript)
            or markdown == _render_historical_notes(data, transcript, include_sources=False)
            or markdown == _render_historical_notes(data, transcript, include_sources=True))


class ConnectorRunner:
    def __init__(self, state, skill, lock_fd=None, config_path=None):
        self.state = Path(state)
        self.skill = str(Path(skill).resolve())
        self.lock_fd = lock_fd
        self.config_path = Path(config_path) if config_path is not None else None

    def require_processing(self, job):
        queue = Queue(self.state)
        try:
            return queue.require_processing(job)
        finally:
            queue.close()

    def cancelled(self, job):
        from .local_deletion import deleted_jobs
        return job in deleted_jobs(self.state)

    def execute(self, argv, cwd, timeout, job=None):
        job = job or getattr(self, "_active_job", None)
        from .processing_lease import child_lease_fds, adapter_environment
        try:
            process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True, env=adapter_environment(),
                                       pass_fds=child_lease_fds() + (() if self.lock_fd is None else (self.lock_fd,)))
        except FileNotFoundError as error:
            raise NeedsConfiguration("Configured connector executable is not installed") from error
        try:
            try:
                deadline = time.monotonic() + timeout
                while True:
                    if job:
                        self.require_processing(job)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WorkflowError("Connector timed out")
                    try:
                        process.wait(timeout=min(0.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except subprocess.TimeoutExpired as error:
                raise WorkflowError("Connector timed out") from error
            if process.returncode != 0:
                raise WorkflowError("Connector exited with status " + str(process.returncode))
        finally:
            # subprocess.run kills its immediate child on timeout or an
            # interrupt. Give our adapter time to unwind its own Codex process
            # group before escalating; retain the worker lock until cleanup.
            def signal_group(signum):
                try:
                    os.killpg(process.pid, signum)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # macOS can deny a signal during the exit/reap race.
                    if process.poll() is None:
                        raise

            signal_group(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            signal_group(signal.SIGKILL)
            process.wait()

    def task(self, job, stage):
        path = self.state / "jobs.sqlite3"
        if not path.exists():
            return None
        connection = sqlite3.connect(path)
        try:
            row = connection.execute("SELECT status,settings FROM tasks WHERE job=? AND stage=?",
                                     (job, stage)).fetchone()
            return {"status": row[0], "settings": json.loads(row[1])} if row else None
        finally:
            connection.close()

    def approved_route(self, job, transcript_path, frozen):
        self.require_processing(job)
        # Run before any external action or cached receipt is accepted.
        read_json(transcript_path)
        source = transcript_path.read_bytes()
        reviews = RoutingReviews(self.state)
        try:
            route = reviews.route(job, source, frozen["companies"])
        except RoutingError as error:
            raise NeedsConfiguration(str(error)) from error
        finally:
            reviews.close()
        if route is None:
            raise NeedsConfiguration("Choose this meeting's company in the routing review")
        return route, source

    @staticmethod
    def merge_external_receipt(receipt, returned):
        if (not isinstance(returned, dict)
                or any(returned.get(key) != receipt[key]
                       for key in ("job", "stage", "idempotency_key"))
                or not returned.get("destination")):
            raise WorkflowError("Connector must verify its destination and return the matching receipt")
        # A connector may add destination evidence, but cannot rewrite the
        # identities established by this worker before external work began.
        reserved = {"job", "stage", "idempotency_key", "settings_sha256",
                    "input_sha256", "route_sha256"}
        if any(key in returned and (key not in receipt or returned[key] != receipt[key])
               for key in reserved):
            raise WorkflowError("Connector returned conflicting reserved receipt fields")
        return dict(receipt, **returned)

    def run_codex_drive(self, job, stage, settings, source, recording, transcript_path, route):
        self.require_processing(job)
        # These adapters reuse the notes harness, which imports pipeline helpers.
        from quill_workflow.codex_drive_transport import CodexDriveTransport
        from quill_workflow.drive_upload import DriveUploader, UploadNeedsReview, UploadRetryable

        def guard():
            self.require_processing(job)
            if settings.get("large_files") and self.config_path is not None:
                live = load_config(self.config_path).get(stage, {})
                if live.get("enabled") is not True or live.get("large_files") != settings["large_files"]:
                    raise NeedsConfiguration("Direct Drive filing was disabled or its connection changed; the saved upload is on hold")
            current = self.task(job, "route")
            if (not current or current["status"] != "succeeded"
                    or current["settings"].get("enabled") is not True):
                raise NeedsConfiguration("This destination waits for its required company routing review")
            current_route, _ = self.approved_route(job, transcript_path, route_settings(current["settings"]))
            if current_route != route:
                raise NeedsConfiguration("Company routing changed before upload; a new review is required")

        guard()
        display_title = None
        result_dir = self.state / "results" / job
        notes_path = result_dir / "notes/notes.json"
        combined = settings.get("text_format") == "combined_google_doc_v1"
        native_template = settings.get("text_format") == "native_template_google_doc_v1"
        structured_document = combined or native_template
        document = None
        payload = None
        if structured_document and stage == "publish" and not notes_path.exists():
            raise NeedsConfiguration("Combined meeting document waits for validated notes")
        if notes_path.exists() or notes_path.is_symlink():
            notes = read_json(notes_path)
            if structured_document and stage == "publish" or isinstance(notes, dict) and "meeting_title" in notes:
                notes_task = self.task(job, "notes")
                notes_receipt = read_json(result_dir / "notes-receipt.json")
                provenance = read_json(result_dir / "notes/provenance.json")
                source_hash = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
                if (not notes_task or notes_task["status"] != "succeeded"
                        or notes_receipt.get("input_sha256") != source_hash
                        or any(provenance.get(key) != notes_receipt.get(key)
                               for key in ("input_sha256", "settings_sha256"))
                        or safe_member(result_dir / "notes", "source-transcript.json").read_bytes()
                        != transcript_path.read_bytes()):
                    raise NeedsConfiguration("Meeting title notes use different source or settings; an explicit revision is required")
                transcript = read_json(transcript_path)
                render_notes(notes, transcript)
                display_title = meeting_title(notes, transcript)
        metadata = read_json(safe_member(recording, "meta.json"), 100_000)
        started = metadata.get("started") if isinstance(metadata, dict) else None
        if not isinstance(started, str) or not started.strip():
            raise WorkflowError("Completed recording has no start timestamp")
        date_zone = settings.get("meeting_timezone")
        try:
            if date_zone != recording_timezone(metadata):
                raise ValueError("Saved meeting timezone differs from its recording")
        except ValueError as error:
            raise NeedsConfiguration(str(error)) from error
        document_zone = date_zone or settings.get("document_timezone", "UTC")
        if combined and stage == "publish":
            from quill_workflow.meeting_document import build_document
            try:
                document = build_document(notes, transcript, metadata, route,
                                          result_dir / "documents",
                                          document_zone,
                                          notes_layout=settings.get("notes_layout", "sources"))
            except ValueError as error:
                raise NeedsConfiguration(str(error)) from error
            # Only the explicit DOCX below is eligible for this inventory; the
            # result directory also holds local provenance and source snapshots.
            source = result_dir
        if native_template and stage == "publish":
            from quill_workflow.native_template import build_payload
            try:
                payload = build_payload(notes, transcript, metadata, route,
                                        document_zone)
            except ValueError as error:
                raise NeedsConfiguration(str(error)) from error
            source = result_dir
        transport = CodexDriveTransport(
            settings.get("codex", "/Applications/Codex.app/Contents/Resources/codex"),
            model=settings.get("model"),
            timeout=min(7200, max(1, int(settings.get("timeout_seconds", 180)))))
        transport.cancelled = lambda: self.cancelled(job)
        upload_options = {}
        if stage == "archive" and settings.get("large_files"):
            from quill_workflow.google_drive_auth import GoogleDriveAuth
            from quill_workflow.resumable_drive import ResumableDriveUploader
            auth = GoogleDriveAuth(settings["large_files"]["credentials_file"])
            upload_options = {"large_upload": ResumableDriveUploader(auth.access_token),
                              "large_upload_preflight": auth.preflight}
        uploader = DriveUploader(self.state, transport.invoke, settings["meetings_root_id"],
                                 guard=guard, **upload_options)
        try:
            options = {"meeting_title": display_title}
            if date_zone is not None:
                options["meeting_timezone"] = date_zone
            if combined:
                options["text_format"] = "combined_google_doc_v1"
            if native_template:
                options["text_format"] = "native_template_google_doc_v1"
                if stage == "publish":
                    options.update(template_document_id=settings["template_document_id"],
                                   template_sha256=settings["template_sha256"], template_payload=payload,
                                   document_title=payload["title"])
            if document:
                options.update(document_path=document["path"],
                               document_title=document["title"] + " - Notes and transcript",
                               document_text=document["text"])
            return uploader.upload(job, stage, source, route, started, **options)
        except UploadNeedsReview as error:
            raise NeedsConfiguration(str(error)) from error
        except RecoverableUploadError:
            # Preserve the reserved-ID guarantee for the queue's longer retry
            # policy; ordinary connector timeouts remain bounded below.
            raise
        except UploadRetryable as error:
            raise WorkflowError(str(error)) from error

    def run_route(self, job, settings, transcript_path, result_dir, receipt):
        self.require_processing(job)
        frozen_task = self.task(job, "route")
        if frozen_task is None:
            raise NeedsConfiguration("This job has no frozen company routing stage")
        frozen = route_settings(frozen_task["settings"])
        if route_settings(settings)["companies"] != frozen["companies"]:
            raise NeedsConfiguration("Company folders changed; a new routing review is required")
        notes_task = self.task(job, "notes")
        if not notes_task or notes_task["status"] != "succeeded":
            raise NeedsConfiguration("Routing waits for completed meeting notes")
        read_json(transcript_path)
        source = transcript_path.read_bytes()
        source_hash, _ = transcript_identity(source)
        notes_receipt = read_json(result_dir / "notes-receipt.json")
        if notes_receipt.get("input_sha256") != source_hash:
            raise NeedsConfiguration("Notes use a different transcript; an explicit revision is required")
        receipt["input_sha256"] = source_hash
        reviews = RoutingReviews(self.state)
        try:
            previous = reviews.get(job)
            if previous is None:
                staging = Path(tempfile.mkdtemp(prefix="route-", dir=result_dir))
                try:
                    companies_path = staging / "companies.json"
                    atomic_json(companies_path, frozen["companies"])
                    output = staging / "output.json"
                    values = {"input": str(transcript_path), "companies": str(companies_path),
                              "output": str(output), "job": job, "stage": "route",
                              "skill": self.skill, "idempotency_key": receipt["idempotency_key"]}
                    argv = [part.format_map(values) for part in settings["command"]]
                    self.require_processing(job)
                    self.execute(argv, staging, min(7200, max(1, int(settings.get("timeout_seconds", 1200)))))
                    if transcript_path.read_bytes() != source:
                        raise NeedsConfiguration("Transcript changed during routing; an explicit revision is required")
                    reviews.propose(job, source, frozen["companies"], read_json(output),
                                    review_required=frozen["review_required"],
                                    auto_file_explicit_companies=frozen["auto_file_explicit_companies"])
                finally:
                    shutil.rmtree(staging, ignore_errors=True)
            # A held, approved, or skipped unchanged review never calls the model again.
            route = reviews.route(job, source, frozen["companies"])
        except RoutingError as error:
            raise NeedsConfiguration(str(error)) from error
        finally:
            reviews.close()
        if route is None:
            raise NeedsConfiguration("Choose this meeting's company in the routing review")
        atomic_json(result_dir / "route.json", route)
        receipt["route"] = route
        atomic_json(result_dir / "route-receipt.json", receipt)
        return receipt

    def __call__(self, stage, job, settings):
        previous = getattr(self, "_active_job", None)
        self._active_job = job
        try:
            return self._call(stage, job, settings)
        finally:
            self._active_job = previous

    def _call(self, stage, job, settings):
        self.require_processing(job)
        if not configured(settings, stage):
            raise NeedsConfiguration(stage + " is disabled or incomplete")
        recording = self.state / "recordings" / job
        result_dir = self.state / "results" / job
        result_dir.mkdir(parents=True, exist_ok=True)
        signature = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        receipt = {"job": job, "stage": stage, "idempotency_key": job + ":" + stage,
                   "settings_sha256": signature}
        transcript_path = result_dir / "input/transcript.json"
        if not transcript_path.exists():
            transcript_path = recording / "transcript.json"
        if stage == "route":
            return self.run_route(job, settings, transcript_path, result_dir, receipt)
        route = None
        if stage in ("archive", "publish"):
            own_task, routing_task = self.task(job, stage), self.task(job, "route")
            required = (settings.get("type") == "codex_drive"
                        or settings.get("requires_route") is True or routing_task is not None
                        or own_task and own_task["settings"].get("requires_route") is True)
            if required:
                if not routing_task or routing_task["status"] != "succeeded":
                    raise NeedsConfiguration("This destination waits for its required company routing review")
                frozen = route_settings(routing_task["settings"])
                if settings.get("type") == "codex_drive" and frozen.get("enabled") is not True:
                    raise NeedsConfiguration("This destination waits for its required company routing review")
                route, source = self.approved_route(job, transcript_path, frozen)
                receipt["input_sha256"] = hashlib.sha256(source).hexdigest()
                receipt["route_sha256"] = hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest()
                atomic_json(result_dir / "route.json", route)
        if stage == "notes":
            if not transcript_path.exists():
                raise NeedsConfiguration("No transcript is available; transcription approval is required")
            transcript = read_json(transcript_path)
            if not isinstance(transcript, dict) or not transcript.get("segments"):
                raise WorkflowError("Empty transcript; notes were not generated")
            receipt["input_sha256"] = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
        receipt_path = result_dir / (stage + "-receipt.json")
        if receipt_path.exists():
            previous = read_json(receipt_path)
            if all(previous.get(key) == value for key, value in receipt.items()):
                # Commit the same success after a crash; don't run the skill
                # or send the same completed output a second time.
                return previous
        if stage == "notes" and (result_dir / "notes/notes.json").exists():
            provenance = read_json(result_dir / "notes/provenance.json")
            if any(provenance.get(key) != receipt.get(key) for key in ("input_sha256", "settings_sha256")):
                raise NeedsConfiguration("Existing notes use different input or settings; an explicit revision is required")
            render_notes(read_json(result_dir / "notes/notes.json"), transcript)
            atomic_json(receipt_path, receipt)
            return receipt
        if settings.get("type") == "codex_drive":
            source = recording if stage == "archive" else result_dir / "notes"
            returned = self.run_codex_drive(job, stage, settings, source, recording, transcript_path, route)
            receipt = self.merge_external_receipt(receipt, returned)
            atomic_json(receipt_path, receipt)
            return receipt
        if settings.get("type") == "rclone":
            remote, folder = settings["remote"], settings["folder"]
            if not isinstance(remote, str) or not remote.replace("-", "").replace("_", "").isalnum():
                raise NeedsConfiguration("rclone remote must be a configured remote name")
            if not isinstance(folder, str) or not folder.strip("/") or ".." in folder.split("/"):
                raise NeedsConfiguration("Choose a non-root destination folder")
            source = recording if stage == "archive" else result_dir / "notes"
            if not source.is_dir():
                raise WorkflowError("No completed output exists for this stage")
            target = remote + ":" + folder.strip("/") + "/" + job + "/" + stage
            command = [settings.get("executable", "rclone"), "copy", str(source), target,
                       "--immutable", "--checksum", "--retries", "1", "--low-level-retries", "3"]
            drive_folder_id = route["drive_folder_id"] if route else settings.get("drive_folder_id")
            if drive_folder_id:
                command.extend(["--drive-root-folder-id", drive_folder_id])
            self.require_processing(job)
            self.execute(command, result_dir, 7200)
            receipt["destination"] = target
            atomic_json(result_dir / (stage + "-receipt.json"), receipt)
            return receipt

        staging = Path(tempfile.mkdtemp(prefix=stage + "-", dir=result_dir))
        try:
            output = staging / "output.json"
            if stage == "notes":
                source = transcript_path
            elif stage == "publish":
                source = result_dir / "notes"
            else:
                source = recording
            values = {"input": str(source), "output": str(output), "skill": self.skill,
                      "idempotency_key": receipt["idempotency_key"], "job": job,
                      "stage": stage}
            if route:
                values.update(route=str(result_dir / "route.json"), drive_folder_id=route["drive_folder_id"])
            argv = [part.format_map(values) for part in settings["command"]]
            self.require_processing(job)
            self.execute(argv, staging, min(7200, max(1, int(settings.get("timeout_seconds", 1200)))))
            returned = read_json(output)
            if stage == "notes":
                markdown = render_notes(returned, transcript)
                notes = staging / "notes"
                notes.mkdir()
                atomic_json(notes / "notes.json", returned)
                atomic_json(notes / "provenance.json", receipt)
                shutil.copyfile(transcript_path, notes / "source-transcript.json")
                (notes / "notes.md").write_text(markdown, encoding="utf-8")
                destination = result_dir / "notes"
                if destination.exists():
                    # A crash after publishing notes but before committing the
                    # receipt can reuse the already validated result.
                    existing = read_json(destination / "notes.json")
                    render_notes(existing, transcript)
                else:
                    os.replace(notes, destination)
            else:
                receipt = self.merge_external_receipt(receipt, returned)
            atomic_json(result_dir / (stage + "-receipt.json"), receipt)
            return receipt
        finally:
            shutil.rmtree(staging, ignore_errors=True)
