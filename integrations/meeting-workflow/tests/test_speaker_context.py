"""Synthetic invitation and learned-name evidence; no account or model access."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.speaker_context import build_context


class CalendarFixture:
    def __init__(self):
        self.calls = []
        self.event = {"id": "invite", "summary": "Example meeting", "status": "confirmed",
                      "start": "2026-10-09T11:00:00Z", "end": "2026-10-09T11:30:00Z",
                      "hangout_link": "https://meet.google.com/abc-defg-hij",
                      "attendees": [
                          {"email": "owner@example.test", "display_name": "Alex Owner", "is_self": True},
                          {"email": "guest@example.test", "display_name": "Casey Rivera"}]}
        self.events = [self.event]
        self.reads = {}
        self.fail = set()

    def __call__(self, operation, arguments):
        self.calls.append((operation, copy.deepcopy(arguments)))
        if operation in self.fail:
            raise RuntimeError("Synthetic calendar unavailable")
        if operation == "list_calendars":
            return {"calendars": [{"id": "owner@example.test", "summary": "Main",
                                    "access_role": "owner", "primary": True}]}
        if operation == "search_events":
            return {"events": copy.deepcopy(self.events)}
        if operation == "read_event":
            event = self.reads.get(arguments["event_id"])
            if event is None:
                event = next(item for item in self.events if item["id"] == arguments["event_id"])
            return copy.deepcopy(event)
        raise AssertionError("Unexpected calendar operation: " + operation)


class SpeakerContextTests(unittest.TestCase):
    def setUp(self):
        self.calendar = CalendarFixture()
        self.metadata = {"started": "2026-10-09T11:00:37Z", "ended": "2026-10-09T11:35:00Z",
                         "meeting_context": {"provider": "google-meet", "key": "abc-defg-hij",
                             "coverage": "visible_only", "participants": [
                                 {"name": "Casey", "self": False}, {"name": "Alex Owner", "self": True}]}}
        self.transcript = {"segments": [{"speaker": "mic_speaker_1", "source": "mic",
                                         "start_ms": 0, "end_ms": 1000, "text": "Synthetic meeting."}]}
        self.config = {"drive_account": {"email": "owner@example.test"}}

    def context(self, **kwargs):
        return build_context("synthetic-meeting", self.metadata, self.transcript,
                             config=self.config, invoke=kwargs.pop("invoke", self.calendar), **kwargs)

    def test_matching_invitation_retains_exact_names_addresses_and_sources(self):
        original = copy.deepcopy((self.metadata, self.transcript, self.calendar.event))
        context, status = self.context()
        self.assertEqual(status, "matched_invitation")
        self.assertEqual(context["candidates"], [
            {"name": "Casey", "email": None, "source": "visible", "aliases": []},
            {"name": "Alex Owner", "email": None, "source": "visible", "aliases": []},
            {"name": "Alex Owner", "email": "owner@example.test", "source": "calendar", "aliases": []},
            {"name": "Casey Rivera", "email": "guest@example.test", "source": "calendar", "aliases": []}])
        self.assertEqual((self.metadata, self.transcript, self.calendar.event), original)
        self.assertEqual([name for name, _ in self.calendar.calls],
                         ["list_calendars", "search_events", "read_event"])

    def test_host_requires_explicit_organizer_on_strong_invitation(self):
        context, _ = self.context()
        self.assertNotIn("organizer", context)
        self.calendar.event["organizer_email"] = "GUEST@example.test"
        context, _ = self.context()
        self.assertEqual(context["organizer"], {"name": "Casey Rivera", "email": "guest@example.test"})
        self.calendar.event.pop("organizer_email")
        self.calendar.event["attendees"][0]["organizer"] = True
        context, _ = self.context()
        self.assertEqual(context["organizer"], {"name": "Alex Owner", "email": "owner@example.test"})
        self.calendar.event["organizer"] = {"email": "unknown@example.test"}
        self.assertNotIn("organizer", self.context()[0])
        self.calendar.event["hangout_link"] = "https://meet.google.com/zzz-yyyy-xxx"
        self.assertNotIn("organizer", self.context()[0])

    def test_matching_invites_with_different_organizers_do_not_establish_host(self):
        self.calendar.event["organizer"] = {"email": "owner@example.test"}
        self.calendar.events.append(dict(self.calendar.event, id="copy", organizer={"email": "guest@example.test"}))
        context, _ = self.context()
        self.assertNotIn("organizer", context)

    def test_connector_is_bound_to_verified_selected_account(self):
        with patch("quill_workflow.drive_accounts.require_verified_account", return_value={"email": "selected@example.test"}) as verified, \
                patch("quill_workflow.codex_calendar_transport.CodexCalendarTransport") as factory:
            factory.return_value.invoke.side_effect = self.calendar
            context, status = build_context("synthetic-meeting", self.metadata, self.transcript,
                                            config={"publish": {"codex": "/synthetic/codex"}})
        verified.assert_called_once_with({"publish": {"codex": "/synthetic/codex"}})
        factory.assert_called_once_with("/synthetic/codex", account_email="selected@example.test", timeout=45)
        self.assertEqual(status, "matched_invitation")
        self.assertTrue(any(item["source"] == "calendar" for item in context["candidates"]))

    def test_no_selected_account_keeps_visible_evidence_without_opening_connector(self):
        with patch("quill_workflow.drive_accounts.require_verified_account", side_effect=ValueError("No selected account")), \
                patch("quill_workflow.codex_calendar_transport.CodexCalendarTransport") as factory:
            context, status = build_context("synthetic-meeting", self.metadata, self.transcript)
        factory.assert_not_called()
        self.assertEqual(status, "not_configured")
        self.assertTrue(all(item["source"] == "visible" and item["email"] is None for item in context["candidates"]))

    def test_calendar_email_handles_never_supply_a_name(self):
        self.calendar.event["attendees"] = [
            {"email": "caseyrivera@example.test"},
            {"email": "other@example.test", "display_name": "other@example.test"}]
        context, _ = self.context()
        self.assertEqual([item["name"] for item in context["candidates"]], ["Casey", "Alex Owner", None, None])
        unnamed = [item for item in context["candidates"] if item["source"] == "calendar"]
        self.assertEqual({item["email"] for item in unnamed}, {"caseyrivera@example.test", "other@example.test"})
        self.assertTrue(all(item["name"] is None and item["aliases"] == [] for item in unnamed))

    def test_unnamed_invitation_matches_saved_identity_only_by_exact_email(self):
        self.metadata["meeting_context"]["participants"] = []
        self.calendar.event["attendees"] = [{"email": "guest@example.test"}]
        learned = [{"name": "Casey Rivera", "email": "GUEST@example.test", "aliases": ["Casey"]},
                   {"name": "Guest", "email": "other@example.test", "aliases": []}]
        context, status = self.context(learned=learned)
        self.assertEqual(status, "matched_invitation")
        self.assertEqual(context["candidates"], [
            {"name": None, "email": "guest@example.test", "source": "calendar", "aliases": []},
            {"name": "Casey Rivera", "email": "guest@example.test", "source": "confirmed_correction", "aliases": ["Casey"]}])

    def test_conflicting_unnamed_invitation_addresses_remain_ambiguous(self):
        self.calendar.event["attendees"] = [{"email": "guest@example.test"}]
        self.calendar.events.append(dict(self.calendar.event, id="other-invite",
                                         attendees=[{"email": "other@example.test"}]))
        context, status = self.context()
        self.assertEqual(status, "ambiguous_invitation")
        self.assertTrue(all(item["source"] == "visible" for item in context["candidates"]))

    def test_names_or_time_without_matching_link_do_not_supply_calendar_identities(self):
        for change in ({"hangout_link": "https://meet.google.com/zzz-yyyy-xxx"}, {"hangout_link": None}):
            with self.subTest(change=change):
                self.calendar.event.update(change)
                self.calendar.event["attendees"][1]["display_name"] = "Casey"
                context, status = self.context()
                self.assertNotEqual(status, "matched_invitation")
                self.assertTrue(all(item["source"] == "visible" for item in context["candidates"]))

    def test_readback_rechecks_invitation_identity_time_and_cancellation(self):
        for change in ({"id": "replaced"}, {"status": "cancelled"},
                       {"hangout_link": "https://meet.google.com/zzz-yyyy-xxx"},
                       {"start": "2026-10-10T11:00:00Z", "end": "2026-10-10T11:30:00Z"}):
            with self.subTest(change=change):
                self.calendar.reads["invite"] = dict(self.calendar.event, **change)
                context, status = self.context()
                self.assertNotEqual(status, "matched_invitation")
                self.assertTrue(all(item["source"] == "visible" for item in context["candidates"]))

    def test_conflicting_matching_invitations_are_ambiguous(self):
        other = copy.deepcopy(self.calendar.event)
        other.update(id="another-invite", attendees=[{"email": "different@example.test", "display_name": "Different Person"}])
        self.calendar.events.append(other)
        context, status = self.context()
        self.assertEqual(status, "ambiguous_invitation")
        self.assertTrue(all(item["source"] == "visible" for item in context["candidates"]))

    def test_identical_invitation_copies_do_not_duplicate_candidates(self):
        self.calendar.events.append(dict(self.calendar.event, id="copy"))
        context, status = self.context()
        self.assertEqual(status, "matched_invitation")
        self.assertEqual(len([item for item in context["candidates"] if item["source"] == "calendar"]), 2)

    def test_no_invitation_or_disconnected_calendar_keeps_visible_evidence(self):
        self.calendar.events = []
        context, status = self.context()
        self.assertEqual(status, "no_match")
        self.assertEqual(len(context["candidates"]), 2)
        self.calendar.fail.add("list_calendars")
        context, status = self.context()
        self.assertEqual(status, "unavailable")
        self.assertEqual(len(context["candidates"]), 2)

    def test_unusable_display_label_does_not_discard_other_visible_evidence(self):
        self.calendar.events = []
        for name in ("A" * 61, "Microphone speaker 2"):
            with self.subTest(name=name):
                self.metadata["meeting_context"]["participants"] = [
                    {"name": name, "self": False}, {"name": "Casey", "self": False}]
                context, status = self.context()
                self.assertEqual(status, "no_match")
                self.assertEqual(context["candidates"], [
                    {"name": "Casey", "email": None, "source": "visible", "aliases": []}])

    def test_learned_people_require_exact_relevant_name_alias_or_email(self):
        learned = [
            {"name": "Casey Rivera", "email": "confirmed@example.test", "aliases": ["Casey"]},
            {"name": "Another Spelling", "email": "GUEST@example.test", "aliases": []},
            {"name": "Unrelated Person", "email": "unrelated@example.test", "aliases": ["Unrelated"]},
            {"name": "Casey Junior", "email": "junior@example.test", "aliases": []}]
        context, _ = self.context(learned=learned)
        relevant = [item for item in context["candidates"] if item["source"] == "confirmed_correction"]
        self.assertEqual({item["name"] for item in relevant}, {"Casey Rivera", "Another Spelling"})
        self.assertEqual(relevant[0]["aliases"], ["Casey"])

    def test_no_visible_or_calendar_evidence_does_not_load_unrelated_memory(self):
        self.metadata.pop("meeting_context")
        self.calendar.events = []
        context, status = self.context(learned=[{"name": "Casey", "email": "casey@example.test", "aliases": []}])
        self.assertEqual(status, "no_match")
        self.assertEqual(context["candidates"], [])

    def test_cancellation_before_or_during_calendar_reads_wins(self):
        for cancel_at in (None, "list_calendars", "read_event"):
            with self.subTest(cancel_at=cancel_at):
                calendar = CalendarFixture()
                cancelled = cancel_at is None
                def guard():
                    if cancelled:
                        raise RuntimeError("Synthetic cancellation")
                def invoke(operation, arguments):
                    nonlocal cancelled
                    value = calendar(operation, arguments)
                    if operation == cancel_at:
                        cancelled = True
                    return value
                with self.assertRaisesRegex(RuntimeError, "Synthetic cancellation"):
                    self.context(invoke=invoke, guard=guard)
                if cancel_at is None:
                    self.assertEqual(calendar.calls, [])
                elif cancel_at == "list_calendars":
                    self.assertEqual(len(calendar.calls), 1)

    def test_overall_deadline_prevents_another_calendar_read(self):
        clock = [0]
        def invoke(operation, arguments):
            value = self.calendar(operation, arguments)
            clock[0] = 61
            return value
        with patch("quill_workflow.speaker_context.time.monotonic", side_effect=lambda: clock[0]):
            context, status = self.context(invoke=invoke)
        self.assertEqual(status, "unavailable")
        self.assertEqual(len(self.calendar.calls), 1)
        self.assertTrue(all(item["source"] == "visible" for item in context["candidates"]))

    def test_final_response_after_overall_deadline_is_not_accepted(self):
        clock = [0]
        def invoke(operation, arguments):
            value = self.calendar(operation, arguments)
            if operation == "read_event":
                clock[0] = 61
            return value
        with patch("quill_workflow.speaker_context.time.monotonic", side_effect=lambda: clock[0]):
            context, status = self.context(invoke=invoke)
        self.assertEqual(status, "unavailable")
        self.assertTrue(all(item["source"] == "visible" for item in context["candidates"]))


if __name__ == "__main__":
    unittest.main()
