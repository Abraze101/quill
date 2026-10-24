"""Bounded, journaled Drive media uploads. Never edits permissions or existing files.

Protocol: https://developers.google.com/workspace/drive/api/guides/manage-uploads
Reserved IDs make a lost final response recoverable without another Drive file.
"""
from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from .drive_upload import UploadNeedsReview, UploadRetryable
from .pipeline import NeedsConfiguration

CHUNK_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
API = "https://www.googleapis.com/drive/v3/files"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
FIELDS = ("id,name,mimeType,parents,size,md5Checksum,appProperties,trashed,shared,"
          "driveId,permissions(type,role,emailAddress,deleted,pendingOwner),"
          "owners(emailAddress,me),capabilities(canShare)")


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: dict
    body: bytes = b""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_request(method, url, *, headers, body, timeout):
    # Explicit proxy disabling avoids forwarding bearer tokens via an ambient
    # proxy. TLS certificate verification remains urllib's default.
    from urllib.request import ProxyHandler
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(url, data=body, headers=headers, method=method)
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise UploadRetryable("Drive returned an oversized response")
        return HTTPResponse(response.code, dict(response.headers), data)


def _json(response):
    try:
        value = json.loads(response.body)
    except (ValueError, TypeError):
        raise UploadRetryable("Drive returned unreadable metadata") from None
    if not isinstance(value, dict):
        raise UploadRetryable("Drive returned unreadable metadata")
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", value):
        raise UploadNeedsReview("The saved resumable Drive ID is invalid")
    return value


def _session_url(url):
    try:
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        valid = (parts.scheme == "https" and parts.netloc == "www.googleapis.com"
                 and parts.path == "/upload/drive/v3/files" and not parts.fragment
                 and query.get("uploadType") == ["resumable"]
                 and len(query.get("upload_id", [])) == 1 and query["upload_id"][0]
                 and len(url) <= 16384 and not any(ord(c) < 33 or ord(c) > 126 for c in url))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise UploadNeedsReview("Drive returned an untrusted resumable upload address")
    return url


def _private(item, owner):
    permissions, owners, capabilities = item.get("permissions"), item.get("owners"), item.get("capabilities")
    if (item.get("trashed") is not False or item.get("shared") is not False or item.get("driveId")
            or not isinstance(capabilities, dict) or capabilities.get("canShare") is not True
            or not isinstance(permissions, list) or len(permissions) != 1
            or not isinstance(owners, list) or len(owners) != 1):
        return False
    permission, user = permissions[0], owners[0]
    return (isinstance(permission, dict) and isinstance(user, dict)
            and permission.get("type") == "user" and permission.get("role") == "owner"
            and permission.get("deleted") is not True and permission.get("pendingOwner") is not True
            and str(permission.get("emailAddress", "")).casefold() == owner.casefold()
            and str(user.get("emailAddress", "")).casefold() == owner.casefold()
            and user.get("me") is True)


