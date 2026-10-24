"""Deterministic private-Drive filing through an injected connector transport.

This module chooses paths and validates receipts. The connector transport
receives one fixed operation at a time, including the destination and file list.
"""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import unicodedata

from .pipeline import (WorkflowError, NeedsConfiguration, RecoverableUploadError,
                       atomic_json, read_json, safe_member)


class UploadNeedsReview(NeedsConfiguration):
    pass


class UploadRetryable(WorkflowError):
    pass


FOLDER_MIME = "application/vnd.google-apps.folder"
DOCUMENT_MIME = "application/vnd.google-apps.document"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
COMBINED_TEXT_FORMAT = "combined_google_doc_v1"
NATIVE_TEMPLATE_FORMAT = "native_template_google_doc_v1"
# Codex rust-v0.150.0-alpha.8 local-file transfer limit; check before any write.
MAX_FILE_BYTES = 512 * 1024 * 1024
UNKNOWN_MEETING_FOLDER = "A meeting folder write has no saved Drive ID; confirm its ownership before retrying"
UNKNOWN_DOCUMENT = "A document import has no saved Drive ID; confirm its ownership before retrying"
UNKNOWN_TEMPLATE_DOCUMENT = "A native template copy has no saved Drive ID; confirm its ownership before retrying"
METADATA_FIELDS = "id,name,mimeType,parents,size,webViewLink,shared,permissions,driveId,capabilities(canShare)"
MIMES = {".caf": "audio/x-caf", ".mp4": "video/mp4", ".m4a": "audio/mp4",
         ".json": "application/json", ".md": "text/markdown", ".mkv": "video/x-matroska"}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def native_template_sha256(document):
    """Pin native template content/styles while ignoring identity and revisions."""
    def stable(value, root=False):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()
                    if key not in {"documentId", "document_url", "url", "revisionId"}
                    and not (root and key == "title")
                    # Connector versions may expose an absent optional native
                    # dropdown map as null. Actual definitions remain pinned.
                    and not (key == "dropdownDefinitions" and item is None)}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value
    return _hash(stable(document, root=True))


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", value):
        raise UploadNeedsReview("Expected a fixed Google Drive folder/file ID")
    return value


