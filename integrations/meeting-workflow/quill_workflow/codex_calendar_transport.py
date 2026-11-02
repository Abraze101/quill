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
import time
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


def validate_arguments(tool, arguments, *, account_link_id=None):
    if not isinstance(tool, str) or tool not in TOOLS or not isinstance(arguments, dict):
        raise private.DriveProtocolError("Calendar permits only bounded read operations")
    arguments = private.account_arguments(arguments, account_link_id)
    allowed = {
        "list_calendars": {"max_results", "next_page_token"},
        "search_events": {"calendar_id", "time_min", "time_max", "max_results", "next_page_token", "query", "timezone_str"},
        "read_event": {"calendar_id", "event_id"},
        "get_profile": set(),
    }[tool] | {"link_id"}
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


def _account_catalog(tool):
    if not isinstance(tool, dict) or tool.get("name") != "google_calendar.get_profile":
        raise private.DriveProtocolError("Calendar account discovery returned an unexpected tool")
    metadata = tool.get("_meta")
    if not isinstance(metadata, dict) or metadata.get("connector_id") != CONNECTOR:
        raise private.DriveProtocolError("Calendar account discovery returned a different connector")
    candidates = metadata.get("link_accounts")
    if not isinstance(candidates, list) or len(candidates) > 100:
        raise private.DriveProtocolError("Calendar returned an invalid connected-account list")
    accounts, links, identities, emails = [], set(), set(), set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise private.DriveProtocolError("Calendar returned an invalid connected account")
        link = private.validate_account_link_id(candidate.get("link_id"))
        if link is None:
            raise private.DriveProtocolError("Calendar connected account has no account link")
        profile = private._profile_fields(candidate.get("profile_id"), candidate.get("profile_email"),
                                          candidate.get("profile_name"))
        if link in links or profile["id"] in identities or profile["email"].casefold() in emails:
            raise private.DriveProtocolError("Calendar returned duplicate connected-account identities")
        links.add(link)
        identities.add(profile["id"])
        emails.add(profile["email"].casefold())
        accounts.append({"link_id": link, **profile})
    return accounts


class _CalendarAccountsRPC(_CalendarRPC):
    """Discover Calendar links from bounded, structured connector metadata."""

    def __init__(self, directory):
        self.accounts, self.pages, self.cursors = None, 0, set()
        super().__init__(directory, "get_profile", {})

    def queue(self, message):
        if message.get("method") == "mcpServer/tool/call":
            message = {"id": 3, "method": "mcpServerStatus/list", "params": {
                "threadId": self.thread, "detail": "toolsAndAuthOnly", "limit": 20}}
        super().queue(message)

    def consume(self, line):
        try:
            message = private._load_json(line)
        except (ValueError, UnicodeError) as error:
            raise private.DriveProtocolError("Codex emitted unreadable Calendar account metadata") from error
        if (not isinstance(message, dict) or "method" in message
                or type(message.get("id")) is not int or message["id"] < 3
                or message["id"] != self.expected_id or self.done or "error" in message):
            return super().consume(line)
        result = message.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list) or len(result["data"]) > 100:
            raise private.DriveProtocolError("Codex returned an invalid Calendar account catalog")
        self.pages += 1
        for server in result["data"]:
            if not isinstance(server, dict):
                raise private.DriveProtocolError("Codex returned an invalid connector catalog entry")
            if server.get("name") != "codex_apps":
                continue
            tools = server.get("tools")
            if not isinstance(tools, dict):
                raise private.DriveProtocolError("Codex returned an invalid connector tool catalog")
            if "google_calendar.get_profile" in tools:
                if self.accounts is not None:
                    raise private.DriveProtocolError("Codex returned a duplicate Calendar account catalog")
                self.accounts = _account_catalog(tools["google_calendar.get_profile"])
        cursor = result.get("nextCursor")
        if cursor is None:
            self.result = self.accounts if self.accounts is not None else []
            self.done = True
            return
        if (not isinstance(cursor, str) or not cursor or len(cursor) > 4096
                or cursor in self.cursors or self.pages >= 10):
            raise private.DriveProtocolError("Calendar account discovery exceeded its page limit")
        self.cursors.add(cursor)
        self.expected_id += 1
        self.queue({"id": self.expected_id, "method": "mcpServerStatus/list", "params": {
            "threadId": self.thread, "detail": "toolsAndAuthOnly", "limit": 20, "cursor": cursor}})


class CodexCalendarTransport(private.CodexDriveTransport):
    def __init__(self, executable="codex", model=None, timeout=90, max_output_bytes=2_000_000,
                 *, account_email=None, account_link_id=None):
        super().__init__(executable, model, timeout, max_output_bytes, account_link_id=account_link_id)
        if timeout > 180:
            raise private.DriveProtocolError("Calendar reads must time out within three minutes")
        if account_email is not None and (not _text(account_email, 254)
                or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", account_email)):
            raise private.DriveProtocolError("Choose a verified Google account for Calendar lookup")
        self.account_email = account_email
        self._account_verified = False

    def invoke(self, tool_name, arguments):
        # Validate the requested read before any account discovery. A supplied
        # account link may never override the pinned Calendar connection.
        validate_arguments(tool_name, arguments, account_link_id=self.account_link_id)
        if self.cancelled and self.cancelled():
            raise private.DriveOperationError("Local deletion requested; calendar lookup was stopped")
        executable = shutil.which(self.executable)
        if not executable:
            raise private.DriveTransportError("Codex CLI is not installed; connect Calendar in Codex")
        deadline = time.monotonic() + self.timeout
        if self.account_email is not None and not self._account_verified:
            accounts = self._read_rpc(executable, "get_profile", {}, deadline, discovery=True)
            matches = [account for account in accounts if account["email"].casefold() == self.account_email.casefold()]
            if len(matches) != 1 or (self.account_link_id is not None
                    and matches[0]["link_id"] != self.account_link_id):
                raise private.DriveOperationError("Connect Calendar for the Google account selected in Quill")
            selected = matches[0]
            profile = self._read_rpc(executable, "get_profile", {"link_id": selected["link_id"]}, deadline)
            if (profile.get("id") != selected["id"]
                    or profile.get("email", "").casefold() != self.account_email.casefold()):
                raise private.DriveOperationError("Calendar returned a different account; reconnect the selected Google account")
            self.account_link_id = selected["link_id"]
            self._account_verified = True
        if self.account_link_id is None:
            raise private.DriveOperationError("Choose the Google account for Calendar lookup before retrying")
        arguments = validate_arguments(tool_name, arguments, account_link_id=self.account_link_id)
        return self._read_rpc(executable, tool_name, arguments, deadline)

    def _read_rpc(self, executable, tool_name, arguments, deadline, *, discovery=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise private.DriveTransportError("The Calendar lookup reached its time limit")
        if self.cancelled and self.cancelled():
            raise private.DriveOperationError("Local deletion requested; calendar lookup was stopped")
        with tempfile.TemporaryDirectory(prefix="quill-calendar-read-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            rpc = _CalendarAccountsRPC(directory) if discovery else _CalendarRPC(directory, tool_name, arguments)
            process = None
            with private._interruptions():
                try:
                    process = subprocess.Popen(command(executable, directory, tool_name), cwd=directory,
                        env=child_environment(executable), stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, pass_fds=private.child_lease_fds())
                    output_limit = max(self.max_output_bytes, 8_000_000) if discovery else self.max_output_bytes
                    return private._stream(process, rpc, remaining, output_limit, self.cancelled)
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
