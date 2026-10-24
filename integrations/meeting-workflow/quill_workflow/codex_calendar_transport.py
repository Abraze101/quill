"""Bounded Calendar reads through the user's authenticated Codex connection.

Only recipient suggestions consume these results. This module cannot create,
edit, delete, respond to, or label calendar events, and cannot share documents.
"""
from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import codex_drive_transport as private
from .codex_notes import child_environment, stop_process_group

# Observed via the installed app-server's app/list metadata, not a guessed
# alias or credentials/configuration read. Only one read method is enabled.
CONNECTOR = "connector_947e0d954944416db111db556030eea6"
TOOLS = frozenset(("list_calendars", "search_events", "read_event", "get_profile"))
MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60


def _text(value, limit=1024):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(ord(character) < 32 or ord(character) == 127 for character in value))


def validate_arguments(tool, arguments):
    if not isinstance(tool, str) or tool not in TOOLS or not isinstance(arguments, dict):
        raise private.DriveProtocolError("Calendar permits only bounded read operations")
    allowed = {
        "list_calendars": {"max_results", "next_page_token"},
        "search_events": {"calendar_id", "time_min", "time_max", "max_results", "next_page_token", "query", "timezone_str"},
        "read_event": {"calendar_id", "event_id"},
        "get_profile": set(),
    }[tool]
    if set(arguments) - allowed:
        raise private.DriveProtocolError("Unsupported Calendar arguments")
    if tool == "search_events" and not {"time_min", "time_max"} <= set(arguments):
        raise private.DriveProtocolError("Calendar searches need an explicit start and end")
    if tool == "read_event" and "event_id" not in arguments:
        raise private.DriveProtocolError("Choose a specific Calendar event")
    for key, value in arguments.items():
        if key == "max_results":
            if type(value) is not int or not 1 <= value <= 100:
                raise private.DriveProtocolError("Calendar pages must contain between one and one hundred items")
        elif not _text(value, 16000 if key == "next_page_token" else 1024):
            raise private.DriveProtocolError("Calendar arguments must be bounded nonempty text")
    if "event_id" in arguments and not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", arguments["event_id"]):
        raise private.DriveProtocolError("Use the saved Calendar event ID")
    if tool == "search_events":
        instants = []
        for key in ("time_min", "time_max"):
            value = arguments[key]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
                raise private.DriveProtocolError("Calendar bounds must include an explicit date, time, and UTC offset")
            try:
                instants.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError as error:
                raise private.DriveProtocolError("Invalid Calendar time boundary") from error
        duration = (instants[1] - instants[0]).total_seconds()
        if not 0 < duration <= MAX_WINDOW_SECONDS:
            raise private.DriveProtocolError("Calendar searches must use a positive window no longer than seven days")
    if "timezone_str" in arguments:
        try:
            ZoneInfo(arguments["timezone_str"])
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise private.DriveProtocolError("Calendar search timezone must be a recognized IANA name") from error
    arguments = dict(arguments)
    if tool in {"search_events", "read_event"}:
        arguments.setdefault("calendar_id", "primary")
    if tool in {"search_events", "list_calendars"}:
        arguments.setdefault("max_results", 50)
    serialized = private._json(arguments)
    if len(serialized.encode()) > 32768:
        raise private.DriveProtocolError("Calendar arguments exceed their input limit")
    return private._load_json(serialized)


def command(executable, directory, tool):
    if not isinstance(tool, str) or tool not in TOOLS:
        raise private.DriveProtocolError("Calendar writes are unavailable")
    argv = private.command(executable, directory, "get_file_metadata")
    return [value.replace(private.CONNECTOR, CONNECTOR).replace(
        f"apps.{CONNECTOR}.tools.get_file_metadata.", f"apps.{CONNECTOR}.tools.{tool}.") for value in argv]