def _query_text(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _folder_name(date, job, title=None, meeting_timezone=None):
    """Use topic/participants then date; preserve the old name without a title."""
    from .meeting_dates import meeting_zone
    try:
        zone = meeting_zone(meeting_timezone) if meeting_timezone is not None else None
    except ValueError as error:
        raise UploadNeedsReview(str(error)) from error
    if title is not None and not isinstance(title, str):
        raise UploadNeedsReview("Meeting title must be text")
    if title:
        # The caller validates transcript evidence. This boundary also prevents
        # control characters and path separators in the supplied title.
        title = "".join(" " if unicodedata.category(char).startswith("C") else char
                        for char in unicodedata.normalize("NFC", title))
        title = " ".join(title.replace("/", "-").replace("\\", "-").split()).strip(" .-_")
        title = title.encode("utf-8")[:160].decode("utf-8", errors="ignore").rstrip()
        if title:
            local_date = date.astimezone(zone)
            date_label = f"{local_date.month}/{local_date.day}/{local_date:%y}"
            return (title + " - " + date_label).encode("utf-8")[:200].decode("utf-8", errors="ignore").rstrip()
    if zone is not None:
        local_date = date.astimezone(zone)
        return f"Meeting - {local_date.month}/{local_date.day}/{local_date:%y} - {job}"[:200]
    return (date.astimezone(timezone.utc).strftime("%Y-%m-%d %H-%M-%S UTC") + " - " + job)[:200]


def file_identity(path):
    """Hash the exact regular file while detecting replacement or modification."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise UploadNeedsReview("Upload sources must be nonempty regular files")
        digest = hashlib.sha256()
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(handle.fileno())
    current = Path(path).lstat()
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
    if identity(before) != identity(after) or identity(after) != identity(current) or not stat.S_ISREG(current.st_mode):
        raise UploadNeedsReview("An upload source changed while being checked")
    return {"size": before.st_size, "sha256": digest.hexdigest()}


def inventory(source, stage, text_format="legacy", document_path=None, *, allow_large_media=False):
    """No recursive uploads, logs, provenance files, or inferred media paths."""
    source = Path(source).expanduser()
    if source.is_symlink() or not source.is_dir():
        raise UploadNeedsReview("Upload source must be an explicit local directory")
    if text_format not in {"legacy", COMBINED_TEXT_FORMAT, NATIVE_TEMPLATE_FORMAT}:
        raise UploadNeedsReview("Unknown meeting text format")
    if stage == "publish" and text_format == NATIVE_TEMPLATE_FORMAT:
        raise UploadNeedsReview("Native templates require a validated replacement payload")
    if stage == "publish" and text_format == COMBINED_TEXT_FORMAT:
        if not isinstance(document_path, (str, Path)):
            raise UploadNeedsReview("The combined meeting document is missing")
        path = Path(document_path)
        if (not path.is_absolute() or path.is_symlink() or path.suffix.lower() != ".docx"
                or source.resolve() not in path.resolve().parents):
            raise UploadNeedsReview("The combined document must be a DOCX inside this job's results")
        identity = file_identity(path)
        if identity["size"] > MAX_FILE_BYTES:
            raise UploadNeedsReview("The combined document exceeds Codex's 512 MiB transfer limit")
        return [{"name": str(path.relative_to(source)), "mime_type": DOCUMENT_MIME,
                 "source_mime_type": DOCX_MIME, **identity}]
    if stage == "publish":
        names = ["notes.md", "notes.json"]
    elif stage == "archive":
        metadata = read_json(safe_member(source, "meta.json"), 100_000)
        files = metadata.get("files", {})
        if not metadata.get("started") or not metadata.get("ended") or not files.get("mic") or not files.get("system"):
            raise UploadNeedsReview("Only a finalized recording can be uploaded")
        names = [files["mic"], files["system"]]
        if text_format == "legacy":
            names.insert(0, "meta.json")
        if files.get("screen"):
            names.append(files["screen"])
        if text_format == "legacy":
            names.extend(name for name in ("transcript.json", "transcript.md") if (source / name).exists())
    else:
        raise UploadNeedsReview("Drive upload supports archive and publish only")
    entries = []
    for name in dict.fromkeys(names):
        path = safe_member(source, name)
        mime = MIMES.get(path.suffix.lower())
        if mime is None:
            raise UploadNeedsReview("Unsupported recording file type")
        if text_format in {COMBINED_TEXT_FORMAT, NATIVE_TEMPLATE_FORMAT} and stage == "archive" and not mime.startswith(("audio/", "video/")):
            raise UploadNeedsReview("The recording archive accepts only audio and video in the combined document format")
        identity = file_identity(path)
        if identity["size"] > MAX_FILE_BYTES and not (
                allow_large_media and stage == "archive" and mime.startswith(("audio/", "video/"))):
            raise UploadNeedsReview("A recording exceeds Codex's 512 MiB per-file transfer limit; keep it local or configure another transfer method")
        entries.append(dict(name=name, mime_type=mime, **identity))
    return sorted(entries, key=lambda item: item["name"])


def _normalized_text(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def native_document_text(document, identity):
    """Read the connector's native body in order, without trusting prose fields."""
    if document.get("documentId") != identity:
        raise UploadNeedsReview("Google Docs returned a different document")

    def content_text(content):
        if not isinstance(content, list):
            raise UploadNeedsReview("Google Docs did not return its complete document body")
        result = []
        for item in content:
            if not isinstance(item, dict):
                raise UploadNeedsReview("Google Docs returned an unreadable document element")
            paragraph = item.get("paragraph")
            if paragraph is not None:
                elements = paragraph.get("elements")
                if not isinstance(elements, list):
                    raise UploadNeedsReview("Google Docs returned an unreadable paragraph")
                chunks = []
                for element in elements:
                    if "textRun" in element:
                        text = element["textRun"].get("content")
                    elif "dateElement" in element:
                        text = element["dateElement"].get("dateElementProperties", {}).get("displayText")
                    else:
                        # Import produces text runs. Unknown native elements
                        # cannot be silently treated as verified transcript text.
                        text = ""
                    if not isinstance(text, str):
                        raise UploadNeedsReview("Google Docs returned unreadable paragraph text")
                    chunks.append(text)
                result.append("".join(chunks))
            table = item.get("table")
            if table is not None:
                for row in table.get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        result.extend(content_text(cell.get("content")))
        return result

    tabs = document.get("tabs")
    if tabs:
        if not isinstance(tabs, list) or len(tabs) != 1 or not isinstance(tabs[0], dict):
            raise UploadNeedsReview("The combined meeting document has an unexpected tab layout")
        content = tabs[0].get("body", {}).get("content")
    else:
        content = (document.get("body") or {}).get("content")
    return _normalized_text(" ".join(content_text(content)))


class DriveUploader:
    def __init__(self, state, invoke, meetings_root_id, guard=None,
                 large_upload=None, large_upload_preflight=None):
        self.state = Path(state).expanduser()
        self.invoke = invoke
        self.root_id = _id(meetings_root_id)
        self.guard = guard or (lambda: None)
        self.large_upload = large_upload
        self.large_upload_preflight = large_upload_preflight
        self._reserved_media_retry = False

    def _resumable_media(self, stage, file, operation):
        """Pin transport once; never turn an uncertain legacy write into a create."""
        op = operation or {}
        transport = op.get("transport")
        if transport not in (None, "google_drive_resumable", "codex_connector"):
            raise UploadNeedsReview("The saved Drive upload transport is unknown")
        saved = "resumable" in op
        if saved and (transport == "codex_connector" or not isinstance(op["resumable"], dict)
                      or not op["resumable"]):
            raise UploadNeedsReview("The saved resumable upload state needs review")
        if saved and op.get("id") and op["id"] != op["resumable"].get("file_id"):
            raise UploadNeedsReview("The saved upload receipt has a different file ID")
        direct = saved or transport == "google_drive_resumable"
        media = stage == "archive" and file["mime_type"].startswith(("audio/", "video/"))
        if direct:
            if not media or self.large_upload is None:
                raise UploadNeedsReview("The saved media upload requires its direct Drive connection")
            return True
        if (transport == "codex_connector" or op.get("id")
                or op.get("state", "planned") != "planned"):
            return False
        return media and self.large_upload is not None

    def _call(self, name, arguments):
        from .codex_drive_transport import DriveTimeoutError
        self.guard()
        try:
            result = self.invoke(name, arguments)
        except DriveTimeoutError as error:
            if self._reserved_media_retry and name in {"get_file_metadata", "search", "list_folder"}:
                raise RecoverableUploadError("Drive upload verification timed out; it will retry later") from error
            raise
        if not isinstance(result, dict):
            raise UploadRetryable("Drive returned an unreadable result")
        return result

    def _metadata(self, identity):
        result = self._call("get_file_metadata", {"fileId": _id(identity), "fields": METADATA_FIELDS})
        if result.get("id") != identity:
            raise UploadNeedsReview("Drive metadata does not match the requested item")
        return result

    def _private(self, item, owner=None, parent=None, name=None, mime=None):
        permissions = item.get("permissions")
        if (item.get("shared") is not False or item.get("current_user_can_share") is not True
                or item.get("drive_id") or not isinstance(permissions, list) or len(permissions) != 1):
            raise UploadNeedsReview("Drive destination privacy could not be verified as owner-only")
        permission = permissions[0]
        email = permission.get("emailAddress")
        if (permission.get("role") != "owner" or permission.get("type") != "user"
                or not isinstance(email, str) or not email or owner is not None and email.casefold() != owner.casefold()):
            raise UploadNeedsReview("Drive destination owner changed or is unavailable")
        if parent is not None and item.get("parent_ids") != [parent]:
            raise UploadNeedsReview("Drive destination moved outside its approved parent")
        if name is not None and item.get("title") != name:
            raise UploadNeedsReview("Drive destination name changed")
        if mime is not None and item.get("mime_type") != mime:
            raise UploadNeedsReview("Drive returned an unexpected file type")
        return email

    def _find(self, parent, name, folder):
        if not folder:
            # Search silently omits some MIME types, including application/json.
            # List this small, dedicated stage folder so retries see all files.
            result = self._call("list_folder", {
                "url": "https://drive.google.com/drive/folders/" + _id(parent), "top_k": 100})
            rows = result.get("files")
            if not isinstance(rows, list):
                raise UploadRetryable("Drive folder listing returned an unreadable result")
            if len(rows) >= 100:
                raise UploadNeedsReview("Drive folder listing may be incomplete; no upload was repeated")
            ids = []
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("title"), str):
                    raise UploadRetryable("Drive folder listing returned an unreadable item")
                identity = _id(row.get("id"))
                if row["title"] == name and identity not in ids:
                    ids.append(identity)
            return ids
        query = "trashed = false and '" + _query_text(parent) + "' in parents and name = '" + _query_text(name) + "'"
        if folder:
            query += " and mimeType = '" + FOLDER_MIME + "'"
        arguments = {"special_filter_query_str": query, "best_effort_fetch": False, "topn": 100}
        if folder:
            arguments["item_type"] = "folder"
        ids, tokens = [], set()
        for _ in range(10):
            result = self._call("search", arguments)
            rows = result.get("results")
            if not isinstance(rows, list):
                raise UploadRetryable("Drive search returned an unreadable result")
            for row in rows:
                if not isinstance(row, dict):
                    raise UploadRetryable("Drive search returned an unreadable item")
                identity = _id(row.get("id"))
                if identity not in ids:
                    ids.append(identity)
            token = result.get("next_page_token")
            if not token:
                if len(rows) >= 100:
                    raise UploadNeedsReview("Drive search may be incomplete; no upload was repeated")
                return ids
            if not isinstance(token, str) or token in tokens:
                raise UploadNeedsReview("Drive search pagination did not finish")
            tokens.add(token)
            arguments["page_token"] = token
        raise UploadNeedsReview("Drive search exceeded its bounded page limit")

    def _verify(self, identity, parent, name, owner, mime, size=None):
        item = self._metadata(identity)
        self._private(item, owner, parent, name, mime)
        if size is not None and str(item.get("size")) != str(size):
            raise UploadNeedsReview("Drive file size does not match the local recording")
        return item

    def upload(self, job, stage, source, route, started, meeting_title=None,
               text_format="legacy", document_path=None, document_title=None, document_text=None,
               template_document_id=None, template_sha256=None, template_payload=None,
               meeting_timezone=None):
        self._reserved_media_retry = False
        if not isinstance(job, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job):
            raise UploadNeedsReview("Expected a fixed meeting job ID")
        if not isinstance(route, dict) or not isinstance(route.get("company_name"), str):
            raise UploadNeedsReview("An approved company route is required")
        company_id = _id(route.get("drive_folder_id"))
        try:
            date = datetime.fromisoformat(started.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError("Missing timezone")
        except (ValueError, AttributeError):
            raise UploadNeedsReview("Meeting start time must be an explicit timestamp")
        source = Path(source).expanduser().absolute()
        template = stage == "publish" and text_format == NATIVE_TEMPLATE_FORMAT
        template_document = None
        if template:
            from quill_workflow.native_template import expected_text, plan_requests, verify_document
            _id(template_document_id)
            if (not isinstance(template_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", template_sha256)
                    or not isinstance(template_payload, dict) or source.is_symlink() or not source.is_dir()):
                raise UploadNeedsReview("Native template settings and payload must be frozen before publishing")
            files = [{"name": "native-template", "mime_type": DOCUMENT_MIME,
                      "payload_sha256": _hash(template_payload)}]
        else:
            files = inventory(source, stage, text_format, document_path,
                              allow_large_media=self.large_upload is not None)
        expected_document = None
        if stage == "publish" and text_format == COMBINED_TEXT_FORMAT:
            if (not isinstance(document_text, list) or not document_text
                    or any(not isinstance(text, str) for text in document_text)):
                raise UploadNeedsReview("Combined document text is required for native readback")
            expected_document = _normalized_text(" ".join(document_text))
            if not expected_document:
                raise UploadNeedsReview("The combined meeting document is empty")
        if expected_document is not None or template:
            if (not isinstance(document_title, str) or not document_title.strip()
                    or len(document_title.encode("utf-8")) > 250
                    or any(unicodedata.category(char).startswith("C") for char in document_title)):
                raise UploadNeedsReview("A bounded combined document title is required")
        proposed_name = _folder_name(date, job, meeting_title, meeting_timezone)
        directory = self.state / "results" / job
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = directory / "drive-upload.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = directory / ("drive-" + stage + ".json")
            identity = {"job": job, "stage": stage, "idempotency_key": job + ":" + stage,
                        "meetings_root_id": self.root_id, "route": route, "started": started,
                        "files": files, "source": str(source)}
            if meeting_timezone is not None:
                identity["meeting_timezone"] = meeting_timezone
            if text_format != "legacy":
                identity["text_format"] = text_format
                if expected_document is not None:
                    identity["document_title"] = document_title
                    identity["document_text_sha256"] = hashlib.sha256(expected_document.encode("utf-8")).hexdigest()
                if template:
                    identity.update(template_document_id=template_document_id,
                                    template_sha256=template_sha256, document_title=document_title)
            signature = _hash(identity)
            if path.exists():
                journal = read_json(path)
                if journal.get("identity_sha256") != signature or journal.get("identity") != identity:
                    raise UploadNeedsReview("Upload inputs or destination changed; create an explicit revision")
            else:
                journal = {"schema_version": 1, "identity": identity, "identity_sha256": signature,
                           "owner": None, "operations": {}, "created_at": time.time()}
                atomic_json(path, journal)

            # Choose from the durable operation, not just the file's size. Old
            # connector receipts remain verifiable without new credentials.
            direct_files = [file for file in files if self._resumable_media(
                stage, file, journal["operations"].get("file:" + file["name"]))]
            for file in direct_files:
                saved = journal["operations"].get("file:" + file["name"], {}).get("resumable", {})
                if saved.get("file_id"):
                    _id(saved["file_id"])
                    self._reserved_media_retry = True
            direct_account = None
            if direct_files:
                if not callable(self.large_upload_preflight):
                    raise UploadNeedsReview("Direct media uploads require an explicit Drive connection")
                self.guard()
                direct_account = self.large_upload_preflight()
                if (not isinstance(direct_account, dict) or not isinstance(direct_account.get("email"), str)
                        or not direct_account["email"].strip()):
                    raise UploadNeedsReview("The direct Drive account could not be verified")

            meeting_path = directory / "drive-meeting.json"
            meeting_identity = {key: identity[key] for key in ("job", "meetings_root_id", "route", "started")}
            if meeting_timezone is not None:
                meeting_identity["meeting_timezone"] = meeting_timezone
            if meeting_path.exists():
                meeting = read_json(meeting_path)
                if meeting.get("identity") != meeting_identity:
                    raise UploadNeedsReview("The meeting folder belongs to a different approved route")
                # Legacy journals used UTC plus the job ID. Preserve that name
                # even if notes gained a descriptive title after their upload.
                folder_name = meeting.get("folder_name", _folder_name(date, job))
            else:
                folder_name = proposed_name
                meeting = {"schema_version": 1, "identity": meeting_identity, "owner": None,
                           "operation": None, "folder_name": folder_name}
                atomic_json(meeting_path, meeting)
            if not isinstance(folder_name, str) or not folder_name or len(folder_name.encode("utf-8")) > 200:
                raise UploadNeedsReview("Saved meeting folder name is invalid")
            meeting["folder_name"] = folder_name
            if meeting["operation"] is not None:
                journal["operations"]["meeting"] = meeting["operation"]

            def save():
                journal["updated_at"] = time.time()
                atomic_json(path, journal)
                meeting["operation"] = journal["operations"].get("meeting")
                meeting["owner"] = journal["owner"]
                atomic_json(meeting_path, meeting)

            self.guard()
            root = self._metadata(self.root_id)
            owner = self._private(root, meeting["owner"] or journal["owner"], mime=FOLDER_MIME)
            if direct_account and direct_account["email"].casefold() != owner.casefold():
                raise UploadNeedsReview("The direct Drive account must match the approved Meetings folder owner")
            # Search filters verify liveness because normalized metadata omits
            # Google's `trashed` field. Root was explicitly created in My Drive.
            if self.root_id not in self._find("root", root["title"], True):
                raise UploadNeedsReview("The approved Meetings root is unavailable")
            self._verify(company_id, self.root_id, route["company_name"], owner, FOLDER_MIME)
            if company_id not in self._find(self.root_id, route["company_name"], True):
                raise UploadNeedsReview("The approved company folder is unavailable")
            journal["owner"] = owner
            save()

            if template:
                template_info = self._metadata(template_document_id)
                self._private(template_info, owner, mime=DOCUMENT_MIME)
                template_document = self._call("get_document", {"document_id": template_document_id})
                if (template_document.get("documentId") != template_document_id
                        or native_template_sha256(template_document) != template_sha256):
                    raise UploadNeedsReview("The saved native template changed; create an explicit template revision")
                try:
                    # Validate every placeholder and the complete payload before
                    # creating even the destination folders.
                    expected_document = expected_text(template_document, template_payload)
                    plan_requests(template_document, template_payload)
                except ValueError as error:
                    raise UploadNeedsReview(str(error)) from error

            ancestors = {company_id: (self.root_id, route["company_name"])}

            def validate_ancestors():
                self.guard()
                self._private(self._metadata(self.root_id), owner, mime=FOLDER_MIME)
                for identity, (parent, name) in ancestors.items():
                    self._verify(identity, parent, name, owner, FOLDER_MIME)

            def fill_template(known, op):
                """A saved copy ID precedes edits; unknown edits are read back."""
                current = self._call("get_document", {"document_id": known})
                if current.get("documentId") != known:
                    raise UploadNeedsReview("Native template copy readback returned a different document")
                fill = op.get("fill")
                if fill:
                    try:
                        if verify_document(current, template_payload, fill["template_document"]) is not True:
                            raise ValueError("Native document verifier did not confirm completion")
                    except ValueError:
                        if (fill.get("state") in {"acknowledged", "verified"}
                                or current.get("revisionId") != fill.get("revision")):
                            raise UploadNeedsReview("The copied meeting document changed during editing; review its saved copy")
                    else:
                        fill.update(state="verified", verified_at=time.time(), reconciled=fill.get("state") == "dispatched")
                        save()
                        return
                else:
                    revision = current.get("revisionId")
                    if not isinstance(revision, str) or not revision:
                        raise UploadNeedsReview("Native template editing requires the copied document revision")
                    if native_template_sha256(current) != template_sha256:
                        raise UploadNeedsReview("The native copy differs from the frozen template; review its saved copy")
                    try:
                        requests = plan_requests(current, template_payload)
                        if expected_text(current, template_payload) != expected_document:
                            raise ValueError("The native copy no longer matches the frozen template")
                    except ValueError as error:
                        raise UploadNeedsReview(str(error)) from error
                    fill = {"state": "planned", "revision": revision, "requests": requests,
                            "template_document": current}
                    op["fill"] = fill
                    save()
                validate_ancestors()
                fill.update(state="dispatched", dispatched_at=time.time())
                save()
                try:
                    self._call("batch_update_document", {"document_id": known, "requests": fill["requests"],
                               "write_control": {"requiredRevisionId": fill["revision"]}})
                except Exception as error:
                    if getattr(error, "outcome_unknown", True) is False:
                        fill["state"] = "planned"
                        save()
                    # Keep the saved ID and plan. The next attempt must read
                    # back this exact document before deciding whether to edit.
                    raise
                fill.update(state="acknowledged", acknowledged_at=time.time())
                save()
                actual = self._call("get_document", {"document_id": known})
                if actual.get("documentId") != known:
                    raise UploadNeedsReview("Native meeting document readback returned a different document")
                try:
                    if verify_document(actual, template_payload, fill["template_document"]) is not True:
                        raise ValueError("Native document verifier did not confirm completion")
                except ValueError as error:
                    raise UploadNeedsReview("Native meeting document readback failed: " + str(error)) from error
                fill.update(state="verified", verified_at=time.time())
                save()

            def ensure(key, parent, name, mime, file=None):
                op = journal["operations"].setdefault(key, {"state": "planned", "name": name, "parent": parent})
                if op["name"] != name or op["parent"] != parent:
                    raise UploadNeedsReview("Saved upload operation no longer matches its destination")
                # A readable title can belong to another job with the same
                # start time. A name match cannot prove ownership after a lost
                # response; only legacy names include the complete job ID.
                native_doc = mime == DOCUMENT_MIME
                needs_remote_id = native_doc or key == "meeting" and folder_name != _folder_name(date, job)
                unknown_message = (UNKNOWN_TEMPLATE_DOCUMENT if native_doc and template else
                                   UNKNOWN_DOCUMENT if native_doc else UNKNOWN_MEETING_FOLDER)
                # Validate parent before every mutation, including inherited
                # access. No operation can change permissions or delete files.
                validate_ancestors()
                ids = self._find(parent, name, mime == FOLDER_MIME)
                if len(ids) > 1:
                    raise UploadNeedsReview("Multiple matching Drive items need review; no duplicate was created")
                direct = file is not None and self._resumable_media(stage, file, op)
                changed = False
                if file and "transport" not in op:
                    op["transport"] = "google_drive_resumable" if direct else "codex_connector"
                    changed = True
                if direct and op.get("state") == "planned":
                    # Older versions do not understand the transport marker.
                    # Their uncertain-write guard must also stop a fresh upload
                    # if this source is rolled back during a partial transfer.
                    op.update(state="dispatched", dispatched_at=time.time())
                    changed = True
                if changed:
                    save()  # Persist before dispatch, even before reserving an ID.
                if direct:
                    from .google_drive_auth import DriveAuthRetryable
                    # This provider owns its saved ID/session. A lost response
                    # must resume that exact operation, never adopt a name match.
                    saved = op.get("resumable", {})
                    reserved = saved.get("file_id") if isinstance(saved, dict) else None
                    if ids and ids != [reserved]:
                        raise UploadNeedsReview("A matching Drive item is not owned by this resumable upload")
                    try:
                        result = self.large_upload.upload(
                            source=source / file["name"], file=file, parent=parent,
                            name=name, mime=mime, owner=owner, operation=op,
                            save=save, guard=self.guard)
                    except (UploadRetryable, DriveAuthRetryable) as error:
                        saved = op.get("resumable")
                        if isinstance(saved, dict) and saved.get("file_id"):
                            _id(saved["file_id"])
                            # Only this reserved-ID operation can keep retrying
                            # through a prolonged outage without a new create.
                            raise RecoverableUploadError(str(error)) from error
                        raise
                    if not isinstance(result, dict) or result.get("success") is not True:
                        raise UploadRetryable("Resumable upload returned no verified completion")
                    known = _id(result.get("id"))
                    self._reserved_media_retry = True
                    item = self._verify(known, parent, name, owner, mime, file["size"])
                    op.update(id=known, state="verified", verified_at=time.time(),
                              url=item.get("url"), verification="metadata_and_checksum")
                    save()
                    return known, item
                known = op.get("id")
                if known:
                    if ids != [known]:
                        raise UploadNeedsReview("Previously uploaded Drive item moved, changed name, or is unavailable")
                elif op["state"] == "dispatched":
                    if needs_remote_id:
                        raise UploadNeedsReview(unknown_message)
                    if not ids:
                        raise UploadRetryable("Waiting to confirm a previous Drive write; it will not be blindly repeated")
                    known = ids[0]
                    op.update(id=known, reconciled=True)
                    save()
                elif ids:
                    raise UploadNeedsReview("A matching Drive item exists without this upload's receipt")
                else:
                    arguments = {"name": name, "parent_folder": parent}
                    tool = "create_folder"
                    if file:
                        if template and native_doc:
                            arguments = {"url": "https://docs.google.com/document/d/" + template_document_id + "/edit",
                                         "new_title": name, "parent_folder": parent}
                            tool = "copy_file"
                        elif file_identity(source / file["name"]) != {key: file[key] for key in ("size", "sha256")}:
                            raise UploadNeedsReview("Local file changed before upload")
                        elif native_doc:
                            arguments = {"source_file": str(source / file["name"]), "title": name,
                                         "parent_folder_id": parent, "upload_mode": "native_google_docs"}
                            tool = "import_document"
                        else:
                            arguments = {"file_uri": str(source / file["name"]), "file_name": name,
                                         "mime_type": mime, "parent_folder_id": parent}
                            tool = "upload_file"
                    # Searches and hashing can take time. Check the chain again
                    # immediately before dispatching the mutation.
                    validate_ancestors()
                    op.update(state="dispatched", dispatched_at=time.time())
                    save()
                    try:
                        result = self._call(tool, arguments)
                    except Exception as error:
                        if getattr(error, "outcome_unknown", True) is False:
                            op["state"] = "planned"
                            save()
                        elif needs_remote_id:
                            raise UploadNeedsReview(unknown_message) from error
                        raise
                    if result.get("success") is not True or not result.get("id"):
                        if needs_remote_id:
                            raise UploadNeedsReview(unknown_message)
                        raise UploadRetryable("Drive write returned no confirmed file identity")
                    known = _id(result["id"])
                    op.update(id=known, state="acknowledged", acknowledged_at=time.time())
                    save()
                    if file and not template and file_identity(source / file["name"]) != {key: file[key] for key in ("size", "sha256")}:
                        raise UploadNeedsReview("Local source changed during upload; its Drive copy needs review")
                item = self._verify(known, parent, name, owner, mime, file["size"] if file and not native_doc else None)
                verification = "metadata"
                if native_doc and template:
                    fill_template(known, op)
                    item = self._verify(known, parent, name, owner, mime)
                    verification = "metadata_and_template_content"
                elif native_doc:
                    actual = native_document_text(self._call("get_document", {"document_id": known}), known)
                    if actual != expected_document:
                        raise UploadNeedsReview("Google Docs readback does not match the complete notes and transcript")
                    verification = "metadata_and_document_text"
                op.update(state="verified", verified_at=time.time(), url=item.get("url"), verification=verification)
                save()
                return known, item

            meeting_id, meeting_info = ensure("meeting", company_id, folder_name, FOLDER_MIME)
            ancestors[meeting_id] = (company_id, folder_name)
            label = "Recordings" if stage == "archive" else "Notes"
            stage_id, stage_info = ensure("stage", meeting_id, label, FOLDER_MIME)
            ancestors[stage_id] = (meeting_id, label)
            uploaded = []
            for file in files:
                remote_name = document_title if file["mime_type"] == DOCUMENT_MIME else file["name"]
                identity, item = ensure("file:" + file["name"], stage_id, remote_name, file["mime_type"], file)
                uploaded.append(dict(file, id=identity, url=item.get("url"),
                                     verification=journal["operations"]["file:" + file["name"]]["verification"],
                                     reconciled=journal["operations"]["file:" + file["name"]].get("reconciled", False)))
            self.guard()
            self._private(self._metadata(self.root_id), owner, mime=FOLDER_MIME)
            self._verify(company_id, self.root_id, route["company_name"], owner, FOLDER_MIME)
            self._verify(meeting_id, company_id, folder_name, owner, FOLDER_MIME)
            self._verify(stage_id, meeting_id, label, owner, FOLDER_MIME)
            receipt = {"job": job, "stage": stage, "idempotency_key": job + ":" + stage,
                       "destination": stage_info.get("url") or stage_id, "folder_id": stage_id,
                       "meeting_folder_id": meeting_id, "meeting_folder_name": folder_name,
                       "meeting_folder_url": meeting_info.get("url"),
                       "files": uploaded, "verification": "metadata_and_template_content" if template else "metadata_and_document_text" if expected_document is not None else "per_file" if direct_files else "metadata",
                       "verified_at": time.time()}
            journal["receipt"] = receipt
            save()
            return receipt
