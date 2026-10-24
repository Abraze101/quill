"""Rename completed local sessions without changing their durable job identity."""
import ctypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

from .meeting_title import meeting_title
from .meeting_dates import recording_timezone
from .pipeline import WorkflowError, read_json, render_notes, safe_member


def enabled(config):
    settings = config.get("local_naming", {})
    if not isinstance(settings, dict) or type(settings.get("enabled", False)) is not bool:
        raise WorkflowError("local_naming.enabled must be a boolean")
    return settings.get("enabled") is True


def request_naming(queue, job, config):
    """Explicitly register one completed source, never enumerate recordings."""
    queue.require_processing(job)
    if not enabled(config):
        raise WorkflowError("Local meeting naming is disabled")
    row = queue.db.execute("SELECT session FROM jobs WHERE id=?", (job,)).fetchone()
    if not row:
        raise WorkflowError("Unknown meeting")
    if queue.db.execute("SELECT 1 FROM local_names WHERE job=?", (job,)).fetchone():
        return
    source = Path(row["session"])
    root = Path(config.get("recordings_root", str(Path.home() / "Recordings"))).expanduser().resolve()
    if source.is_symlink() or not source.is_dir() or source.resolve().parent != root:
        raise WorkflowError("Local naming requires the original recording under its configured root")
    identity = source.stat()
    settings = dict(config["local_naming"])
    settings.pop("meeting_timezone", None)
    try:
        zone = recording_timezone(read_json(queue.state / "recordings" / job / "meta.json", 100_000))
    except ValueError as error:
        raise WorkflowError(str(error)) from error
    if zone is not None:
        settings["meeting_timezone"] = zone
    queue.db.execute("INSERT INTO local_names(job,settings,device,inode,status) VALUES (?,?,?,?,?)",
                     (job, json.dumps(settings, sort_keys=True),
                      identity.st_dev, identity.st_ino, "pending"))


def _row(queue, job):
    row = queue.db.execute("SELECT n.*,j.session FROM local_names n JOIN jobs j ON j.id=n.job WHERE job=?",
                           (job,)).fetchone()
    if not row:
        raise WorkflowError("This meeting has not been registered for local naming")
    return dict(row)


def _directory(path, row, queue=None):
    if path.is_symlink() or not path.is_dir():
        raise WorkflowError("Local recording folder is missing or linked")
    st = path.stat()
    if queue is None:
        if (st.st_dev, st.st_ino) != (row["device"], row["inode"]):
            raise WorkflowError("Local recording folder was replaced; review its saved path")
    else:
        from .recording_identity import verify_directory
        from .recording_safety import RecordingSafetyHold
        try:
            verify_directory(queue, row["job"], path, row["device"], row["inode"])
        except RecordingSafetyHold as error:
            raise WorkflowError(str(error)) from error


def _verify_files(queue, job, path):
    from .drive_upload import file_identity
    manifest = read_json(queue.state / "recordings" / job / "manifest.json")
    for name, expected in manifest.items():
        if file_identity(safe_member(path, name)) != expected:
            raise WorkflowError("Local recording changed since completion; naming needs review")


def session_path(queue, job):
    """Resolve a relocated original for explicit repair, including crash recovery."""
    row = queue.db.execute("SELECT n.*,j.session FROM jobs j LEFT JOIN local_names n ON n.job=j.id WHERE j.id=?",
                           (job,)).fetchone()
    if not row:
        raise WorkflowError("Unknown meeting")
    source = Path(row["session"])
    if row["destination"]:
        destination = Path(row["destination"])
        if destination.exists() or destination.is_symlink():
            _directory(destination, row, queue)
            return destination
    # A requested date-format update saves its previous named path before
    # moving. That path remains resolvable if the update is interrupted.
    for alias in queue.db.execute("SELECT path FROM local_name_aliases WHERE job=?", (job,)):
        candidate = Path(alias["path"])
        if candidate.parent == source.parent and candidate.is_dir() and not candidate.is_symlink():
            st = candidate.stat()
            if st.st_ino == row["inode"]:
                _directory(candidate, row, queue)
                return candidate
    return source


def relocated_job(queue, session, recordings_root):
    """Honor repeated hooks at either path without admitting a different source."""
    path = Path(os.path.abspath(Path(session).expanduser()))
    if path.is_symlink():
        raise WorkflowError("A session cannot be a symlink")
    path = path.parent.resolve() / path.name
    root = Path(recordings_root).expanduser().resolve()
    if path.parent.resolve() != root:
        raise WorkflowError("Session must be a direct child of the configured recordings root")
    rows = queue.db.execute("SELECT n.*,j.session FROM local_names n JOIN jobs j ON j.id=n.job "
                            "WHERE destination IS NOT NULL AND (j.session=? OR destination=? OR EXISTS "
                            "(SELECT 1 FROM local_name_aliases a WHERE a.job=n.job AND a.path=?))",
                            (str(path), str(path), str(path))).fetchall()
    for row in rows:
        source = Path(row["session"])
        if path == source and source.exists():
            st = source.stat()
            if st.st_ino != row["inode"]:
                # A later recording can legitimately reuse the old timestamp.
                continue
        actual = session_path(queue, row["job"])
        _directory(actual, row, queue)
        queue.require_processing(row["job"])
        _verify_files(queue, row["job"], actual)
        return row["job"]
    return None