def _native_result(result, tool, arguments):
    if not isinstance(result, dict):
        raise private.DriveProtocolError("Calendar returned an unreadable tool response")
    markers = [result[key] for key in ("isError", "is_error") if key in result]
    if any(type(value) is not bool or value for value in markers):
        raise private.DriveOperationError("The Calendar connector reported a read failure")
    value = result.get("structuredContent")
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    if not isinstance(value, dict) or not value:
        raise private.DriveProtocolError("Calendar reads require a structured result")
    if tool in {"list_calendars", "search_events"}:
        key = "calendars" if tool == "list_calendars" else "events"
        rows = value.get(key)
        if (not isinstance(rows, list) or len(rows) > arguments["max_results"]
                or any(not isinstance(row, dict) or not _text(row.get("id")) for row in rows)):
            raise private.DriveProtocolError("Calendar returned an unreadable or oversized result page")
        token = value.get("next_page_token")
        if token is not None and not _text(token, 16000):
            raise private.DriveProtocolError("Calendar returned an invalid pagination token")
    elif tool == "read_event":
        if value.get("id") != arguments["event_id"]:
            raise private.DriveProtocolError("Calendar returned a different event")
        attendees = value.get("attendees")
        if attendees is not None and (not isinstance(attendees, list) or len(attendees) > 1000
                                     or any(not isinstance(attendee, dict) for attendee in attendees)):
            raise private.DriveProtocolError("Calendar returned unreadable attendees")
    elif not _text(value.get("email"), 254):
        raise private.DriveProtocolError("Calendar did not return a readable user profile")
    return value


class _CalendarRPC(private._RPC):
    def queue(self, message):
        # Retain the established handshake and unexpected-request rejection,
        # replacing only this isolated session's tool namespace and label.
        if message.get("method") == "initialize":
            message["params"]["clientInfo"]["name"] = "quill_calendar_transport"
        elif message.get("method") == "thread/start":
            message["params"]["baseInstructions"] = "No model turns. Only one explicitly specified Calendar read is authorized."
        elif message.get("method") == "mcpServer/tool/call":
            message["params"]["tool"] = "google_calendar." + self.tool
        super().queue(message)

    def consume(self, line):
        try:
            message = private._load_json(line)
        except (ValueError, UnicodeError) as error:
            raise private.DriveProtocolError("Codex emitted unreadable Calendar RPC data") from error
        if isinstance(message, dict) and message.get("id") == 3 and "method" not in message and "error" not in message:
            if type(message["id"]) is not int or self.expected_id != 3 or self.done:
                raise private.DriveProtocolError("Unexpected or duplicate Calendar response")
            self.result = _native_result(message.get("result"), self.tool, self.arguments)
            self.done = True
            return
        super().consume(line)


class CodexCalendarTransport(private.CodexDriveTransport):
    def __init__(self, executable="codex", model=None, timeout=90, max_output_bytes=2_000_000):
        super().__init__(executable, model, timeout, max_output_bytes)
        if timeout > 180:
            raise private.DriveProtocolError("Calendar reads must time out within three minutes")

    def invoke(self, tool_name, arguments):
        arguments = validate_arguments(tool_name, arguments)
        if self.cancelled and self.cancelled():
            raise private.DriveOperationError("Local deletion requested; calendar lookup was stopped")
        executable = shutil.which(self.executable)
        if not executable:
            raise private.DriveTransportError("Codex CLI is not installed; connect Calendar in Codex")
        with tempfile.TemporaryDirectory(prefix="quill-calendar-read-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            rpc, process = _CalendarRPC(directory, tool_name, arguments), None
            with private._interruptions():
                try:
                    process = subprocess.Popen(command(executable, directory, tool_name), cwd=directory,
                        env=child_environment(executable), stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, pass_fds=private.child_lease_fds())
                    return private._stream(process, rpc, self.timeout, self.max_output_bytes, self.cancelled)
                except private.DriveTransportError as error:
                    error.outcome_unknown = False  # This transport has no write operation.
                    raise
                except OSError as error:
                    raise private.DriveTransportError("Codex could not complete the Calendar read") from error
                finally:
                    if process is not None:
                        try:
                            stop_process_group(process)
                        finally:
                            for pipe in (process.stdin, process.stdout, process.stderr):
                                if pipe and not pipe.closed:
                                    pipe.close()
