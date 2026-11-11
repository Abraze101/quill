"""One exact Drive operation through Codex's authenticated app-server.

The caller owns authorization, destinations, and retry reconciliation. This
transport sends reads and folder creation directly on an ephemeral thread. File
uploads use a narrow normal Codex tool call because its runtime owns local-file
transfer. Neither path answers approval requests or changes config/auth files.
"""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from quill_workflow.codex_notes import child_environment, stop_process_group
from quill_workflow.processing_lease import child_lease_fds

CONNECTOR = "connector_5f3c8c41a1e54ad7a76272c89e2554fa"
WRITE_TOOLS = frozenset(("create_folder", "upload_file", "import_document",
                         "copy_file", "batch_update_document"))
TOOLS = frozenset(("get_profile", "get_file_metadata", "get_document", "search", "list_folder")) | WRITE_TOOLS
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "shell_snapshot", "hooks", "browser_use",
    "browser_use_external", "in_app_browser", "computer_use", "image_generation",
    "multi_agent", "multi_agent_v2", "goals", "memories", "chronicle",
    "workspace_dependencies", "code_mode", "tool_suggest",
    "skill_mcp_dependency_install", "view_image",
)


class DriveTransportError(RuntimeError):
    """Failure with a conservative signal that a write may have happened."""

    def __init__(self, message, *, outcome_unknown=False):
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


class DriveProtocolError(DriveTransportError):
    """The server deviated from the bounded, single-operation RPC contract."""


class DriveOperationError(DriveTransportError):
    """The CLI or connector reported failure; reconcile writes before retrying."""


class DriveTimeoutError(DriveTransportError):
    """The bounded invocation expired; reconcile writes before retrying."""


def validate_account_link_id(value):
    if value is not None and (not isinstance(value, str)
            or not re.fullmatch(r"link_[A-Za-z0-9_-]{1,200}", value)):
        raise DriveProtocolError("Configure a valid Google Drive account_link_id")
    return value


def account_arguments(arguments, account_link_id=None):
    """Pin the connector account without allowing a call to change it."""
    account_link_id = validate_account_link_id(account_link_id)
    if not isinstance(arguments, dict):
        raise DriveProtocolError("Unsupported Drive arguments")
    if "link_id" in arguments and (account_link_id is None
            or arguments["link_id"] != account_link_id):
        raise DriveProtocolError("Drive arguments conflict with the configured account_link_id")
    result = dict(arguments)
    if account_link_id is not None:
        result["link_id"] = account_link_id
    return result


def operation_error(detail, fallback):
    """Recognize one contract failure without exposing untrusted error text."""
    if isinstance(detail, dict):
        detail = detail.get("message")
    if (isinstance(detail, str)
            and "link_id is required for this multi-link tool" in detail.lower()):
        return DriveOperationError(
            "Google Drive account selection is required; configure account_link_id "
            "for the approved account before retrying")
    return DriveOperationError(fallback)