def _stat_key(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


class ResumableDriveUploader:
    """request is injectable; it receives bounded bytes and returns HTTPResponse.

    Transient failures escape to the workflow's backoff retry scheduler. Every
    later attempt queries the server before sending more data. Session URLs are
    credentials: the caller must save its journal privately and never log it.
    """
    def __init__(self, access_token, request=None, *, clock=time.monotonic, deadline_seconds=1200):
        self.access_token = access_token
        self.request = request or _http_request
        self.clock = clock
        self.deadline_seconds = deadline_seconds

    def _token(self):
        token = self.access_token()
        if (not isinstance(token, str) or not token or len(token) > 16384
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise NeedsConfiguration("Google Drive upload authorization is unavailable")
        return token

    def preflight(self):
        self._token()

    @staticmethod
    def _failure(response):
        if response.status == 401:
            raise NeedsConfiguration("Google Drive upload authorization expired; reconnect the upload account")
        if response.status == 403:
            try:
                reasons = {item.get("reason") for item in _json(response).get("error", {}).get("errors", [])}
            except (AttributeError, TypeError, UploadRetryable):
                reasons = set()
            if reasons & {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded", "backendError"}:
                raise UploadRetryable("Google Drive temporarily limited this upload; it will resume later")
            raise NeedsConfiguration("Google Drive refused the upload; check the upload account and access")
        if response.status in {408, 429} or response.status >= 500:
            raise UploadRetryable("Google Drive upload was interrupted; it will resume later")
        raise UploadNeedsReview("Google Drive rejected the resumable upload; review its saved state")

    def upload(self, *, source, file, parent, name, mime, owner, operation, save, guard):
        source = Path(source).absolute()
        _id(parent)
        if (not isinstance(file, dict) or type(file.get("size")) is not int or file["size"] <= 0
                or not isinstance(file.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", file["sha256"])
                or not isinstance(name, str) or not name
                or not isinstance(mime, str) or not re.fullmatch(r"[\w.+-]+/[\w.+-]+", mime)
                or mime.startswith("application/vnd.google-apps.")
                or not isinstance(owner, str) or not owner):
            raise UploadNeedsReview("The resumable upload identity is invalid")
        identity = {"source": str(source), "size": file["size"], "sha256": file["sha256"],
                    "parent": parent, "name": name, "mime": mime, "owner": owner.casefold()}
        deadline = self.clock() + self.deadline_seconds
        guard()
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            raise UploadNeedsReview("The recording is unavailable or is not a regular file") from None
        with os.fdopen(descriptor, "rb") as handle:
            initial = os.fstat(handle.fileno())
            if not stat.S_ISREG(initial.st_mode) or initial.st_size != file["size"]:
                raise UploadNeedsReview("The recording changed before resumable upload")

            def stable():
                try:
                    current = source.lstat()
                    opened = os.fstat(handle.fileno())
                except OSError:
                    raise UploadNeedsReview("The recording changed during resumable upload") from None
                if (not stat.S_ISREG(current.st_mode) or _stat_key(current) != _stat_key(initial)
                        or _stat_key(opened) != _stat_key(initial)):
                    raise UploadNeedsReview("The recording changed during resumable upload")
                if self.clock() >= deadline:
                    raise UploadRetryable("The upload reached its time limit; it will resume later")

            sha, md5 = hashlib.sha256(), hashlib.md5()
            while True:
                stable()
                block = handle.read(CHUNK_BYTES)
                if not block:
                    break
                sha.update(block)
                md5.update(block)
            stable()
            if sha.hexdigest() != file["sha256"]:
                raise UploadNeedsReview("The recording content changed before resumable upload")
            checksum = md5.hexdigest()
            state = operation.get("resumable")
            if state is None:
                state = {"schema_version": 1, "identity": identity, "md5": checksum,
                         "marker": uuid.uuid4().hex}
                operation["resumable"] = state
                save()
            elif (not isinstance(state, dict) or state.get("schema_version") != 1
                  or state.get("identity") != identity or state.get("md5") != checksum
                  or not re.fullmatch(r"[a-f0-9]{32}", str(state.get("marker", "")))):
                raise UploadNeedsReview("The recording or destination changed since this upload was started")

            def call(method, url, body=None, headers=None):
                stable()
                guard()
                token = self._token()
                # The only variable endpoint is separately checked as a session.
                if not (url.startswith(API + "/") or url.startswith(UPLOAD_API + "?")):
                    raise UploadNeedsReview("The Drive upload endpoint is invalid")
                parts = urlsplit(url)
                if parts.scheme != "https" or parts.netloc != "www.googleapis.com" or parts.fragment:
                    raise UploadNeedsReview("The Drive upload endpoint is invalid")
                request_headers = {"Authorization": "Bearer " + token, **(headers or {})}
                if body is not None:
                    request_headers["Content-Length"] = str(len(body))
                stable()
                guard()
                try:
                    response = self.request(method, url, headers=request_headers, body=body,
                                            timeout=max(1, min(60, deadline - self.clock())))
                except (OSError, URLError, HTTPException):
                    raise UploadRetryable("Google Drive upload was interrupted; it will resume later") from None
                stable()
                if not isinstance(response, HTTPResponse) or len(response.body) > MAX_RESPONSE_BYTES:
                    raise UploadRetryable("Drive returned an unreadable upload response")
                return response

            if not state.get("file_id"):
                response = call("GET", API + "/generateIds?count=1&space=drive&type=files")
                if response.status != 200:
                    self._failure(response)
                ids = _json(response).get("ids")
                if not isinstance(ids, list) or len(ids) != 1:
                    raise UploadRetryable("Drive did not return one reserved file ID")
                state["file_id"] = _id(ids[0])
                save()  # A create is forbidden until this ID is durable.
            reserved = _id(state["file_id"])
            if operation.get("id") not in {None, reserved}:
                raise UploadNeedsReview("The saved upload receipt has a different file ID")
            metadata_url = API + "/" + reserved + "?" + urlencode({"fields": FIELDS})
            properties = {"quillUpload": state["marker"], "quillSha256": file["sha256"]}

            def verify_parent():
                response = call("GET", API + "/" + parent + "?" + urlencode({"fields": FIELDS}))
                if response.status == 404:
                    raise UploadNeedsReview("The private Drive upload folder is unavailable")
                if response.status != 200:
                    self._failure(response)
                item = _json(response)
                if (item.get("id") != parent or item.get("mimeType") != "application/vnd.google-apps.folder"
                        or not _private(item, owner)):
                    raise UploadNeedsReview("The Drive upload folder is no longer private to the approved account")

            def complete_if_present(required=False):
                response = call("GET", metadata_url)
                if response.status == 404:
                    if required or state.get("complete") or operation.get("id"):
                        raise UploadRetryable("Waiting to verify the completed Drive upload")
                    return False
                if response.status != 200:
                    self._failure(response)
                item = _json(response)
                if (not _private(item, owner) or item.get("id") != reserved or item.get("name") != name
                        or item.get("parents") != [parent] or item.get("mimeType") != mime
                        or item.get("appProperties") != properties
                        or str(item.get("size")) != str(file["size"])
                        or item.get("md5Checksum") != checksum):
                    raise UploadNeedsReview("The reserved Drive file does not match this private recording upload")
                verify_parent()
                state.update(complete=True, offset=file["size"])
                operation["id"] = reserved  # Only a verified complete file reaches the outer receipt.
                save()
                return True

            verify_parent()
            if complete_if_present():
                return {"success": True, "id": reserved}
            offset = 0
            session = state.get("session_uri")
            if session:
                _session_url(session)
                response = call("PUT", session, b"", {"Content-Range": f"bytes */{file['size']}"})
                if response.status in {200, 201}:
                    complete_if_present(required=True)
                    return {"success": True, "id": reserved}
                if response.status == 404:
                    if complete_if_present():
                        return {"success": True, "id": reserved}
                    # Session expired. Retain the reserved ID so restart cannot
                    # create a second file, even if completion raced this read.
                    state.pop("session_uri", None)
                    state["offset"] = 0
                    save()
                    session = None
                elif response.status == 308:
                    offset = self._offset(response, file["size"])
                else:
                    self._failure(response)
            if not session:
                payload = json.dumps({"id": reserved, "name": name, "parents": [parent],
                                      "mimeType": mime, "appProperties": properties}).encode()
                response = call("POST", UPLOAD_API + "?uploadType=resumable", payload,
                                {"Content-Type": "application/json; charset=UTF-8",
                                 "X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(file["size"])})
                if response.status == 409:
                    if complete_if_present():
                        return {"success": True, "id": reserved}
                    raise UploadRetryable("The reserved Drive upload is awaiting reconciliation")
                if response.status not in {200, 201}:
                    self._failure(response)
                session = _session_url({k.lower(): v for k, v in response.headers.items()}.get("location"))
                state["session_uri"] = session
                state["offset"] = 0
                save()  # Persist the bearer-like session before sending media.
            while offset < file["size"]:
                verify_parent()
                stable()
                handle.seek(offset)
                block = handle.read(min(CHUNK_BYTES, file["size"] - offset))
                if not block:
                    raise UploadNeedsReview("The recording changed during resumable upload")
                response = call("PUT", session, block,
                                {"Content-Type": mime,
                                 "Content-Range": f"bytes {offset}-{offset + len(block) - 1}/{file['size']}"})
                if response.status in {200, 201}:
                    complete_if_present(required=True)
                    return {"success": True, "id": reserved}
                if response.status == 404:
                    # Keep exact session until next attempt queries it, then
                    # reconciles the reserved ID before restarting.
                    raise UploadRetryable("The Drive upload session expired; it will resume later")
                if response.status != 308:
                    self._failure(response)
                next_offset = self._offset(response, file["size"])
                if next_offset <= offset or next_offset > offset + len(block):
                    raise UploadRetryable("Drive did not confirm upload progress; it will be checked before retrying")
                offset = next_offset
                state["offset"] = offset
                save()
            # A 308 claiming all bytes is not a completion receipt.
            complete_if_present(required=True)
            return {"success": True, "id": reserved}

    @staticmethod
    def _offset(response, total):
        value = {k.lower(): v for k, v in response.headers.items()}.get("range")
        if value is None:
            return 0
        match = re.fullmatch(r"bytes=0-([0-9]+)", str(value))
        if not match or int(match[1]) >= total:
            raise UploadNeedsReview("Drive returned an invalid resumable upload offset")
        return int(match[1]) + 1
