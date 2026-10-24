"""Read-only display metadata for the meeting library.

This is a view, never an upload/retry/approval authority. A confirmed storage
state means a completed filing has matching saved evidence, not that Drive was
queried just now. Strings are plain text: callers must use textContent or HTML
escaping. No original recording path, calendar suggestion, or current company
configuration is used to invent meeting metadata.
"""
from datetime import datetime
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import unicodedata
from zoneinfo import ZoneInfo

from .drive_upload import COMBINED_TEXT_FORMAT, NATIVE_TEMPLATE_FORMAT, _hash
from .capture_quality import notice
from .meeting_dates import recording_timezone
from .meeting_title import meeting_title
from .native_template import build_payload
from .pipeline import WorkflowError, render_notes
from .transcript_sources import segment_label, speaker_attributions


_JOB = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")
MAX_JSON_BYTES = 8_000_000
MAX_TRANSCRIPT_CHARACTERS = 200_000
_INVALID = object()


def _nonfinite(value):
    raise ValueError("Non-finite JSON")


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON member")
        value[key] = item
    return value


def _decode(value):
    if not isinstance(value, (str, bytes)) or len(value) > MAX_JSON_BYTES:
        raise ValueError("Unbounded JSON")
    return json.loads(value, object_pairs_hook=_object, parse_constant=_nonfinite)


def _settings_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _text(value, limit=300):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    return " ".join("".join(char for char in value if not unicodedata.category(char).startswith("C")).split()) or None