def _title(queue, job):
    task = queue.db.execute("SELECT status,receipt FROM tasks WHERE job=? AND stage='notes'", (job,)).fetchone()
    if not task or task["status"] != "succeeded":
        raise WorkflowError("Local naming waits for completed notes")
    result = queue.state / "results" / job
    transcript_path = result / "input/transcript.json"
    if not transcript_path.exists():
        transcript_path = queue.state / "recordings" / job / "transcript.json"
    transcript = read_json(transcript_path)
    receipt = read_json(result / "notes-receipt.json")
    provenance = read_json(result / "notes/provenance.json")
    stored = json.loads(task["receipt"] or "{}")
    if (receipt.get("input_sha256") != hashlib.sha256(transcript_path.read_bytes()).hexdigest()
            or any(not receipt.get(key) or receipt.get(key) != provenance.get(key)
                   or receipt.get(key) != stored.get(key)
                   for key in ("input_sha256", "settings_sha256"))
            or safe_member(result / "notes", "source-transcript.json").read_bytes() != transcript_path.read_bytes()):
        raise WorkflowError("Local title notes have different provenance; review is required")
    notes = read_json(result / "notes/notes.json")
    render_notes(notes, transcript)
    return meeting_title(notes, transcript)


def _rename_exclusive(source, destination):
    """Atomic no-replace directory rename; never overwrite an unrelated folder."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = function(os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = function(-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    else:
        raise WorkflowError("Atomic local folder naming is unsupported on this system")
    if result:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))


def name_session(queue, job, refresh=False):
    from .local_deletion import job_lock
    with job_lock(queue.state, job):
        return _name_session(queue, job, refresh)


def _name_session(queue, job, refresh=False):
    """Save the target before moving; recover the same inode after interruption."""
    queue.require_processing(job)
    from .drive_upload import _folder_name
    queue.db.execute("BEGIN IMMEDIATE")
    try:
        row = _row(queue, job)
        source = Path(row["session"])
        if refresh and row["destination"] and row["status"] == "succeeded":
            source = session_path(queue, job)
            _directory(source, row, queue)
            _verify_files(queue, job, source)
            queue.db.execute("INSERT OR IGNORE INTO local_name_aliases(job,path) VALUES (?,?)",
                             (job, str(source)))
            row["destination"] = None
        if row["destination"]:
            destination = Path(row["destination"])
            if destination.parent != source.parent:
                raise WorkflowError("Saved local destination is outside its recording root")
        else:
            title = _title(queue, job)
            if not title:
                queue.db.execute("UPDATE local_names SET status='skipped',error=NULL WHERE job=?", (job,))
                queue.db.commit()
                return None
            _directory(source, row, queue)
            _verify_files(queue, job, source)
            meta = read_json(safe_member(source, "meta.json"), 100_000)
            date = datetime.fromisoformat(meta["started"].replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise WorkflowError("Recording start time needs an explicit timezone")
            # Drive permits slash dates; the local filesystem treats slashes
            # as path separators. Keep the same short date with hyphens here.
            zone = json.loads(row["settings"]).get("meeting_timezone")
            destination = source.parent / _folder_name(date, job, title, zone).replace("/", "-")
            if destination != source and os.path.lexists(destination):
                destination = destination.with_name(destination.name + " - " + job[:8])
            if destination != source and os.path.lexists(destination):
                raise WorkflowError("Local meeting name already exists; no folder was replaced")
            queue.db.execute("UPDATE local_names SET destination=?,status='planned',error=NULL WHERE job=?",
                             (str(destination), job))
        # The plan must survive a crash between rename and recording success.
        queue.db.commit()
        queue.db.execute("BEGIN IMMEDIATE")
        row = _row(queue, job)
        if os.path.lexists(destination):
            _directory(destination, row, queue)
            _verify_files(queue, job, destination)
        else:
            source = session_path(queue, job)
            _directory(source, row, queue)
            _verify_files(queue, job, source)
            queue.require_processing(job)
            _rename_exclusive(source, destination)
        queue.db.execute("UPDATE local_names SET status='succeeded',error=NULL WHERE job=?", (job,))
        queue.db.commit()
        return destination
    except BaseException:
        queue.db.rollback()
        raise


def name_pending(queue, config):
    if not enabled(config):
        return
    rows = queue.db.execute("SELECT n.job FROM local_names n JOIN tasks t ON n.job=t.job "
                            "WHERE t.stage='notes' AND t.status='succeeded' "
                            "AND n.status IN ('pending','planned','retry')").fetchall()
    for row in rows:
        try:
            name_session(queue, row["job"])
        except Exception as error:
            with queue.db:
                queue.db.execute("UPDATE local_names SET status='retry',error=? WHERE job=? AND status<>'deleted'",
                                 (str(error)[:500], row["job"]))
