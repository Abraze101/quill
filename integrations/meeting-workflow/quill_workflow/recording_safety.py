"""Read-only recording approval boundary, shared by every workflow stage."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import uuid

FILENAME = "recording-safety.json"
MAX_BYTES = 100_000


class RecordingSafetyHold(Exception):
    """Capture is unfinished or requires the user's explicit processing decision."""


def _iso8601(value):
    # Match the complete internet date/time written and accepted by the app's
    # ISO8601 formatter. Naive dates, partial times, and numeric dates are not
    # substitutes for a persisted deadline with an explicit UTC offset.
    if (not isinstance(value, str) or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)", value) is None):
        raise ValueError("Expected an ISO8601 timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number: " + value)


def _bounded_json(fd):
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Expected a bounded regular file")
        data = handle.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("JSON file grew beyond its bound")
        return json.loads(data, parse_constant=_invalid_constant)


def _require_finalized_metadata(session):
    try:
        fd = os.open(Path(session) / "meta.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        data = _bounded_json(fd)
        if not isinstance(data, dict):
            raise ValueError("Expected finalized recording metadata")
        _iso8601(data.get("started"))
        _iso8601(data.get("ended"))
        files = data.get("files")
        if (not isinstance(files, dict)
                or not all(isinstance(key, str) and isinstance(value, str) for key, value in files.items())
                or files.get("mic") != "mic.caf" or files.get("system") != "system.caf"):
            raise ValueError("Expected finalized microphone and system track metadata")
        # Do not compare ended with started: the wall clock can change during
        # a valid recording. Capture durations use the app's monotonic clock.
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError) as error:
        raise RecordingSafetyHold("Recording metadata is unfinished or invalid; processing stays local") from error


def read_safety(session, required=False, expected_session_id=None, require_approval=True):
    """Return validated approval, or None only for a genuinely legacy recording.

    This module never creates, edits, or approves the app-owned safety journal.
    The queue remembers its presence so removal cannot reopen an existing job.
    """
    path = Path(session) / FILENAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as error:
        if required:
            raise RecordingSafetyHold("Recording safety review is missing; processing stays local") from error
        return None
    except OSError as error:
        raise RecordingSafetyHold("Recording safety review is unreadable; processing stays local") from error
    try:
        data = _bounded_json(fd)
        if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
                or data["schema_version"] != 1
                or not isinstance(data.get("session_id"), str)
                or str(uuid.UUID(data["session_id"])) != data["session_id"].lower()
                or data.get("owner") not in ("manual", "automatic")
                or data.get("phase") not in ("active", "finished")
                or data.get("review_status") not in ("pending", "approved", "kept_local")
                or type(data.get("warning_revision")) is not int or data["warning_revision"] < 1
                or not isinstance(data.get("reasons"), list) or len(data["reasons"]) > 100
                or not all(isinstance(reason, str) and reason.strip()
                           and len(reason.encode("utf-8")) <= 2000 for reason in data["reasons"])
                or data["phase"] == "active" and data["review_status"] == "approved"):
            raise ValueError("Unsupported safety journal")
        started = _iso8601(data.get("started_at"))
        check_in = _iso8601(data.get("check_in_at"))
        stop = _iso8601(data.get("stop_at"))
        if check_in < started or stop <= check_in:
            raise ValueError("Invalid recording safety deadlines")
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError) as error:
        raise RecordingSafetyHold("Recording safety review is invalid; processing stays local") from error
    if expected_session_id is not None and data["session_id"].lower() != expected_session_id.lower():
        raise RecordingSafetyHold("Recording safety session changed; processing stays local")
    if require_approval:
        require_approved(data, session)
    return data


def require_approved(data, session):
    if data["phase"] != "finished":
        raise RecordingSafetyHold("Recording is unfinished; processing stays local")
    if data["review_status"] == "kept_local":
        raise RecordingSafetyHold("Recording was kept local; processing is on hold")
    if data["review_status"] != "approved":
        raise RecordingSafetyHold("Review this recording in Quill before processing")
    _require_finalized_metadata(session)
