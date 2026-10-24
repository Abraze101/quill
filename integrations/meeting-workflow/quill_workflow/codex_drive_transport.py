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
TOOLS = frozenset(("get_file_metadata", "get_document", "search", "list_folder")) | WRITE_TOOLS
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


def validate_arguments(tool_name, arguments):
    """Restrict this private filing adapter to the documented connector surface."""
    if not isinstance(tool_name, str) or tool_name not in TOOLS or not isinstance(arguments, dict):
        raise DriveProtocolError("Unsupported Drive operation or arguments")
    allowed = {
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
    }[tool_name]
    required = {
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
    ))

    def __init__(self, directory, tool, arguments):
        self.directory, self.tool, self.arguments = directory, tool, arguments
        self.expected_id = 1
        self.thread = None
        self.result = None
        self.dispatched = False
        self.done = False
        self.outgoing = bytearray()
        self.queue({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "quill_drive_transport", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True}}})

    def queue(self, message):
        self.outgoing.extend((_json(message) + "\n").encode("utf-8"))

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
            raise DriveOperationError("Codex rejected the Drive setup or operation request")
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
            # Mark before writing the request to the pipe. Interrupted pipe
            # writes can still reach the server and cannot be blindly retried.
            self.dispatched = True
        else:
            self.result = _native_result(result, self.tool, self.arguments)
            self.done = True


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
                        count = os.write(pipe.fileno(), rpc.outgoing[:8192])
                    except BrokenPipeError as error:
                        raise DriveOperationError("Codex app-server closed its request stream") from error
                    del rpc.outgoing[:count]
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
    def __init__(self, executable="codex", model=None, timeout=180, max_output_bytes=2_000_000, cancelled=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise DriveProtocolError("Codex Drive timeout must be positive")
        if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 10_000_000:
            raise DriveProtocolError("Codex Drive output limit must be between 1024 and 10000000")
        self.executable = executable
        self.model = model  # Used only by the runtime-mediated file upload.
        self.timeout, self.max_output_bytes = timeout, max_output_bytes
        self.cancelled = cancelled

    def invoke(self, tool_name, arguments):
        arguments = validate_arguments(tool_name, arguments)
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
                              tool_name=tool_name, cancelled=self.cancelled)
        with tempfile.TemporaryDirectory(prefix="quill-codex-drive-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            rpc = _RPC(directory, tool_name, arguments)
            process = None
            with _interruptions():
                try:
                    process = subprocess.Popen(command(executable, directory, tool_name),
                                               cwd=directory, env=child_environment(executable),
                                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, start_new_session=True, pass_fds=child_lease_fds())
                    # Full native document readback includes per-run styles.
                    # A normal long meeting can exceed the metadata limit.
                    output_limit = max(self.max_output_bytes, 8_000_000) if tool_name == "get_document" else self.max_output_bytes
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
           max_output_bytes=2_000_000):
    return CodexDriveTransport(executable, model, timeout, max_output_bytes).invoke(tool_name, arguments)
