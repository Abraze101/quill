"""Bounded calendar read contract with a synthetic Codex app-server."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.codex_calendar_transport import (
    CONNECTOR, CodexCalendarTransport, command, validate_arguments, _native_result,
)
from quill_workflow import codex_drive_transport as private
import test_codex_drive_transport as fixtures


class CalendarTransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CodexDriveTransportTests()
        self.fixture.setUp()
        self.bounds = {"calendar_id": "primary", "time_min": "2026-10-09T09:30:37Z",
                       "time_max": "2026-10-09T14:00:37Z", "timezone_str": "UTC", "max_results": 30}
        self.link = "link_calendar_owner"

    def tearDown(self):
        self.fixture.tearDown()

    def test_exact_read_rpc_no_model_turn_or_mutation(self):
        native = {"events": [{"id": "event_20261009", "summary": "Synthetic meeting",
                              "start": "2026-10-09T11:00:00Z", "end": "2026-10-09T12:00:00Z"}],
                  "next_page_token": None}
        result = CodexCalendarTransport(executable=self.fixture.fake(native=native),
                                       account_link_id=self.link).invoke("search_events", self.bounds)
        self.assertEqual(result, native)
        messages = self.fixture.requests()
        self.assertEqual([m["method"] for m in messages], ["initialize", "initialized", "thread/start", "mcpServer/tool/call"])
        self.assertEqual(messages[-1]["params"]["tool"], "google_calendar.search_events")
        self.assertEqual(messages[-1]["params"]["arguments"], dict(self.bounds, link_id=self.link))
        self.assertTrue(messages[2]["params"]["ephemeral"])

    def test_list_calendars_and_exact_event_shapes(self):
        for tool, args, native in (
            ("list_calendars", {"max_results": 50}, {"calendars": [{"id": "owner@example.test", "primary": True}], "next_page_token": None}),
            ("read_event", {"event_id": "event_123", "calendar_id": "primary"},
                {"id": "event_123", "attendees": [{"email": "max@example.test", "response_status": "accepted"}]}),
            ("get_profile", {}, {"email": "owner@example.test"}),
        ):
            with self.subTest(tool=tool):
                response = {"structuredContent": native, "isError": False}
                self.assertEqual(_native_result(response, tool, args), native)

    def test_command_enables_only_selected_calendar_read(self):
        argv = command("codex", self.fixture.root, "read_event")
        self.assertIn(f"apps.{CONNECTOR}.tools.read_event.enabled=true", argv)
        self.assertIn(f'apps.{CONNECTOR}.tools.read_event.approval_mode="auto"', argv)
        self.assertIn(f"apps.{CONNECTOR}.default_tools_enabled=false", argv)
        self.assertIn("apps._default.enabled=false", argv)
        self.assertFalse(any(private.CONNECTOR in value for value in argv))

    def test_all_calendar_writes_and_drive_operations_rejected(self):
        for tool in ("create_event", "update_event", "delete_event", "respond_event", "set_event_label_silently",
                     "share_file", "copy_file", "get_file_metadata", [], None):
            with self.subTest(tool=tool), self.assertRaises(private.DriveProtocolError):
                validate_arguments(tool, {})

    def test_search_requires_fixed_bounded_explicit_window(self):
        invalid = [{}, {"time_min": "now", "time_max": "now+1h"},
            {"time_min": "2026-10-09T11:00:00", "time_max": "2026-10-09T12:00:00"},
            {"time_min": "2026-10-09T12:00:00Z", "time_max": "2026-10-09T11:00:00Z"},
            {"time_min": "2026-10-09T12:00:00Z", "time_max": "2026-10-17T12:00:00Z"},
            {"time_min": "2026-10-99T12:00:00Z", "time_max": "2026-10-17T12:00:00Z"}]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(private.DriveProtocolError):
                validate_arguments("search_events", args)

    def test_limits_tokens_calendar_id_timezone_and_extra_keys_checked(self):
        for fields in ({"max_results": 101}, {"max_results": True}, {"max_results": 0},
                       {"next_page_token": ""}, {"calendar_id": "a\nb"}, {"timezone_str": "+08:00"},
                       {"attendees": ["a@example.test"]}, {"query": "\x00"}):
            with self.subTest(fields=fields), self.assertRaises(private.DriveProtocolError):
                validate_arguments("search_events", dict(self.bounds, **fields))
        for identity in ("https://calendar.google.com/event?id=123", "", "a/b"):
            with self.subTest(identity=identity), self.assertRaises(private.DriveProtocolError):
                validate_arguments("read_event", {"event_id": identity})

    def test_missing_primary_and_page_defaults_are_explicit_in_rpc(self):
        args = validate_arguments("search_events", {k: self.bounds[k] for k in ("time_min", "time_max")})
        self.assertEqual(args["calendar_id"], "primary")
        self.assertEqual(args["max_results"], 50)
        self.assertEqual(validate_arguments("list_calendars", {}), {"max_results": 50})

    def test_unstructured_error_wrong_event_and_incomplete_pages_rejected(self):
        invalid = [({"content": [{"type": "text", "text": "Success"}]}, "read_event", {"event_id": "requested"}),
                   ({"isError": True, "structuredContent": {"events": []}}, "search_events", self.bounds),
                   ({"structuredContent": {"id": "wrong"}}, "read_event", {"event_id": "requested"}),
                   ({"structuredContent": {"events": ["bad"]}}, "search_events", self.bounds),
                   ({"structuredContent": {"events": [{}]}}, "search_events", self.bounds),
                   ({"structuredContent": {"events": [], "next_page_token": 123}}, "search_events", self.bounds),
                   ({"structuredContent": {"id": "requested", "attendees": ["person"]}}, "read_event", {"event_id": "requested"})]
        for response, tool, args in invalid:
            with self.subTest(response=response), self.assertRaises(private.DriveTransportError):
                _native_result(response, tool, args)

    def test_bounded_output_pages_and_timeout(self):
        with self.assertRaises(private.DriveProtocolError):
            _native_result({"structuredContent": {"events": [{"id": str(i)} for i in range(31)]}}, "search_events", self.bounds)
        with self.assertRaises(private.DriveProtocolError):
            CodexCalendarTransport(timeout=181)

    def test_interrupted_read_is_not_an_unknown_write_and_does_not_retry(self):
        executable = self.fixture.fake(call="sys.exit(0)")
        with self.assertRaises(private.DriveTransportError) as caught:
            CodexCalendarTransport(executable=executable, account_link_id=self.link).invoke("search_events", self.bounds)
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(len([m for m in self.fixture.requests() if m["method"] == "mcpServer/tool/call"]), 1)

    def test_approval_or_server_tool_requests_are_never_answered(self):
        executable = self.fixture.fake(call="send({'id':'approval','method':'mcpServer/elicitation/request','params':{}})")
        with self.assertRaises(private.DriveOperationError):
            CodexCalendarTransport(executable=executable, account_link_id=self.link).invoke("search_events", self.bounds)

    def catalog(self, accounts=None):
        if accounts is None:
            accounts = [{"link_id": self.link, "profile_id": "owner123", "profile_email": "owner@example.test",
                         "profile_name": "Owner"},
                        {"link_id": "link_other_account", "profile_id": "other123", "profile_email": "other@example.test",
                         "profile_name": "Other"}]
        return {"data": [{"name": "codex_apps", "tools": {"google_calendar.get_profile": {
            "name": "google_calendar.get_profile", "_meta": {"connector_id": CONNECTOR,
                "link_accounts": accounts}}}}], "nextCursor": None}

    def matching_transport(self, *, catalog=None, profile=None):
        profile = profile or {"id": "owner123", "email": "owner@example.test"}
        call = "value = " + repr(profile) + " if request['params']['tool']=='google_calendar.get_profile' else {'calendars': [], 'next_page_token': None}\n"
        call += "send({'id':3,'result':{'structuredContent':value,'isError':False}})"
        executable = self.fixture.fake(catalog=catalog if catalog is not None else self.catalog(), call=call)
        return CodexCalendarTransport(executable, account_email="Owner@example.test")

    def test_owner_account_is_discovered_verified_and_pinned_on_all_reads(self):
        transport = self.matching_transport()
        self.assertEqual(transport.invoke("list_calendars", {"max_results": 20})["calendars"], [])
        transport.invoke("list_calendars", {"max_results": 10})
        requests = self.fixture.requests()
        calls = [item["params"] for item in requests if item["method"] == "mcpServer/tool/call"]
        self.assertEqual([item["tool"] for item in calls],
                         ["google_calendar.get_profile", "google_calendar.list_calendars", "google_calendar.list_calendars"])
        self.assertTrue(all(item["arguments"]["link_id"] == self.link for item in calls))
        self.assertEqual(sum(item["method"] == "mcpServerStatus/list" for item in requests), 1)
        self.assertNotIn("turn/start", [item["method"] for item in requests])
        self.assertEqual(transport.account_link_id, self.link)

    def test_no_account_does_not_fall_back_to_a_default(self):
        with self.assertRaisesRegex(private.DriveOperationError, "Choose the Google account"):
            CodexCalendarTransport(self.fixture.fake()).invoke("list_calendars", {})
        self.assertFalse(self.fixture.capture.exists())

    def test_unmatched_owner_and_duplicate_matches_never_read_calendar_events(self):
        for accounts in ([], [{"link_id": "link_other", "profile_id": "other123",
                              "profile_email": "other@example.test", "profile_name": "Other"}],
                         self.catalog()["data"][0]["tools"]["google_calendar.get_profile"]["_meta"]["link_accounts"] * 2):
            with self.subTest(accounts=accounts):
                if self.fixture.capture.exists():
                    self.fixture.capture.unlink()
                transport = self.matching_transport(catalog=self.catalog(accounts))
                with self.assertRaises(private.DriveTransportError):
                    transport.invoke("list_calendars", {})
                self.assertFalse(any(item["method"] == "mcpServer/tool/call" for item in self.fixture.requests()))

    def test_mismatched_verified_profile_never_reads_calendar_events(self):
        for profile in ({"id": "wrong", "email": "owner@example.test"},
                        {"id": "owner123", "email": "other@example.test"}):
            with self.subTest(profile=profile):
                if self.fixture.capture.exists():
                    self.fixture.capture.unlink()
                transport = self.matching_transport(profile=profile)
                with self.assertRaisesRegex(private.DriveOperationError, "different account"):
                    transport.invoke("list_calendars", {})
                calls = [item["params"]["tool"] for item in self.fixture.requests() if item["method"] == "mcpServer/tool/call"]
                self.assertEqual(calls, ["google_calendar.get_profile"])

    def test_caller_cannot_override_selected_calendar_account(self):
        transport = CodexCalendarTransport(self.fixture.fake(), account_link_id=self.link)
        with self.assertRaises(private.DriveProtocolError):
            transport.invoke("list_calendars", {"link_id": "link_other"})
        self.assertFalse(self.fixture.capture.exists())

    def test_wrong_connector_metadata_does_not_select_an_account(self):
        catalog = self.catalog()
        catalog["data"][0]["tools"]["google_calendar.get_profile"]["_meta"]["connector_id"] = private.CONNECTOR
        with self.assertRaisesRegex(private.DriveProtocolError, "different connector"):
            self.matching_transport(catalog=catalog).invoke("list_calendars", {})
        self.assertFalse(any(item["method"] == "mcpServer/tool/call" for item in self.fixture.requests()))

    def test_discovery_profile_and_read_share_one_deadline(self):
        transport = self.matching_transport()
        seen = []
        original = transport._read_rpc
        def read(*args, **kwargs):
            seen.append(args[3])
            return original(*args, **kwargs)
        with patch.object(transport, "_read_rpc", side_effect=read):
            transport.invoke("list_calendars", {})
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 1)

    def test_expired_deadline_does_not_launch_a_process(self):
        transport = self.matching_transport()
        with patch("quill_workflow.codex_calendar_transport.time.monotonic", return_value=20):
            with self.assertRaisesRegex(private.DriveTransportError, "time limit"):
                transport._read_rpc(transport.executable, "list_calendars", {}, 10)
        self.assertFalse(self.fixture.capture.exists())

    def test_discovery_pagination_is_bounded(self):
        catalog = {"data": [], "nextCursor": "repeat"}
        with self.assertRaisesRegex(private.DriveProtocolError, "page limit"):
            self.matching_transport(catalog=catalog).invoke("list_calendars", {})
        self.assertEqual(sum(item["method"] == "mcpServerStatus/list" for item in self.fixture.requests()), 2)


if __name__ == "__main__":
    unittest.main()
