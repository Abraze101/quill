"""A separate, restricted transport for explicitly approved notes sharing.

Private filing retains its original allowlist. This transport adds only one
operation: granting one named user reader access to a native Google document.
The caller must journal intent and verify resulting permissions independently.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from . import codex_drive_transport as private
from .codex_notes import child_environment, stop_process_group

READ_AND_PREPARE = frozenset(("get_file_metadata", "get_document", "copy_file", "batch_update_document"))


def recipient_email(value):
    if (not isinstance(value, str) or len(value) > 254 or value != value.strip()
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value)
            or ".." in value or value.startswith(".") or ".@" in value):
        raise private.DriveProtocolError("Confirm a complete, individual recipient email address")
    return value.casefold()


def validate_arguments(tool_name, arguments):
    if tool_name in READ_AND_PREPARE:
        return private.validate_arguments(tool_name, arguments)
    if (tool_name != "share_file" or not isinstance(arguments, dict)
            or set(arguments) != {"url", "user_email", "permission", "anyone_at_company"}
            or arguments.get("permission") != "reader" or arguments.get("anyone_at_company") is not False
            or not isinstance(arguments.get("url"), str)
            or not re.fullmatch(r"https://docs\.google\.com/document/d/[A-Za-z0-9_-]{10,200}/edit", arguments["url"])):
        raise private.DriveProtocolError("Sharing permits only individual reader access to a native notes document")
    return dict(arguments, user_email=recipient_email(arguments["user_email"]))


def command(executable, directory):
    # Reuse the established process isolation without extending its allowlist.
    argv = private.command(executable, directory, "get_file_metadata")
    old = f"apps.{private.CONNECTOR}.tools.get_file_metadata."
    new = f"apps.{private.CONNECTOR}.tools.share_file."
    return [arg.replace(old, new).replace(new + 'approval_mode="auto"', new + 'approval_mode="approve"')
            for arg in argv]


class _SharingRPC(private._RPC):
    def consume(self, line):
        # Setup and server-request rejection use the same bounded RPC contract.
        message = private._load_json(line)
        if isinstance(message, dict) and message.get("id") == 3 and "method" not in message and "error" not in message:
            if type(message["id"]) is not int or self.expected_id != 3 or self.done:
                raise private.DriveProtocolError("Codex returned a duplicate sharing response")
            result = message.get("result")
            if not isinstance(result, dict):
                raise private.DriveProtocolError("Unreadable sharing response")
            markers = [result[key] for key in ("isError", "is_error") if key in result]
            if any(type(value) is not bool or value for value in markers):
                raise private.DriveOperationError("The Drive connector did not confirm sharing")
            native = result.get("structuredContent")
            if isinstance(native, dict) and set(native) == {"result"}:
                native = native["result"]
            if not isinstance(native, dict) or native.get("success") is not True:
                raise private.DriveOperationError("The Drive connector did not confirm sharing")
            self.result, self.done = native, True
            return
        super().consume(line)


class CodexSharingTransport(private.CodexDriveTransport):
    def invoke(self, tool_name, arguments):
        arguments = validate_arguments(tool_name, arguments)
        if self.cancelled and self.cancelled():
            raise private.DriveOperationError("Local deletion requested; sharing was stopped")
        if tool_name != "share_file":
            return super().invoke(tool_name, arguments)
        executable = shutil.which(self.executable)
        if not executable:
            raise private.DriveTransportError("Codex CLI is not installed")
        with tempfile.TemporaryDirectory(prefix="quill-notes-sharing-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            rpc, process = _SharingRPC(directory, tool_name, arguments), None
            with private._interruptions():
                try:
                    process = subprocess.Popen(command(executable, directory), cwd=directory,
                        env=child_environment(executable), stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, pass_fds=private.child_lease_fds())
                    return private._stream(process, rpc, self.timeout, self.max_output_bytes, self.cancelled)
                except private.DriveTransportError as error:
                    error.outcome_unknown = rpc.dispatched
                    raise
                except (OSError, ValueError, UnicodeError) as error:
                    raise private.DriveTransportError("Codex could not complete the sharing operation",
                                                      outcome_unknown=rpc.dispatched) from error
                finally:
                    if process is not None:
                        try:
                            stop_process_group(process)
                        finally:
                            for pipe in (process.stdin, process.stdout, process.stderr):
                                if pipe and not pipe.closed:
                                    pipe.close()
