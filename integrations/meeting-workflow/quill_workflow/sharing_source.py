"""Validate an already-filed meeting and expose only its reviewable notes.

The private transcript is read solely to validate the existing notes and their
published payload. It is never returned to the sharing transport. This module
does not create documents, change permissions, or approve any recording.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from .drive_upload import DOCUMENT_MIME, NATIVE_TEMPLATE_FORMAT, _hash, native_template_sha256
from .meeting_dates import recording_timezone
from .native_template import build_payload, note_sections
from .capture_quality import with_summary_notice
from .pipeline import Queue, WorkflowError, load_config, render_notes, route_settings
from .recording_safety import RecordingSafetyHold
from .routing import RoutingError, RoutingReviews, preserves_company_map


class SharingSourceError(ValueError):
    """The saved meeting no longer supports the proposed sharing review."""


_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")
_HASH = re.compile(r"[a-f0-9]{64}")
_SAFE_FIELDS = {"schema_version", "meeting_title", "summary", "topics", "decisions",
                "actions", "next_steps", "open_questions"}


def _require(condition, message):
    if not condition:
        raise SharingSourceError(message)


def _settings_hash(settings):
    # Match ConnectorRunner's established receipt encoding exactly.
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def _bytes_hash(value):
    return hashlib.sha256(value).hexdigest()


def _nonfinite(value):
    raise ValueError("Non-finite JSON value")


def _processing(queue, job):
    from .local_naming import session_path
    original = session_path(queue, job)
    _require(not original.is_symlink() and original.is_dir(), "The original recording is unavailable")
    return queue.require_processing(job)


def _preview(payload):
    values = payload["replacements"]
    lines = [values["{{QUILL_TITLE}}"], values["{{QUILL_DATE}}"],
             "Participants: " + values["{{QUILL_PARTICIPANTS}}"], values["{{QUILL_ATTACHMENTS}}"], "",
             "Summary", values["{{QUILL_SUMMARY}}"], "", "Decisions",
             values["{{QUILL_DECISION_STATUS}}"], values["{{QUILL_DECISIONS}}"], "",
             "Next steps", values["{{QUILL_ACTIONS}}"], "", "Details", values["{{QUILL_DETAILS}}"]]
    return "\n".join(lines) + "\n"


def sharing_snapshot(state, config_path, job):
    """Return a frozen notes-only proposal for one successfully filed job.

    Eligibility is checked before reading any transcript. Repeating this at
    confirmation and every external step makes edited sources invalidate the
    earlier review. Audio archival is intentionally not a dependency.
    """
    try:
        return _snapshot(state, config_path, job)
    except SharingSourceError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error,
            WorkflowError, RoutingError, RecordingSafetyHold) as error:
        raise SharingSourceError("The meeting source needs review before sharing: " + str(error)) from error


def _snapshot(state, config_path, job):
    _require(isinstance(job, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job)
             and job not in {".", ".."}, "Choose a saved meeting job")
    state = Path(state).expanduser().absolute()
    _require(not state.is_symlink() and state.is_dir(), "The workflow state is unavailable")
    reads = {}

    def read(relative, limit=20_000_000):
        path = state / relative
        _require(path.is_relative_to(state), "Sharing sources must remain in the workflow state")
        for parent in (path, *path.parents):
            if parent == state:
                break
            _require(not parent.is_symlink(), "A sharing source cannot be a symbolic link")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            _require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit,
                     "Sharing sources must be bounded regular files")
            value = source.read(limit + 1)
            after = os.fstat(source.fileno())
        _require(len(value) <= limit and (info.st_size, info.st_mtime_ns) ==
                 (after.st_size, after.st_mtime_ns), "The meeting source changed while being read")
        previous = reads.get(relative)
        _require(previous is None or previous == value, "The meeting source changed during review")
        reads[relative] = value
        return json.loads(value, parse_constant=_nonfinite)

    for name in ("jobs.sqlite3", "reviews.sqlite3"):
        _require(not (state / name).is_symlink() and (state / name).is_file(),
                 "The meeting's saved workflow is unavailable")
    connection = sqlite3.connect((state / "jobs.sqlite3").as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT stage,status,settings,receipt FROM tasks WHERE job=?", (job,)).fetchall()
        tasks = {row["stage"]: dict(row) for row in rows}
    finally:
        connection.close()
    _require(all(tasks.get(stage, {}).get("status") == "succeeded" for stage in ("notes", "route", "publish")),
             "Sharing waits for completed notes, company approval, and Drive filing")
    for stage in ("notes", "route", "publish"):
        tasks[stage]["settings"] = json.loads(tasks[stage]["settings"])
        tasks[stage]["receipt"] = json.loads(tasks[stage]["receipt"])
    settings = tasks["publish"]["settings"]
    _require(settings.get("type") == "codex_drive" and settings.get("text_format") == NATIVE_TEMPLATE_FORMAT,
             "Sharing requires an already-filed native meeting document")
    frozen = route_settings(tasks["route"]["settings"])
    config = load_config(config_path)
    current_route = route_settings(config.get("route", {}))
    _require(frozen.get("enabled") is True and current_route.get("enabled") is True
             and preserves_company_map(current_route["companies"], frozen["companies"]),
             "Company folders changed or routing is disabled; review them before sharing")
    queue = Queue(state)
    try:
        approval = _processing(queue, job)
    finally:
        queue.close()

    result = Path("results") / job
    recording = Path("recordings") / job
    transcript_file = result / "input/transcript.json"
    if not os.path.lexists(state / transcript_file):
        transcript_file = recording / "transcript.json"
    transcript = read(transcript_file)
    transcript_bytes = reads[transcript_file]
    transcript_hash = _bytes_hash(transcript_bytes)
    notes = read(result / "notes/notes.json")
    provenance = read(result / "notes/provenance.json", 100_000)
    read(result / "notes/source-transcript.json")
    _require(reads[result / "notes/source-transcript.json"] == transcript_bytes,
             "The notes use a different transcript; create a new review")
    receipts = {}
    for stage in ("notes", "route", "publish"):
        receipt = read(result / (stage + "-receipt.json"))
        expected = {"job": job, "stage": stage, "idempotency_key": job + ":" + stage,
                    "settings_sha256": _settings_hash(tasks[stage]["settings"]), "input_sha256": transcript_hash}
        _require(receipt == tasks[stage]["receipt"] and all(receipt.get(key) == value for key, value in expected.items()),
                 "The saved " + stage + " receipt no longer matches this meeting")
        receipts[stage] = receipt
    _require(all(provenance.get(key) == receipts["notes"].get(key)
                 for key in ("job", "stage", "idempotency_key", "input_sha256", "settings_sha256")),
             "The notes provenance changed; create a new review")
    render_notes(notes, transcript)
    reviews = RoutingReviews(state)
    try:
        review = reviews.get(job)
        route = reviews.route(job, transcript_bytes, frozen["companies"])
    finally:
        reviews.close()
    _require(route is not None and review and review["status"] == "approved", "Choose the meeting's company before sharing")
    _require(receipts["route"].get("route") == route, "The saved company route differs from its approval")
    _require(receipts["publish"].get("route_sha256") == _settings_hash(route),
             "The filed document belongs to a different company review")
    metadata = read(recording / "meta.json", 100_000)
    _require(settings.get("meeting_timezone") == recording_timezone(metadata),
             "The meeting's recorded timezone changed")
    timezone = settings.get("meeting_timezone") or settings.get("document_timezone", "UTC")
    private_payload = build_payload(notes, transcript, metadata, route, timezone)
    journal = read(result / "drive-publish.json")
    identity = journal.get("identity", {})
    payload_hash = _hash(private_payload)
    files = [{"name": "native-template", "mime_type": DOCUMENT_MIME, "payload_sha256": payload_hash}]
    expected_identity = {"job": job, "stage": "publish", "idempotency_key": job + ":publish",
                         "meetings_root_id": settings.get("meetings_root_id"), "route": route,
                         "started": metadata.get("started"), "files": files, "source": str(state / result),
                         "text_format": NATIVE_TEMPLATE_FORMAT,
                         "template_document_id": settings.get("template_document_id"),
                         "template_sha256": settings.get("template_sha256"), "document_title": private_payload["title"]}
    if settings.get("meeting_timezone") is not None:
        expected_identity["meeting_timezone"] = settings["meeting_timezone"]
    _require(identity == expected_identity and journal.get("identity_sha256") == _hash(identity),
             "The notes changed after Drive filing; create a new review")
    published = receipts["publish"]
    saved = journal.get("receipt")
    _require(isinstance(saved, dict) and all(published.get(key) == value for key, value in saved.items())
             and saved.get("verification") == "metadata_and_template_content"
             and isinstance(saved.get("files"), list) and len(saved["files"]) == 1,
             "The native document's completed filing could not be verified")
    remote = saved["files"][0]
    _require(all(remote.get(key) == value for key, value in files[0].items())
             and remote.get("verification") == "metadata_and_template_content",
             "The filed document no longer matches the validated notes")
    document_id, destination = remote.get("id"), saved.get("folder_id")
    for value in (document_id, destination, settings.get("template_document_id")):
        _require(isinstance(value, str) and _ID.fullmatch(value), "The saved Drive identity is invalid")
    _require(isinstance(settings.get("template_sha256"), str) and _HASH.fullmatch(settings["template_sha256"]),
             "The native template binding is invalid")
    operations = journal.get("operations", {})
    operation, stage_operation = operations.get("file:native-template", {}), operations.get("stage", {})
    _require(operation.get("state") == "verified" and operation.get("id") == document_id
             and operation.get("parent") == destination and operation.get("name") == private_payload["title"]
             and operation.get("verification") == "metadata_and_template_content"
             and stage_operation.get("state") == "verified" and stage_operation.get("id") == destination
             and stage_operation.get("parent") == saved.get("meeting_folder_id"),
             "The native document's verified destination is unavailable")
    fill = operation.get("fill", {})
    _require(fill.get("state") == "verified" and isinstance(fill.get("template_document"), dict)
             and native_template_sha256(fill["template_document"]) == settings["template_sha256"],
             "The source document's template verification is unavailable")
    owner = journal.get("owner")
    _require(isinstance(owner, str) and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", owner)
             and len(owner) <= 254, "The source document owner is unverified")

    # The private payload has served its validation purpose. Build a fresh
    # output with only explicitly shareable fields and no source references.
    safe_notes = {key: copy.deepcopy(value) for key, value in notes.items() if key in _SAFE_FIELDS}
    sections = with_summary_notice(note_sections(safe_notes), transcript)
    values = {key: private_payload["replacements"][key] for key in (
        "{{QUILL_DATE}}", "{{QUILL_TITLE}}", "{{QUILL_PARTICIPANTS}}")}
    values.update({"{{QUILL_" + key + "}}": value for key, value in sections.items()})
    values.update({"{{QUILL_TRANSCRIPT}}": "Not included.", "{{QUILL_ATTACHMENTS}}": "Meeting notes only."})
    title = private_payload["title"][:180].rstrip() + " - Shared notes"
    payload = {"format": NATIVE_TEMPLATE_FORMAT, "title": title, "replacements": values,
               "date": copy.deepcopy(private_payload["date"]), "company_name": route["company_name"],
               "transcript_segments": 0}
    snapshot = {"job": job, "title": title, "template_document_id": settings["template_document_id"],
                "template_sha256": settings["template_sha256"], "destination_folder_id": destination,
                "owner": owner, "private_document_id": document_id, "notes_payload": payload,
                "preview": _preview(payload), "binding": {
                    "transcript_sha256": transcript_hash,
                    "notes_sha256": _bytes_hash(reads[result / "notes/notes.json"]),
                    "metadata_sha256": _bytes_hash(reads[recording / "meta.json"]),
                    "settings_sha256": {stage: _settings_hash(tasks[stage]["settings"]) for stage in receipts},
                    "receipts_sha256": {stage: _hash(value) for stage, value in receipts.items()},
                    "route_sha256": _hash(route), "routing_revision": review["revision"],
                    "processing_approval_sha256": _hash(approval),
                    "routing_review_sha256": _hash(review), "private_payload_sha256": payload_hash,
                    "journal_sha256": _bytes_hash(reads[result / "drive-publish.json"])}}
    # Detect changes while assembling the preview, including a withdrawn
    # recording decision. Re-read only bounded source files already selected.
    for relative in tuple(reads):
        read(relative)
    queue = Queue(state)
    try:
        _require(_processing(queue, job) == approval, "The recording's processing approval changed during review")
        fresh = {row["stage"]: dict(row) for row in queue.db.execute(
            "SELECT stage,status,settings,receipt FROM tasks WHERE job=?", (job,))}
    finally:
        queue.close()
    for stage in receipts:
        _require(fresh.get(stage, {}).get("status") == "succeeded"
                 and json.loads(fresh[stage]["settings"]) == tasks[stage]["settings"]
                 and json.loads(fresh[stage]["receipt"]) == tasks[stage]["receipt"],
                 "The meeting's processing status changed during review")
    current_route = route_settings(load_config(config_path).get("route", {}))
    _require(current_route.get("enabled") is True
             and preserves_company_map(current_route["companies"], frozen["companies"]),
             "Company folders changed during review")
    reviews = RoutingReviews(state)
    try:
        _require(reviews.get(job) == review, "The meeting's company review changed")
    finally:
        reviews.close()
    return snapshot