class _Sources:
    """Open only fixed members beneath a held directory, with no symlink hops."""

    def __init__(self, state):
        self.state = Path(state).expanduser().absolute()
        self.fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.cache = {}

    def close(self):
        os.close(self.fd)

    def read(self, relative, limit=MAX_JSON_BYTES):
        key = str(relative)
        if key in self.cache:
            return self.cache[key]
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(part in (".", "..") for part in parts):
            return _INVALID
        directory = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
                    raise ValueError("Not a bounded regular source")
                data = source.read(limit + 1)
                after = os.fstat(source.fileno())
            if len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("Source changed during read")
            result = data
        except FileNotFoundError:
            result = None
        except (OSError, ValueError):
            result = _INVALID
        finally:
            os.close(directory)
        self.cache[key] = result
        return result

    def json(self, relative, limit=MAX_JSON_BYTES):
        raw = self.read(relative, limit)
        if not isinstance(raw, bytes):
            return raw
        try:
            return _decode(raw)
        except (ValueError, UnicodeError, RecursionError):
            return _INVALID

    def rows(self):
        """Read one SQLite snapshot without creating a missing database."""
        path = self.state / "jobs.sqlite3"
        info = os.stat("jobs.sqlite3", dir_fd=self.fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            return [], {}, set()
        # SQLite owns WAL visibility. immutable=1 would silently omit pending
        # WAL commits, so use its read-only URI and a single read transaction.
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                side = os.stat("jobs.sqlite3" + suffix, dir_fd=self.fd, follow_symlinks=False)
                if not stat.S_ISREG(side.st_mode):
                    return [], {}, set()
            except FileNotFoundError:
                pass
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            after = path.lstat()
            if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
                return [], {}, set()
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_names'").fetchone():
                query = ("SELECT j.id,j.session,n.destination,n.status AS naming_status FROM jobs j "
                         "LEFT JOIN local_names n ON n.job=j.id")
            else:
                query = "SELECT id,session,NULL AS destination,NULL AS naming_status FROM jobs"
            jobs = [dict(row) for row in db.execute(query)]
            tasks = {}
            for row in db.execute("SELECT job,stage,status,settings,receipt FROM tasks"):
                value = dict(row)
                try:
                    value["settings"] = _decode(value["settings"])
                    value["receipt"] = _decode(value["receipt"]) if value["receipt"] else None
                except (ValueError, UnicodeError, RecursionError):
                    value["settings"], value["receipt"] = {}, _INVALID
                tasks.setdefault(row["job"], {})[row["stage"]] = value
            removed = set()
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_deletions'").fetchone():
                # Any tombstone closes access to local content while Trash is
                # pending too. The deletion view supplies its own recovery row.
                removed = {row[0] for row in db.execute("SELECT job FROM local_deletions")}
        return jobs, tasks, removed


def _receipt(sources, job, stage, tasks, transcript_hash):
    task = tasks.get(stage, {})
    settings = task.get("settings")
    if task.get("status") != "succeeded" or not isinstance(settings, dict):
        return None
    receipt = sources.json(f"results/{job}/{stage}-receipt.json", 1_000_000)
    expected = {"job": job, "stage": stage, "idempotency_key": job + ":" + stage,
                "settings_sha256": _settings_hash(settings), "input_sha256": transcript_hash}
    if (not transcript_hash or not isinstance(receipt, dict) or receipt != task.get("receipt")
            or any(receipt.get(key) != value for key, value in expected.items())):
        return None
    return receipt


def _route(sources, job, tasks, transcript_hash):
    receipt = _receipt(sources, job, "route", tasks, transcript_hash)
    settings = tasks.get("route", {}).get("settings", {})
    if not receipt or not isinstance(settings, dict) or settings.get("enabled") is not True:
        return None
    route = receipt.get("route")
    companies = settings.get("companies")
    if not isinstance(route, dict) or not isinstance(companies, list):
        return None
    for company in companies:
        if not isinstance(company, dict):
            continue
        expected = {"company_id": company.get("id"), "company_name": company.get("name"),
                    "drive_folder_id": company.get("drive_folder_id")}
        if (route == expected and isinstance(route["company_id"], str)
                and _text(route["company_name"]) and isinstance(route["drive_folder_id"], str)
                and _ID.fullmatch(route["drive_folder_id"])):
            return route
    return None


def _transcript(sources, job):
    relative = f"results/{job}/input/transcript.json"
    raw = sources.read(relative)
    if raw is None:
        relative = f"recordings/{job}/transcript.json"
        raw = sources.read(relative)
    if not isinstance(raw, bytes):
        return None, None, None, []
    value = sources.json(relative)
    if not isinstance(value, dict) or not isinstance(value.get("segments"), list):
        return None, None, None, []
    try:
        capture_notice = notice(value)
        attributions = speaker_attributions(value)
        lines = []
        for segment in value["segments"]:
            if (not isinstance(segment, dict) or not isinstance(segment.get("text"), str)
                    or type(segment.get("start_ms")) is not int or segment["start_ms"] < 0):
                raise ValueError("Invalid transcript segment")
            lines.append(segment_label(segment, attributions) + ": " + segment["text"])
        text = "\n\n".join(lines)
        if text and capture_notice:
            text = capture_notice + "\n\n" + text
        if len(text) > MAX_TRANSCRIPT_CHARACTERS:
            text = text[:MAX_TRANSCRIPT_CHARACTERS] + "\n\n[Transcript preview shortened.]"
        participants = [person["name"] for person in (attributions or {}).values() if person.get("name")]
        return value, hashlib.sha256(raw).hexdigest(), text or None, participants
    except (ValueError, TypeError, KeyError, RecursionError):
        return None, None, None, []


def _notes(sources, job, tasks, transcript, transcript_hash):
    receipt = _receipt(sources, job, "notes", tasks, transcript_hash)
    if not receipt or transcript is None:
        return None, None
    notes = sources.json(f"results/{job}/notes/notes.json")
    provenance = sources.json(f"results/{job}/notes/provenance.json", 100_000)
    source = sources.read(f"results/{job}/notes/source-transcript.json")
    if (not isinstance(notes, dict) or not isinstance(provenance, dict) or not isinstance(source, bytes)
            or hashlib.sha256(source).hexdigest() != transcript_hash
            or any(provenance.get(key) != receipt.get(key) for key in (
                "job", "stage", "idempotency_key", "input_sha256", "settings_sha256"))):
        return None, None
    try:
        render_notes(notes, transcript)
        return notes, meeting_title(notes, transcript)
    except (ValueError, TypeError, KeyError, RecursionError, WorkflowError):
        return None, None


def _date(metadata):
    if not isinstance(metadata, dict):
        return None, None
    try:
        started = datetime.fromisoformat(metadata["started"].replace("Z", "+00:00"))
        if started.tzinfo is None:
            return None, None
        zone = recording_timezone(metadata)
        display = started.astimezone(ZoneInfo(zone)) if zone else started
        return f"{display.month}/{display.day}/{display.year % 100:02d}", started.timestamp()
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None, None


def _recording_files(metadata):
    if not isinstance(metadata, dict) or not isinstance(metadata.get("files"), dict):
        return []
    result = []
    for kind, label, suffixes in (("mic", "Microphone audio", (".caf", ".m4a", ".wav")),
                                  ("system", "System audio", (".caf", ".m4a", ".wav")),
                                  ("screen", "Screen video", (".mp4", ".mkv", ".mov"))):
        name = metadata["files"].get(kind)
        if (isinstance(name, str) and len(name) <= 255 and name not in (".", "..")
                and "/" not in name and "\\" not in name and _text(name, 255) == name
                and Path(name).suffix.lower() in suffixes):
            result.append({"kind": kind, "label": label, "name": name})
    return result


def _local_folder(row):
    # The persisted name is useful context, not proof the original still
    # exists. Never resolve, enumerate, or read the user-supplied path here.
    value = row.get("destination") if row.get("naming_status") == "succeeded" else row.get("session")
    if (not isinstance(value, str) or len(value) > 4096 or not Path(value).is_absolute()
            or ".." in Path(value).parts or any(unicodedata.category(char).startswith("C") for char in value)):
        return None
    return value


def _matches_file(sources, relative, identity):
    """Compare bounded current bytes, never a path supplied by the journal."""
    value = sources.read(relative)
    return (isinstance(value, bytes) and type(identity.get("size")) is int
            and identity["size"] == len(value)
            and identity.get("sha256") == hashlib.sha256(value).hexdigest())


def _current_older_document(sources, job, text_format, files, identity, settings, metadata, route, notes, transcript):
    """Establish that an older upload represents the currently displayed notes.

    Historical completion alone cannot confirm a later local revision. A
    missing or incompatible provenance record leaves that document uncertain.
    """
    if notes is None or not all(isinstance(item, dict) for item in files):
        return False
    if text_format == "legacy":
        if len(files) != 2 or {item.get("name") for item in files} != {"notes.json", "notes.md"}:
            return False
        return all(_matches_file(sources, f"results/{job}/notes/{item['name']}", item) for item in files)
    if text_format != COMBINED_TEXT_FORMAT or len(files) != 1 or files[0].get("name") != "documents/meeting.docx":
        return False
    manifest = sources.json(f"results/{job}/documents/manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("format") != COMBINED_TEXT_FORMAT:
        return False
    timezone = settings.get("meeting_timezone") or settings.get("document_timezone", "UTC")
    inputs = {"notes": notes, "transcript": transcript, "metadata": metadata, "route": route, "timezone": timezone}
    layout = settings.get("notes_layout", "sources")
    if layout == "clean":
        inputs["notes_layout"] = layout
    if (layout not in ("sources", "clean") or manifest.get("notes_layout", "sources") != layout
            or manifest.get("inputs_sha256") != _hash(inputs)
            or manifest.get("docx_sha256") != files[0].get("sha256")
            or not _matches_file(sources, f"results/{job}/documents/meeting.docx", files[0])
            or manifest.get("title") != identity.get("document_title") or manifest.get("timezone") != timezone):
        return False
    paragraphs = manifest.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs or any(not isinstance(item, str) for item in paragraphs):
        return False
    normalized = " ".join(unicodedata.normalize("NFC", " ".join(paragraphs)).split())
    if hashlib.sha256(normalized.encode("utf-8")).hexdigest() != identity.get("document_text_sha256"):
        return False
    # The web preview reads notes.md. The DOCX manifest binds notes.json, so
    # also reject a separately edited Markdown preview. Do not regenerate a
    # document to repair missing historical proof from this read-only view.
    markdown = sources.read(f"results/{job}/notes/notes.md")
    return isinstance(markdown, bytes) and markdown == render_notes(notes, transcript).encode("utf-8")


def _confirmed(sources, job, stage, tasks, transcript_hash, route, metadata, notes, transcript, journal):
    receipt = _receipt(sources, job, stage, tasks, transcript_hash)
    settings = tasks[stage]["settings"]
    if (not receipt or route is None or not isinstance(metadata, dict) or not isinstance(journal, dict)
            or receipt.get("route_sha256") != _settings_hash(route)):
        return None
    identity, saved = journal.get("identity"), journal.get("receipt")
    if (not isinstance(identity, dict) or not isinstance(saved, dict) or not saved
            or journal.get("identity_sha256") != _hash(identity)
            or any(receipt.get(key) != value for key, value in saved.items())):
        return None
    expected = {"job": job, "stage": stage, "idempotency_key": job + ":" + stage,
                "meetings_root_id": settings.get("meetings_root_id"), "route": route,
                "started": metadata.get("started")}
    if any(identity.get(key) != value for key, value in expected.items()):
        return None
    text_format = settings.get("text_format", "legacy")
    source = sources.state / (f"recordings/{job}" if stage == "archive" else
                              f"results/{job}/notes" if text_format == "legacy" else f"results/{job}")
    if identity.get("source") != str(source) or identity.get("text_format", "legacy") != text_format:
        return None
    if settings.get("meeting_timezone") != recording_timezone(metadata):
        return None
    if identity.get("meeting_timezone") != settings.get("meeting_timezone"):
        return None
    files, uploaded, operations = identity.get("files"), saved.get("files"), journal.get("operations")
    if (not isinstance(files, list) or not files or not isinstance(uploaded, list)
            or len(files) != len(uploaded) or not isinstance(operations, dict)):
        return None
    destination, meeting = saved.get("folder_id"), saved.get("meeting_folder_id")
    if not all(isinstance(value, str) and _ID.fullmatch(value) for value in (destination, meeting)):
        return None
    folder_op = operations.get("stage", {})
    if folder_op.get("state") != "verified" or folder_op.get("id") != destination or folder_op.get("parent") != meeting:
        return None
    if stage == "publish" and settings.get("text_format") == NATIVE_TEMPLATE_FORMAT:
        if notes is None:
            return None
        markdown = sources.read(f"results/{job}/notes/notes.md")
        if markdown is not None and (not isinstance(markdown, bytes)
                or markdown != render_notes(notes, transcript).encode("utf-8")):
            # Native upload binds structured notes. If the independent local
            # preview was edited, it must not inherit that saved status.
            return None
        payload = build_payload(notes, transcript, metadata, route,
                                settings.get("meeting_timezone") or settings.get("document_timezone", "UTC"))
        expected_files = [{"name": "native-template", "mime_type": "application/vnd.google-apps.document",
                           "payload_sha256": _hash(payload)}]
        if (files != expected_files or identity.get("document_title") != payload["title"]
                or identity.get("template_document_id") != settings.get("template_document_id")
                or identity.get("template_sha256") != settings.get("template_sha256")
                or saved.get("verification") != "metadata_and_template_content"):
            return None
    elif stage == "publish":
        if not _current_older_document(sources, job, text_format, files, identity, settings, metadata, route, notes, transcript):
            return None
        if text_format == COMBINED_TEXT_FORMAT and saved.get("verification") != "metadata_and_document_text":
            return None
    elif stage == "archive":
        manifest = sources.json(f"recordings/{job}/manifest.json", 100_000)
        capture = metadata.get("files", {})
        if not isinstance(manifest, dict) or not isinstance(capture, dict):
            return None
        required = {capture.get("mic"), capture.get("system")}
        if capture.get("screen"):
            required.add(capture["screen"])
        if None in required or not required.issubset({item.get("name") for item in files if isinstance(item, dict)}):
            return None
        for item in files:
            if not isinstance(item, dict) or not isinstance(manifest.get(item.get("name")), dict):
                return None
            if any(item.get(key) != manifest[item["name"]].get(key) for key in ("sha256", "size")):
                return None
    ids = []
    for file, remote in zip(files, uploaded):
        if not isinstance(file, dict) or not isinstance(remote, dict) or not isinstance(file.get("name"), str):
            return None
        identity = remote.get("id")
        op = operations.get("file:" + file["name"], {})
        if (not isinstance(identity, str) or not _ID.fullmatch(identity)
                or any(remote.get(key) != value for key, value in file.items())
                or op.get("state") != "verified" or op.get("id") != identity or op.get("parent") != destination
                or remote.get("verification") not in ("metadata", "metadata_and_checksum",
                    "metadata_and_template_content", "metadata_and_document_text")
                or op.get("verification") != remote.get("verification")):
            return None
        if file.get("payload_sha256") and op.get("fill", {}).get("state") != "verified":
            return None
        ids.append(identity)
    return {"status": "confirmed", "file_ids": ids, "folder_id": destination,
            "meeting_folder_id": meeting, "meeting_folder_name": _text(saved.get("meeting_folder_name"))}


def _storage(sources, job, stage, tasks, transcript_hash, route, metadata, notes, transcript):
    task = tasks.get(stage)
    result = {"status": "unknown", "file_ids": []}
    if task is None:
        return result
    settings = task.get("settings")
    if not isinstance(settings, dict):
        return result
    journal = sources.json(f"results/{job}/drive-{stage}.json")
    if settings.get("enabled") is False and journal is None and not task.get("receipt"):
        result["status"] = "not_requested"
        return result
    if settings.get("type") != "codex_drive":
        return result
    try:
        confirmed = _confirmed(sources, job, stage, tasks, transcript_hash, route, metadata, notes, transcript, journal)
        if confirmed:
            return confirmed
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, WorkflowError):
        pass
    operations = journal.get("operations", {}) if isinstance(journal, dict) else {}
    external_evidence = journal is _INVALID or task.get("receipt") or task.get("status") == "succeeded"
    if isinstance(operations, dict):
        external_evidence = external_evidence or any(isinstance(op, dict) and (
            op.get("state") in ("dispatched", "acknowledged", "verified")
            or isinstance(op.get("fill"), dict) and op["fill"].get("state") in ("dispatched", "acknowledged", "verified"))
            for op in operations.values())
    result["status"] = "uncertain" if external_evidence else "failed" if task.get("status") == "failed" else "pending"
    return result


def _entry(sources, row, tasks):
    job = row["id"]
    transcript, transcript_hash, transcript_text, participants = _transcript(sources, job)
    metadata = sources.json(f"recordings/{job}/meta.json", 100_000)
    display_date, recorded_at = _date(metadata)
    notes, title = _notes(sources, job, tasks, transcript, transcript_hash)
    route = _route(sources, job, tasks, transcript_hash)
    storage = {name: _storage(sources, job, stage, tasks, transcript_hash, route, metadata, notes, transcript)
               for name, stage in (("document", "publish"), ("media", "archive"))}
    states = [value["status"] for value in storage.values()]
    status = ("upload_uncertain" if "uncertain" in states else
              "saved_in_drive" if "confirmed" in states and all(item in ("confirmed", "not_requested") for item in states) else
              "partly_saved" if "confirmed" in states else
              "not_requested" if all(item == "not_requested" for item in states) else "not_confirmed")
    filed = next((value for value in storage.values() if value["status"] == "confirmed"), {})
    return {"title": title, "display_date": display_date, "recorded_at": recorded_at,
            "participants": participants, "transcript_text": transcript_text,
            "recording_files": _recording_files(metadata), "local_folder": _local_folder(row),
            "company_id": route["company_id"] if route else None,
            "folder_id": route["drive_folder_id"] if route else None,
            "folder_name": _text(route["company_name"]) if route else None,
            "meeting_folder_id": filed.get("meeting_folder_id"),
            "meeting_folder_name": filed.get("meeting_folder_name"),
            "storage_status": status, "storage": storage}


def meeting_library(state):
    """Return metadata keyed by queued job; never create missing state.

    storage_status: saved_in_drive, partly_saved, upload_uncertain,
    not_confirmed, or not_requested. Individual document/media status is
    confirmed, uncertain, pending, failed, unknown, or not_requested.
    Unknown means unverified, never proof that a cloud copy does not exist.
    """
    try:
        sources = _Sources(state)
    except (OSError, ValueError, TypeError):
        return {}
    try:
        try:
            jobs, all_tasks, removed = sources.rows()
        except (OSError, ValueError, sqlite3.Error):
            return {}
        library = {}
        for row in jobs:
            job = row["id"]
            if not isinstance(job, str) or not _JOB.fullmatch(job) or job in removed:
                continue
            try:
                library[job] = _entry(sources, row, all_tasks.get(job, {}))
            except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, WorkflowError):
                # A damaged historical record must not hide unrelated meetings
                # or turn uncertain cloud evidence into a local-only claim.
                library[job] = {"title": None, "display_date": None, "recorded_at": None,
                                "participants": [], "transcript_text": None, "recording_files": [],
                                "local_folder": _local_folder(row), "company_id": None,
                                "folder_id": None, "folder_name": None, "meeting_folder_id": None,
                                "meeting_folder_name": None, "storage_status": "not_confirmed",
                                "storage": {name: {"status": "unknown", "file_ids": []} for name in ("document", "media")}}
            finally:
                sources.cache.clear()
        return library
    finally:
        sources.close()
