"""Explicit Drive account selection; verifies destinations without creating files.

This setup boundary only changes connection fields in the workflow config.
Activation flags, queued jobs, receipts, and upload journals remain untouched.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from .pipeline import NeedsConfiguration, atomic_json, large_files_configured

DEFAULT_CODEX = "/Applications/Codex.app/Contents/Resources/codex"
MAX_CONFIG_BYTES = 100_000
_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")
_PROFILE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_LINK = re.compile(r"link_[A-Za-z0-9_-]{1,200}")
_REVISION = re.compile(r"[a-f0-9]{64}")


def _email(value):
    return (isinstance(value, str) and len(value) <= 254
            and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) is not None
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _configured_stages(config):
    stages = {}
    for name in ("archive", "publish"):
        value = config.get(name, {})
        if not isinstance(value, dict):
            raise NeedsConfiguration("The Drive settings need review before choosing an account.")
        if value.get("type") == "codex_drive":
            stages[name] = value
    return stages


def require_verified_account(config, settings=None):
    """Validate the saved selection without accessing Drive or changing state.

Use for new job activation and before external operations. Passing frozen
stage settings rejects a later live connection change rather than migrating
the job. Historical receipt readers deliberately do not call this helper.
"""
    if not isinstance(config, dict):
        raise NeedsConfiguration("Choose and verify a Google Drive account in Quill Settings.")
    account = config.get("drive_account")
    if (not isinstance(account, dict) or not isinstance(account.get("link_id"), str)
            or not _LINK.fullmatch(account["link_id"]) or not _email(account.get("email"))
            or not isinstance(account.get("profile_id"), str)
            or not _PROFILE_ID.fullmatch(account["profile_id"])
            or not isinstance(account.get("meetings_root_id"), str)
            or not _ID.fullmatch(account["meetings_root_id"])
            or type(account.get("verified_at")) not in (int, float)
            or not math.isfinite(account["verified_at"]) or account["verified_at"] <= 0):
        raise NeedsConfiguration("Choose and verify a Google Drive account in Quill Settings.")
    selected = [settings] if settings is not None else list(_configured_stages(config).values())
    if not selected:
        raise NeedsConfiguration("Finish the Meetings folder configuration before choosing a Drive account.")
    for stage in selected:
        if (not isinstance(stage, dict) or stage.get("type") != "codex_drive"
                or stage.get("account_link_id") != account["link_id"]
                or stage.get("meetings_root_id") != account["meetings_root_id"]):
            raise NeedsConfiguration("The Drive account or Meetings folder changed. Verify the connection in Quill Settings.")
    return dict(account)


def _snapshot(config_path, *, missing_ok=False):
    path = Path(config_path).expanduser().absolute()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if missing_ok:
            return path, {}, hashlib.sha256(b"").hexdigest()
        raise NeedsConfiguration("Finish the Meetings folder configuration before choosing a Drive account.") from None
    except OSError:
        raise NeedsConfiguration("The workflow settings must be a readable regular file.") from None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_size > MAX_CONFIG_BYTES):
                raise NeedsConfiguration("The workflow settings must be an owned regular file under 100 KB.")
            raw = handle.read(MAX_CONFIG_BYTES + 1)
            after = os.fstat(handle.fileno())
        current = path.lstat()
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if (len(raw) > MAX_CONFIG_BYTES or identity(before) != identity(after)
                or identity(after) != identity(current)):
            raise NeedsConfiguration("The workflow settings changed. Refresh the account list and try again.")
        def nonfinite(_value):
            raise ValueError("Non-finite settings")
        config = json.loads(raw, parse_constant=nonfinite)
        if not isinstance(config, dict):
            raise ValueError("Not an object")
        _configured_stages(config)
        return path, config, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, UnicodeError):
        raise NeedsConfiguration("The workflow settings could not be read. Review their configuration and try again.") from None


def _transport(config, factory=None, *, link_id=None):
    from .codex_drive_transport import CodexDriveTransport
    stages = _configured_stages(config)
    settings = stages.get("publish", stages.get("archive", {}))
    executable = settings.get("codex", DEFAULT_CODEX)
    if not isinstance(executable, str) or not executable.startswith("/"):
        raise NeedsConfiguration("Configure the installed Codex application before choosing a Drive account.")
    timeout = settings.get("timeout_seconds", 180)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise NeedsConfiguration("The Drive connection timeout needs configuration.")
    return (factory or CodexDriveTransport)(executable, model=settings.get("model"),
                                           timeout=min(180, timeout), account_link_id=link_id)


def list_accounts(config_path, *, transport_factory=None):
    """Return choices and the config revision; never infer or save a selection."""
    _path, config, revision = _snapshot(config_path, missing_ok=True)
    accounts = _transport(config, transport_factory).discover_accounts()
    selected = {stage.get("account_link_id") for stage in _configured_stages(config).values()
                if isinstance(stage.get("account_link_id"), str)}
    selected_link_id = next(iter(selected)) if len(selected) == 1 else None
    return {"accounts": accounts, "revision": revision, "selected_link_id": selected_link_id}


@contextmanager
def _locked(path, busy_message):
    path = Path(path)
    if path.parent.is_symlink():
        raise NeedsConfiguration("The workflow settings location needs review.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise NeedsConfiguration("The workflow lock must be an owned regular file.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NeedsConfiguration(busy_message) from None
        yield
    finally:
        os.close(descriptor)


def select_account(config_path, state, link_id, revision, *, expected_profile_id,
                   expected_email, transport_factory=None, auth_factory=None, clock=time.time):
    """Verify one explicitly chosen account and atomically save its binding."""
    if not isinstance(link_id, str) or not _LINK.fullmatch(link_id):
        raise NeedsConfiguration("Choose a connected Google Drive account from the list.")
    if (not isinstance(expected_profile_id, str) or not _PROFILE_ID.fullmatch(expected_profile_id)
            or not _email(expected_email)):
        raise NeedsConfiguration("Refresh the account list before choosing an account.")
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise NeedsConfiguration("Refresh the account list before choosing an account.")
    path, config, current_revision = _snapshot(config_path)
    if revision != current_revision:
        raise NeedsConfiguration("The workflow settings changed. Refresh the account list and try again.")
    stages = _configured_stages(config)
    roots = {stage.get("meetings_root_id") for stage in stages.values()
             if isinstance(stage.get("meetings_root_id"), str)}
    if (not stages or len(roots) != 1 or not all(isinstance(stage.get("meetings_root_id"), str)
            and _ID.fullmatch(stage["meetings_root_id"]) for stage in stages.values())):
        raise NeedsConfiguration("Finish the Meetings folder configuration first. Both Drive outputs must use the same private Meetings folder.")
    root = next(iter(roots))
    state = Path(state).expanduser().absolute()
    with _locked(state / "worker.lock", "An upload is running. Let it finish, then choose the Drive account again."):
        accounts = _transport(config, transport_factory).discover_accounts()
        matches = [account for account in accounts if account.get("link_id") == link_id]
        if len(matches) != 1:
            raise NeedsConfiguration("That Google Drive account is no longer connected. Refresh the list and try again.")
        selected = matches[0]
        if (selected.get("profile_id") != expected_profile_id or not _email(selected.get("email"))
                or selected["email"].casefold() != expected_email.casefold()):
            raise NeedsConfiguration("The selected Google account details changed. Refresh the list and choose the account again.")
        transport = _transport(config, transport_factory, link_id=link_id)
        profile = transport.invoke("get_profile", {})
        if (not isinstance(profile, dict) or not _email(profile.get("email"))
                or not isinstance(profile.get("id"), str) or not _PROFILE_ID.fullmatch(profile["id"])
                or profile["id"] != selected.get("profile_id") or not _email(selected.get("email"))
                or profile["email"].casefold() != selected["email"].casefold()):
            raise NeedsConfiguration("The connected Google account changed. Refresh the list and try again.")
        from .drive_upload import DriveUploader, FOLDER_MIME, DOCUMENT_MIME, UploadNeedsReview
        from .codex_drive_transport import DriveOperationError
        verifier = DriveUploader(state, transport.invoke, root, account_link_id=link_id)
        try:
            root_item = verifier._metadata(root)
            owner = verifier._private(root_item, owner=profile["email"], mime=FOLDER_MIME)
            if root not in verifier._find("root", root_item["title"], True):
                raise UploadNeedsReview("The configured Meetings root is unavailable")
        except (DriveOperationError, UploadNeedsReview):
            raise NeedsConfiguration("Could not verify this account as the owner of the configured private Meetings folder. Choose the folder's owner or review the folder configuration.") from None
        route = config.get("route", {})
        if not isinstance(route, dict):
            raise NeedsConfiguration("The company folder configuration needs review.")
        if route.get("companies") is not None:
            from .routing import validate_companies
            for company in validate_companies(route["companies"]):
                verifier._private(verifier._metadata(company["drive_folder_id"]), owner=owner,
                                  parent=root, name=company["name"], mime=FOLDER_MIME)
                if company["drive_folder_id"] not in verifier._find(root, company["name"], True):
                    raise NeedsConfiguration("A configured company folder is unavailable. Review the folder configuration.")
        publish = stages.get("publish", {})
        template_id = publish.get("template_document_id")
        if template_id is not None:
            verifier._private(verifier._metadata(template_id), owner=owner, mime=DOCUMENT_MIME)
        archive = stages.get("archive", {})
        if archive.get("large_files") is not None:
            if not large_files_configured(archive, "archive"):
                raise NeedsConfiguration("Review the separate recording-upload connection before choosing a Drive account.")
            from .google_drive_auth import GoogleDriveAuth
            auth = (auth_factory or GoogleDriveAuth)(archive["large_files"]["credentials_file"])
            direct = auth.preflight()
            if (not isinstance(direct, dict) or not _email(direct.get("email"))
                    or direct["email"].casefold() != owner.casefold()):
                raise NeedsConfiguration("The separate recording-upload connection uses a different Google account. Reconnect it to the Meetings folder owner first.")
        verified_at = clock()
        if type(verified_at) not in (int, float) or not math.isfinite(verified_at) or verified_at <= 0:
            raise NeedsConfiguration("The verification time is unavailable. Try again.")
        account = {"link_id": link_id, "email": profile["email"], "profile_id": profile["id"],
                   "verified_at": verified_at, "meetings_root_id": root}
        # Network verification does not lock the settings UI. Require its exact
        # revision again while holding the lock shared with preference writes.
        with _locked(path.with_name(path.name + ".lock"), "Settings are being saved. Try again in a moment."):
            _path, fresh, fresh_revision = _snapshot(path)
            if fresh_revision != revision:
                raise NeedsConfiguration("The workflow settings changed. Refresh the account list and try again.")
            fresh["drive_account"] = account
            for stage in _configured_stages(fresh).values():
                stage["account_link_id"] = link_id
            require_verified_account(fresh)
            if len(json.dumps(fresh, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")) + 1 > MAX_CONFIG_BYTES:
                raise NeedsConfiguration("The workflow settings would exceed 100 KB. Review the configuration before saving the account.")
            atomic_json(path, fresh)
        return account