def _profile_fields(identity, email, name):
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", identity):
        raise DriveProtocolError("Google Drive returned an invalid account profile identity")
    if (not isinstance(email, str) or len(email) > 254 or not email.isprintable()
            or not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", email)):
        raise DriveProtocolError("Google Drive returned an invalid account email")
    if (not isinstance(name, str) or not name or name != name.strip()
            or len(name) > 500 or not name.isprintable()):
        raise DriveProtocolError("Google Drive returned an invalid account name")
    return {"id": identity, "email": email, "name": name}


def _account_catalog(tool):
    """Read account candidates only from the exact connector's structured metadata."""
    if not isinstance(tool, dict) or tool.get("name") != "google_drive.get_profile":
        raise DriveProtocolError("Google Drive account discovery returned an unexpected tool")
    metadata = tool.get("_meta")
    if not isinstance(metadata, dict) or metadata.get("connector_id") != CONNECTOR:
        raise DriveProtocolError("Google Drive account discovery returned a different connector")
    candidates = metadata.get("link_accounts")
    if not isinstance(candidates, list) or len(candidates) > 100:
        raise DriveProtocolError("Google Drive returned an invalid connected-account list")
    accounts, links, profiles, emails = [], set(), set(), set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise DriveProtocolError("Google Drive returned an invalid connected account")
        link = validate_account_link_id(candidate.get("link_id"))
        if link is None:
            raise DriveProtocolError("Google Drive connected account has no account link")
        profile = _profile_fields(candidate.get("profile_id"), candidate.get("profile_email"),
                                  candidate.get("profile_name"))
        if link in links or profile["id"] in profiles or profile["email"].casefold() in emails:
            raise DriveProtocolError("Google Drive returned duplicate connected-account identities")
        links.add(link)
        profiles.add(profile["id"])
        emails.add(profile["email"].casefold())
        accounts.append({"link_id": link, "profile_id": profile["id"],
                         "email": profile["email"], "name": profile["name"]})
    return sorted(accounts, key=lambda account: (account["email"].casefold(), account["link_id"]))


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _load_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    def constant(_value):
        raise ValueError("Non-finite JSON number")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def validate_arguments(tool_name, arguments, *, account_link_id=None):
    """Restrict this private filing adapter to the documented connector surface."""
    if not isinstance(tool_name, str) or tool_name not in TOOLS or not isinstance(arguments, dict):
        raise DriveProtocolError("Unsupported Drive operation or arguments")
    arguments = account_arguments(arguments, account_link_id)
    allowed = {
        "get_profile": set(),
        "get_file_metadata": {"fileId", "fields", "supportsAllDrives"},
        "get_document": {"document_id"},
        "search": {"query", "item_type", "page_token", "special_filter_query_str",
                   "best_effort_fetch", "require_viewed_by_user", "topn"},
        "list_folder": {"url", "top_k"},
        "create_folder": {"name", "parent_folder"},
        "upload_file": {"file_uri", "file_name", "mime_type", "parent_folder_id"},
        "import_document": {"source_file", "title", "parent_folder_id", "upload_mode"},
        "copy_file": {"url", "new_title", "parent_folder"},
        "batch_update_document": {"document_id", "requests", "write_control"},
    }[tool_name] | {"link_id"}
    required = {
        "get_profile": set(),
        "get_file_metadata": {"fileId", "fields"},
        "get_document": {"document_id"},
        "search": {"best_effort_fetch", "special_filter_query_str", "topn"},
        "list_folder": {"url", "top_k"},
        "create_folder": {"name", "parent_folder"},
        "upload_file": {"file_uri", "file_name", "mime_type", "parent_folder_id"},
        "import_document": {"source_file", "title", "parent_folder_id", "upload_mode"},
        "copy_file": {"url", "new_title", "parent_folder"},
        "batch_update_document": {"document_id", "requests", "write_control"},
    }[tool_name]
    if set(arguments) - allowed or not required <= set(arguments):
        raise DriveProtocolError("Drive arguments have unsupported or missing fields")
    for key, value in arguments.items():
        if key == "requests":
            supported = {"replaceAllText", "insertText", "deleteContentRange", "insertDate",
                         "updateTextStyle", "updateParagraphStyle", "createParagraphBullets",
                         "deleteParagraphBullets"}
            if (not isinstance(value, list) or not 1 <= len(value) <= 1000
                    or any(not isinstance(item, dict) or len(item) != 1
                           or next(iter(item)) not in supported
                           or not isinstance(next(iter(item.values())), dict) for item in value)):
                raise DriveProtocolError("Unsupported native template edit requests")
        elif key == "write_control":
            if (not isinstance(value, dict) or set(value) != {"requiredRevisionId"}
                    or not isinstance(value["requiredRevisionId"], str)
                    or not 1 <= len(value["requiredRevisionId"]) <= 1000):
                raise DriveProtocolError("Native template edits require an exact document revision")
        elif key in {"supportsAllDrives", "best_effort_fetch", "require_viewed_by_user"}:
            if type(value) is not bool:
                raise DriveProtocolError("Expected a boolean Drive argument")
        elif key in {"topn", "top_k"}:
            if type(value) is not int or not 1 <= value <= 100:
                raise DriveProtocolError("Drive listing limit must be between 1 and 100")
        elif not isinstance(value, str) or not value or len(value) > 16000 or "\x00" in value:
            raise DriveProtocolError("Expected a bounded nonempty Drive argument")
    if tool_name == "search":
        if arguments["best_effort_fetch"] is not False:
            raise DriveProtocolError("Drive discovery must not fetch file contents")
        if "item_type" in arguments and arguments["item_type"] not in {"image", "document", "folder"}:
            raise DriveProtocolError("Unsupported Drive search item type")
    if tool_name == "list_folder":
        match = re.fullmatch(r"https://drive\.google\.com/drive/folders/([A-Za-z0-9_-]+)", arguments["url"])
        if not match or match.group(1) == "root":
            raise DriveProtocolError("Use an explicit official Google Drive folder URL")
    if tool_name == "copy_file" and not re.fullmatch(
            r"https://docs\.google\.com/document/d/[A-Za-z0-9_-]{10,200}/edit", arguments["url"]):
        raise DriveProtocolError("Copy an explicit native Google Docs template URL")
    for key in ("fileId", "document_id", "parent_folder", "parent_folder_id"):
        if tool_name == "get_file_metadata" and key == "fileId" and arguments.get(key) == "root":
            # Read-only identity lookup for authenticated My Drive ancestry.
            # No write or document operation may use this alias.
            continue
        if key in arguments and (arguments[key] == "root" or not re.fullmatch(r"[A-Za-z0-9_-]+", arguments[key])):
            raise DriveProtocolError("Use an explicit raw Drive file or folder ID")
    if tool_name in {"upload_file", "import_document"}:
        path = Path(arguments["file_uri" if tool_name == "upload_file" else "source_file"])
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise DriveProtocolError("Upload input must be an absolute regular local file")
        if tool_name == "import_document" and (path.suffix.lower() != ".docx"
                or arguments["upload_mode"] != "native_google_docs"):
            raise DriveProtocolError("Meeting document imports must convert a DOCX to native Google Docs")
    # Serialize and copy before dispatch so caller mutation cannot change scope.
    try:
        serialized = _json(arguments)
    except (TypeError, ValueError) as error:
        raise DriveProtocolError("Drive arguments must be finite JSON") from error
    limit = 8_000_000 if tool_name == "batch_update_document" else 65536
    if len(serialized.encode("utf-8")) > limit:
        raise DriveProtocolError("Drive arguments exceed the input limit")
    return _load_json(serialized)


def command(executable, directory, tool_name, model=None):
    """Build only process-scoped settings; model is ignored for compatibility."""
    if not isinstance(tool_name, str) or tool_name not in TOOLS:
        raise DriveProtocolError("Unsupported Drive operation")
    directory = Path(directory)
    argv = [str(executable), "app-server", "--stdio"]
    config = {
        "approval_policy": '"never"', "forced_login_method": '"chatgpt"',
        "web_search": '"disabled"', "project_doc_max_bytes": "0",
        "skills.include_instructions": "false", "mcp_servers": "{}",
        "history.persistence": '"none"', "allow_login_shell": "false",
        "log_dir": json.dumps(str(directory / "logs")),
        "features.apps": "true", "features.plugins": "true",
        "features.remote_plugin": "true",
        # Reset the app table before adding the one permitted connector. This
        # avoids inheriting another explicitly enabled app or tool from config.
        "apps": "{}", "apps._default.enabled": "false",
        "apps._default.destructive_enabled": "false",
        "apps._default.open_world_enabled": "false",
        f"apps.{CONNECTOR}.enabled": "true",
        f"apps.{CONNECTOR}.default_tools_enabled": "false",
        f"apps.{CONNECTOR}.default_tools_approval_mode": '"auto"',
        f"apps.{CONNECTOR}.tools.{tool_name}.enabled": "true",
        # The deterministic uploader calls writes only after a saved user
        # approval. Carry that into this process for this one method only.
        f"apps.{CONNECTOR}.tools.{tool_name}.approval_mode":
            '"approve"' if tool_name in WRITE_TOOLS else '"auto"',
    }
    # Keep code_mode_host at its installed default. This CLI can require the
    # host while initializing tools even when no model turn is started.
    for feature in DISABLED_FEATURES:
        config[f"features.{feature}"] = "false"
    for key, value in config.items():
        argv.extend(["-c", key + "=" + value])
    return argv


def _native_result(result, tool, arguments):
    if not isinstance(result, dict):
        raise DriveOperationError("The Google Drive connector reported failure")
    markers = [result[key] for key in ("isError", "is_error") if key in result]
    if any(type(value) is not bool for value in markers):
        raise DriveProtocolError("The Google Drive connector returned an invalid error marker")
    if any(markers):
        raise DriveOperationError("The Google Drive connector reported failure")
    native = result.get("structuredContent")
    if not isinstance(native, dict) or not native:
        raise DriveProtocolError("Drive response has no structured connector result")
    if set(native) == {"result"} and isinstance(native["result"], dict):
        native = native["result"]
    if tool == "get_profile":
        _profile_fields(native.get("id"), native.get("email"), native.get("name"))
        return native
    if tool == "import_document":
        # The import connector uses Google API camelCase fields, unlike raw
        # upload_file's normalized id/mime_type response. Accept only the
        # observed native conversion contract before adapting its field names.
        identity = native.get("fileId")
        if (native.get("success") is not True or native.get("converted") is not True
                or native.get("mimeType") != "application/vnd.google-apps.document"):
            raise DriveOperationError("Google Drive did not confirm native document conversion")
        if (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", identity)
                or native.get("documentId") != identity
                or native.get("id", identity) != identity
                or native.get("mime_type", native["mimeType"]) != native["mimeType"]):
            raise DriveProtocolError("Google Drive returned conflicting imported document identity")
        if (native.get("parentId") != arguments["parent_folder_id"]
                or native.get("title") != arguments["title"]):
            raise DriveProtocolError("Google Drive imported the document outside its declared destination or title")
        native = dict(native, id=identity, mime_type=native["mimeType"], parent_id=native["parentId"])
    if tool == "batch_update_document":
        if (native.get("documentId") != arguments["document_id"]
                or not isinstance(native.get("replies"), list)):
            raise DriveProtocolError("Google Docs did not confirm the requested document edit")
    elif tool in WRITE_TOOLS:
        if native.get("success") is not True:
            raise DriveOperationError("The Google Drive write did not report success")
        if not isinstance(native.get("id"), str) or not native["id"]:
            raise DriveProtocolError("The Google Drive write did not return its file ID")
    elif tool == "get_file_metadata":
        if arguments["fileId"] == "root":
            identity = native.get("id")
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", identity):
                raise DriveProtocolError("Drive did not resolve its authenticated root to a concrete ID")
        elif native.get("id") != arguments["fileId"]:
            raise DriveProtocolError("Drive returned metadata for a different item")
    elif tool == "get_document":
        # The uploader validates the returned document identity and every
        # source paragraph. Do not accept a model-written textual result here.
        pass
    elif tool == "list_folder":
        if (not isinstance(native.get("files"), list)
                or any(not isinstance(item, dict) for item in native["files"])):
            raise DriveProtocolError("Drive folder listing did not return a files list")
    elif (not isinstance(native.get("results"), list)
          or any(not isinstance(item, dict) for item in native["results"])):
        raise DriveProtocolError("Drive search did not return a results list")
    return native


@contextmanager
def _interruptions():
    """Let normal stack cleanup terminate the separate app-server session."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)
    terminating = False

    def terminate(signum, frame):
        nonlocal terminating
        if terminating:
            return
        terminating = True
        if callable(previous):
            previous(signum, frame)
        # SystemExit bypasses the worker's retryable Exception handler while
        # still unwinding this transport's process-group cleanup.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


class _RPC:
    NOTIFICATIONS = frozenset((
        "remoteControl/status/changed", "thread/started", "thread/status/changed",
        "mcpServer/startupStatus/updated", "warning", "configWarning",
        "deprecationNotice", "app/list/updated", "skills/changed",
        # Newer app-servers emit this auth-status notification during startup.
        # It carries no operation; messages with an id still fail below.
        "account/updated",
    ))

    def __init__(self, directory, tool, arguments):
        self.directory, self.tool, self.arguments = directory, tool, arguments
        self.expected_id = 1
        self.thread = None
        self.result = None
        self.dispatched = False
        self.done = False
        self.outgoing = bytearray()
        self.bytes_before_tool_call = None
        self.queue({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "quill_drive_transport", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True}}})

    def queue(self, message):
        if message.get("method") == "mcpServer/tool/call":
            self.bytes_before_tool_call = len(self.outgoing)
        self.outgoing.extend((_json(message) + "\n").encode("utf-8"))

    def write_pending(self, descriptor):
        # Keep any pending setup bytes in a separate write. A queued tool
        # call is still unsent until its own first pipe write is attempted.
        limit = min(8192, self.bytes_before_tool_call) if self.bytes_before_tool_call else 8192
        data = self.outgoing[:limit]
        if self.bytes_before_tool_call == 0:
            self.dispatched = True  # A partial or interrupted write is uncertain.
        count = os.write(descriptor, data)
        del self.outgoing[:count]
        if self.bytes_before_tool_call:
            self.bytes_before_tool_call -= count

    def consume(self, line):
        try:
            message = _load_json(line)
        except (ValueError, UnicodeError) as error:
            raise DriveProtocolError("Codex emitted invalid JSON-RPC") from error
        if not isinstance(message, dict):
            raise DriveProtocolError("Codex emitted an invalid RPC message")
        if "method" in message:
            if "id" in message:
                # Never approve an elicitation or execute a server-requested
                # operation. The exact intended RPC is already predetermined.
                if "approval" in str(message["method"]).lower() or message["method"] in {
                        "item/tool/requestUserInput", "tool/requestUserInput", "mcpServer/elicitation/request"}:
                    raise DriveOperationError("Codex requires an approval or confirmation; no response was sent")
                raise DriveProtocolError("Codex requested an unexpected operation; no response was sent")
            if message["method"] not in self.NOTIFICATIONS:
                raise DriveProtocolError("Codex emitted an unexpected notification or tool operation")
            return
        identity = message.get("id")
        if type(identity) is not int or identity != self.expected_id or self.done:
            raise DriveProtocolError("Codex returned an unexpected or duplicate RPC response")
        if "error" in message:
            # Keep untrusted remote error content out of actionable instructions.
            error = message["error"]
            detail = error.get("message", "") if isinstance(error, dict) else ""
            if isinstance(detail, str) and "approval" in detail.lower():
                raise DriveOperationError("Codex requires approval for this Drive operation")
            raise operation_error(detail, "Codex rejected the Drive setup or operation request")
        result = message.get("result")
        if not isinstance(result, dict):
            raise DriveProtocolError("Codex returned an unreadable RPC response")
        if identity == 1:
            self.queue({"method": "initialized"})
            self.queue({"id": 2, "method": "thread/start", "params": {
                "ephemeral": True, "cwd": str(self.directory), "runtimeWorkspaceRoots": [],
                "environments": [], "approvalPolicy": "never", "sandbox": "read-only",
                "baseInstructions": "No model turns. Only one explicitly specified Drive operation is authorized."}})
            self.expected_id = 2
        elif identity == 2:
            thread = result.get("thread")
            if (not isinstance(thread, dict) or not isinstance(thread.get("id"), str)
                    or not thread["id"] or len(thread["id"]) > 512
                    or thread.get("ephemeral", True) is not True):
                raise DriveProtocolError("Codex did not return an ephemeral thread identity")
            self.thread = thread["id"]
            self.queue({"id": 3, "method": "mcpServer/tool/call", "params": {
                "threadId": self.thread, "server": "codex_apps",
                "tool": "google_drive." + self.tool, "arguments": self.arguments}})
            self.expected_id = 3
        else:
            self.result = _native_result(result, self.tool, self.arguments)
            self.done = True


class _AccountsRPC(_RPC):
    """Bounded connector catalog reads, without starting a model turn."""

    def __init__(self, directory):
        self.accounts = None
        self.pages = 0
        self.cursors = set()
        super().__init__(directory, "get_profile", {})

    def queue(self, message):
        if message.get("method") == "mcpServer/tool/call":
            message = {"id": 3, "method": "mcpServerStatus/list", "params": {
                "threadId": self.thread, "detail": "toolsAndAuthOnly", "limit": 20}}
        super().queue(message)

    def consume(self, line):
        try:
            message = _load_json(line)
        except (ValueError, UnicodeError) as error:
            raise DriveProtocolError("Codex emitted invalid account-discovery JSON-RPC") from error
        if (not isinstance(message, dict) or "method" in message
                or type(message.get("id")) is not int or message["id"] < 3
                or message["id"] != self.expected_id or self.done or "error" in message):
            return super().consume(line)
        result = message.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list) or len(result["data"]) > 100:
            raise DriveProtocolError("Codex returned an invalid account-discovery catalog")
        self.pages += 1
        for server in result["data"]:
            if not isinstance(server, dict):
                raise DriveProtocolError("Codex returned an invalid connector catalog entry")
            if server.get("name") != "codex_apps":
                continue
            tools = server.get("tools")
            if not isinstance(tools, dict):
                raise DriveProtocolError("Codex returned an invalid connector tool catalog")
            if "google_drive.get_profile" in tools:
                if self.accounts is not None:
                    raise DriveProtocolError("Codex returned a duplicate Google Drive account catalog")
                self.accounts = _account_catalog(tools["google_drive.get_profile"])
        cursor = result.get("nextCursor")
        if cursor is None:
            self.result = self.accounts if self.accounts is not None else []
            self.done = True
            return
        if (not isinstance(cursor, str) or not cursor or len(cursor) > 4096
                or cursor in self.cursors or self.pages >= 10):
            raise DriveProtocolError("Codex account-discovery pagination exceeded its bounds")
        self.cursors.add(cursor)
        self.expected_id += 1
        self.queue({"id": self.expected_id, "method": "mcpServerStatus/list", "params": {
            "threadId": self.thread, "detail": "toolsAndAuthOnly", "limit": 20, "cursor": cursor}})


def _stream(process, rpc, timeout, max_output_bytes, cancelled=None):
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    totals = {"stdout": 0, "stderr": 0}
    with selectors.DefaultSelector() as selector:
        for pipe, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, label)
        os.set_blocking(process.stdin.fileno(), False)
        writing = False
        while not rpc.done:
            if cancelled and cancelled():
                raise DriveOperationError("Local deletion requested; the owned Drive operation was stopped")
            if rpc.outgoing and not writing:
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                writing = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DriveTimeoutError("Codex Drive operation timed out")
            selected = selector.select(min(remaining, 0.25))
            if not selected and process.poll() is not None:
                raise DriveOperationError("Codex app-server exited before completing the Drive operation")
            for key, _mask in selected:
                pipe, label = key.fileobj, key.data
                if label == "stdin":
                    try:
                        rpc.write_pending(pipe.fileno())
                    except BrokenPipeError as error:
                        raise DriveOperationError("Codex app-server closed its request stream") from error
                    if not rpc.outgoing:
                        selector.unregister(pipe)
                        writing = False
                    continue
                data = os.read(pipe.fileno(), 8192)
                if not data:
                    selector.unregister(pipe)
                    if label == "stdout":
                        # Reap a server exiting concurrently with EOF before
                        # signalling its group. macOS can report EPERM for the
                        # transient exited-but-not-reaped process group.
                        try:
                            process.wait(timeout=0.1)
                        except subprocess.TimeoutExpired:
                            pass
                        raise DriveOperationError("Codex app-server closed before completing the Drive operation")
                    continue
                totals[label] += len(data)
                if totals[label] > (max_output_bytes if label == "stdout" else 131072):
                    raise DriveProtocolError("Codex Drive output exceeded its size limit")
                if label == "stdout":
                    buffer.extend(data)
                    while b"\n" in buffer:
                        line, _, rest = buffer.partition(b"\n")
                        buffer[:] = rest
                        if line.strip():
                            rpc.consume(line)
                        if rpc.done:
                            # Validate complete buffered messages too. A second
                            # reply or request cannot hide beside the result.
                            while b"\n" in buffer:
                                extra, _, rest = buffer.partition(b"\n")
                                buffer[:] = rest
                                if extra.strip():
                                    rpc.consume(extra)
                            return rpc.result
    raise DriveProtocolError("Codex did not return a verified Drive result")


class CodexDriveTransport:
    def __init__(self, executable="codex", model=None, timeout=180, max_output_bytes=2_000_000, cancelled=None,
                 *, account_link_id=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise DriveProtocolError("Codex Drive timeout must be positive")
        if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 10_000_000:
            raise DriveProtocolError("Codex Drive output limit must be between 1024 and 10000000")
        self.executable = executable
        self.model = model  # Used only by the runtime-mediated file upload.
        self.timeout, self.max_output_bytes = timeout, max_output_bytes
        self.cancelled = cancelled
        self.account_link_id = validate_account_link_id(account_link_id)

    def discover_accounts(self):
        """List connected Drive account candidates; verify selection with get_profile."""
        if self.cancelled and self.cancelled():
            raise DriveOperationError("Google Drive account discovery was stopped")
        executable = shutil.which(self.executable)
        if not executable:
            raise DriveTransportError("Codex CLI is not installed; connect Google Drive in Codex")
        return self._run_rpc(executable, "get_profile", {}, discovery=True)

    def invoke(self, tool_name, arguments):
        arguments = validate_arguments(tool_name, arguments, account_link_id=self.account_link_id)
        if self.cancelled and self.cancelled():
            raise DriveOperationError("Local deletion requested; no new Drive operation was started")
        executable = shutil.which(self.executable)
        if not executable:
            raise DriveTransportError("Codex CLI is not installed; choose its executable path")
        if tool_name in {"upload_file", "import_document"}:
            # Direct mcpServer/tool/call bypasses Codex's openai/fileParams
            # adapter. Let the normal harness create the genuine file reference.
            from quill_workflow.codex_file_upload import run_upload
            return run_upload(arguments, executable=executable, model=self.model,
                              timeout=self.timeout, max_output_bytes=self.max_output_bytes,
                              tool_name=tool_name, cancelled=self.cancelled,
                              account_link_id=self.account_link_id)
        return self._run_rpc(executable, tool_name, arguments)

    def _run_rpc(self, executable, tool_name, arguments, *, discovery=False):
        with tempfile.TemporaryDirectory(prefix="quill-codex-drive-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            rpc = _AccountsRPC(directory) if discovery else _RPC(directory, tool_name, arguments)
            process = None
            with _interruptions():
                try:
                    process = subprocess.Popen(command(executable, directory, tool_name),
                                               cwd=directory, env=child_environment(executable),
                                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, start_new_session=True, pass_fds=child_lease_fds())
                    # Full documents and connector catalogs can exceed the
                    # normal metadata limit while still remaining bounded.
                    output_limit = max(self.max_output_bytes, 8_000_000) if discovery or tool_name == "get_document" else self.max_output_bytes
                    return _stream(process, rpc, self.timeout, output_limit, self.cancelled)
                except DriveTransportError as error:
                    error.outcome_unknown = tool_name in WRITE_TOOLS and rpc.dispatched
                    raise
                except OSError as error:
                    raise DriveTransportError("Codex app-server could not complete the Drive operation",
                                              outcome_unknown=tool_name in WRITE_TOOLS and rpc.dispatched) from error
                finally:
                    if process is not None:
                        try:
                            stop_process_group(process)
                        finally:
                            for pipe in (process.stdin, process.stdout, process.stderr):
                                if pipe and not pipe.closed:
                                    pipe.close()


def invoke(tool_name, arguments, *, executable="codex", model=None, timeout=180,
           max_output_bytes=2_000_000, account_link_id=None):
    return CodexDriveTransport(executable, model, timeout, max_output_bytes,
                              account_link_id=account_link_id).invoke(tool_name, arguments)
