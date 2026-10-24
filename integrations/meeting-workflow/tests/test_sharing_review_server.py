"""The notes-sharing UI never treats preparing a copy as permission to share."""
import http.client
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.review_server import MAX_FORM_BYTES, create_server, serve_review, _selection


class SharingReviewServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.items = [{"status": "available", "title": "Meeting <title>", "job": "exact-job",
                       "revision": "exact-revision", "preview": "# Notes\nOwner & action <script>alert(1)</script>"}]
        self.calls = []
        self.server = create_server(self.state, sharing_options=lambda: self.items,
                                    on_sharing=self.callback)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary.cleanup()

    def callback(self, action, job, revision, emails=None):
        self.calls.append((action, job, revision, emails))

    def request(self, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        base = {"Host": self.server.review_host}
        if method == "POST":
            base.update({"Origin": self.server.review_origin,
                         "Content-Type": "application/x-www-form-urlencoded"})
        base.update(headers or {})
        base = {key: value for key, value in base.items() if value is not None}
        path = self.server.review_path + ("decision" if method == "POST" else
            "?" + urlencode({"meeting": _selection("exact-job"), "tab": "sharing"}))
        connection.request(method, path, body=body, headers=base)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        connection.close()
        return result

    def decide(self, action, **changes):
        fields = {"action": action, "job": "exact-job", "revision": "exact-revision"}
        fields.update(changes)
        return self.request("POST", urlencode(fields))

    def ready(self):
        self.items[0].update(status="ready", recipients=["alice@example.com", "bob@example.com"],
                             draft_url="https://docs.google.com/document/d/exact-document-123/edit", cancellable=True)

    def test_available_page_has_blank_recipients_and_distinct_private_action(self):
        self.items[0]["preview"] = "# Meeting <title>\nOwner & action <script>alert(1)</script>"
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn("<h3>Share meeting notes</h3>", body)
        self.assertIn("Preparing a copy does not share it", body)
        self.assertIn("recording and full transcript stay private", body)
        self.assertIn("Prepare private sharing copy", body)
        self.assertIn("Keep private", body)
        self.assertNotIn("Nothing is shared with participants here", body)
        self.assertNotIn('value="sharing_confirm"', body)
        self.assertIn('spellcheck="false"></textarea>', body)
        self.assertIn("Meeting &lt;title&gt;", body)
        self.assertIn("Owner &amp; action &lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("<script>", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "strict-origin")
        self.assertIn("form-action 'self'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        forms = [part.split("</form>")[0] for part in body.split('<form method="post"')[1:]]
        self.assertEqual(len(forms), 2)
        for form in forms:
            self.assertIn('name="job" value="exact-job"', form)
            self.assertIn('name="revision" value="exact-revision"', form)
        private = next(form for form in forms if 'value="sharing_private"' in form)
        self.assertNotIn('name="emails"', private)
        self.assertEqual(self.calls, [])

    def test_prepare_passes_only_exact_revision_and_supplied_emails(self):
        emails = "alice@example.com, bob@example.com\ncarol@example.com"
        status, headers, _ = self.decide("sharing_prepare", emails=emails)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"].split("?", 1)[0], self.server.review_path)
        self.assertEqual(self.calls, [("sharing_prepare", "exact-job", "exact-revision", emails)])

    def test_ready_preview_requires_explicit_confirmation_without_recipient_changes(self):
        self.ready()
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn("<details open>", body)
        self.assertIn("alice@example.com", body)
        self.assertIn("bob@example.com", body)
        self.assertIn("Google may email these recipients an invitation", body)
        self.assertIn('rel="noopener noreferrer"', body)
        self.assertIn('value="sharing_confirm">Share view-only', body)
        confirm_form = next(part.split("</form>")[0] for part in body.split('<form method="post"')[1:]
                            if 'value="sharing_confirm"' in part.split("</form>")[0])
        self.assertNotIn('name="emails"', confirm_form)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.decide("sharing_confirm")[0], 303)
        self.assertEqual(self.calls, [("sharing_confirm", "exact-job", "exact-revision", None)])
        self.assertEqual(self.decide("sharing_confirm", emails="mallory@example.com")[0], 400)
        self.assertEqual(len(self.calls), 1)

    def test_busy_and_terminal_states_never_expose_a_resend_action(self):
        self.ready()
        for state, expected in (("preparing", "Preparing your private"),
                                ("sharing", "Sharing is in progress"),
                                ("shared", "Shared view-only with"),
                                ("private", "Kept private")):
            self.items[0]["status"] = state
            status, _, body = self.request()
            self.assertEqual(status, 200)
            self.assertIn(expected, body)
            self.assertNotIn('<form method="post"', body)
            if state in ("preparing", "sharing"):
                self.assertIn("Refresh status", body)
        self.assertEqual(self.calls, [])

    def test_failed_state_exposes_only_safe_retry_and_escapes_error(self):
        self.items[0].update(status="needs_attention", error="Check <details>", retryable=False)
        body = self.request()[2]
        self.assertIn("Check &lt;details&gt;", body)
        self.assertNotIn('<form method="post"', body)
        self.items[0]["retryable"] = True
        body = self.request()[2]
        self.assertIn('value="sharing_retry"', body)
        self.assertNotIn('value="sharing_confirm"', body)
        self.assertNotIn('value="sharing_private"', body)
        self.assertEqual(self.decide("sharing_retry")[0], 303)
        self.assertEqual(self.calls, [("sharing_retry", "exact-job", "exact-revision", None)])

    def test_changed_source_can_be_kept_private_only_when_explicitly_cancellable(self):
        self.items[0].update(status="needs_attention", error="The saved meeting notes changed.",
                             retryable=False, cancellable=True)
        body = self.request()[2]
        self.assertIn('value="sharing_private"', body)
        self.assertNotIn('value="sharing_retry"', body)
        self.items[0]["cancellable"] = False
        self.assertNotIn('<form method="post"', self.request()[2])

    def test_partial_sharing_explains_verified_access_and_uncertain_invitations(self):
        self.items[0].update(status="needs_attention", error="The second invitation needs review.",
                             retryable=True, cancellable=False, sharing_started=True,
                             verified_recipients=["alice@example.com", "<test>@example.com"])
        body = self.request()[2]
        self.assertIn("Access already verified for:", body)
        self.assertIn("alice@example.com", body)
        self.assertIn("&lt;test&gt;@example.com", body)
        self.assertIn("Some invitations may already have been sent", body)
        self.assertIn("Retry checks existing access", body)
        self.assertIn('value="sharing_retry"', body)
        self.assertNotIn('value="sharing_private"', body)

    def test_ready_recipient_changes_prepare_a_new_review_without_confirming(self):
        self.ready()
        body = self.request()[2]
        self.assertIn("Change recipients", body)
        self.assertIn("Save recipients for review", body)
        self.assertIn("alice@example.com\nbob@example.com</textarea>", body)
        change_form = next(part.split("</form>")[0] for part in body.split('<form method="post"')[1:]
                           if 'value="sharing_prepare"' in part.split("</form>")[0])
        self.assertIn('name="job" value="exact-job"', change_form)
        self.assertIn('name="revision" value="exact-revision"', change_form)
        self.assertNotIn('value="sharing_confirm"', change_form)
        self.assertEqual(self.decide("sharing_prepare", emails="carol@example.com")[0], 303)
        self.assertEqual(self.calls, [("sharing_prepare", "exact-job", "exact-revision", "carol@example.com")])
        self.items[0]["cancellable"] = False
        body = self.request()[2]
        self.assertNotIn("Change recipients", body)
        self.assertNotIn('value="sharing_private"', body)

    def test_private_choice_does_not_receive_emails_or_touch_folder_decisions(self):
        folder_calls = []
        self.server.on_decision = folder_calls.append
        self.assertEqual(self.decide("sharing_private")[0], 303)
        self.assertEqual(self.calls, [("sharing_private", "exact-job", "exact-revision", None)])
        self.assertEqual(folder_calls, [])
        self.assertEqual(self.decide("sharing_private", emails="")[0], 400)
        self.assertEqual(len(self.calls), 1)

    def test_untrusted_or_missing_draft_links_never_allow_confirmation(self):
        self.ready()
        for link in (None, "javascript:alert(1)", "https://evil.example/document/d/exact-document-123/edit",
                     "https://docs.google.com.evil.example/document/d/exact-document-123/edit",
                     "https://docs.google.com@evil.example/document/d/exact-document-123/edit",
                     "http://docs.google.com/document/d/exact-document-123/edit",
                     "https://docs.google.com/document/d/../edit",
                     "https://docs.google.com/document/d/exact-document-123/edit\n"):
            self.items[0]["draft_url"] = link
            status, _, body = self.request()
            self.assertEqual(status, 200)
            self.assertNotIn('value="sharing_confirm"', body)
            self.assertNotIn("Open notes-only copy</a>", body)
            self.assertIn("notes copy link is unavailable", body)
        self.assertEqual(self.calls, [])

    def test_sharing_post_requires_same_origin_host_and_bounded_exact_fields(self):
        body = urlencode({"action": "sharing_prepare", "job": "exact-job", "revision": "exact-revision",
                          "emails": "alice@example.com"})
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"},
                        {"Origin": None}, {"Origin": "null"}):
            self.assertEqual(self.request("POST", body, headers)[0], 403)
        for invalid in (body + "&emails=bob%40example.com", body + "&company_id=company",
                        body + "&extra=a&other=b", body.replace("sharing_prepare", "sharing_all"),
                        "action=sharing_confirm&job=&revision=exact-revision",
                        "action=sharing_confirm&job=exact-job&revision="):
            self.assertEqual(self.request("POST", invalid)[0], 400)
        self.assertEqual(self.request("POST", body + "x" * MAX_FORM_BYTES)[0], 413)
        self.assertEqual(self.request("POST", "{}", {"Content-Type": "application/json"})[0], 415)
        self.assertEqual(self.calls, [])

    def test_duplicate_security_headers_cannot_share(self):
        body = urlencode({"action": "sharing_confirm", "job": "exact-job", "revision": "exact-revision"}).encode()
        for duplicate, value in (("Host", "evil.example"), ("Origin", "https://evil.example")):
            connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
            try:
                connection.putrequest("POST", self.server.review_path + "decision", skip_host=True)
                connection.putheader("Host", self.server.review_host)
                connection.putheader("Origin", self.server.review_origin)
                connection.putheader(duplicate, value)
                connection.putheader("Content-Type", "application/x-www-form-urlencoded")
                connection.putheader("Content-Length", str(len(body)))
                connection.endheaders(body)
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
            finally:
                connection.close()
        self.assertEqual(self.calls, [])

    def test_stale_state_and_unavailable_callbacks_show_conflict_without_retry(self):
        def stale(*args, **kwargs):
            raise ValueError("Notes changed <reload>")
        self.server.on_sharing = stale
        status, _, body = self.decide("sharing_confirm")
        self.assertEqual(status, 409)
        self.assertIn("Notes changed &lt;reload&gt;", body)
        self.server.on_sharing = None
        self.assertEqual(self.decide("sharing_prepare", emails="alice@example.com")[0], 409)
        self.assertNotIn('<form method="post"', self.request()[2])
        self.assertEqual(self.calls, [])

    def test_empty_sharing_section_and_callback_validation(self):
        self.items.clear()
        self.assertIn("No meetings", self.request()[2])
        for kwargs in ({"sharing_options": []}, {"on_sharing": True}):
            with self.assertRaises(TypeError):
                create_server(self.state, **kwargs)

    def test_serve_review_forwards_appended_callbacks_without_opening_browser(self):
        ready = []
        options = lambda: self.items
        with patch("quill_workflow.review_server.create_server", wraps=create_server) as create:
            with patch("quill_workflow.review_server.webbrowser.open") as browser:
                serve_review(self.state, open_browser=False, timeout_seconds=0.03,
                             on_ready=ready.append, sharing_options=options, on_sharing=self.callback)
                browser.assert_not_called()
        self.assertEqual(create.call_args.args[-2:], (options, self.callback))
        self.assertEqual(len(ready), 1)
        self.assertEqual(self.calls, [])

    def calendar(self, status="ready"):
        self.items[0].update(suggestion_status=status, suggestion_revision="lookup-revision", suggested_events=[{
            "selection_token": "opaque-validated-event-token", "title": "Portal planning <invite>",
            "start": "10/10/26, 10:00 AM", "match_reason": "Overlaps this recording and matches its title.",
            "match_strength": "strong", "event_url": "https://calendar.google.com/calendar/event?eid=exact-event",
            "recipients": [{"name": "Alice & team", "email": "alice@example.com", "source": "calendar_invite",
                            "response_status": "accepted"}, {"email": "bob@example.com", "source": "calendar_invite"}]}])

    def test_calendar_lookup_only_starts_after_the_explicit_button(self):
        self.calendar("idle")
        body = self.request()[2]
        self.assertIn("Find attendees from calendar", body)
        self.assertNotIn("alice@example.com", body)
        self.assertNotIn("Use these suggestions", body)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.decide("sharing_suggest")[0], 303)
        self.assertEqual(self.calls, [("sharing_suggest", "exact-job", "exact-revision", None)])
        self.assertEqual(self.decide("sharing_suggest", emails="alice@example.com")[0], 400)

    def test_calendar_candidates_show_sources_and_send_only_opaque_selection(self):
        self.calendar()
        calls = []
        self.server.on_sharing = lambda action, job, revision, emails=None, suggestion=None: calls.append(
            (action, job, revision, emails, suggestion))
        body = self.request()[2]
        self.assertIn("Portal planning &lt;invite&gt;", body)
        self.assertIn("Alice &amp; team (alice@example.com)", body)
        self.assertIn("Calendar invite · accepted", body)
        self.assertIn("10/10/26, 10:00 AM", body)
        self.assertIn("Overlaps this recording and matches its title.", body)
        self.assertEqual(body.count("Invited people; attendance unverified."), 1)
        self.assertIn("Open calendar invitation", body)
        self.assertIn("Use these suggestions", body)
        form = next(part.split("</form>")[0] for part in body.split('<form method="post"')[1:]
                    if 'value="sharing_use_suggestions"' in part.split("</form>")[0])
        self.assertIn('name="job" value="exact-job"', form)
        self.assertIn('name="revision" value="exact-revision"', form)
        self.assertIn('name="suggestion" value="opaque-validated-event-token"', form)
        self.assertNotIn('name="emails"', form)
        self.assertNotIn('value="sharing_confirm"', form)
        self.assertEqual(calls, [])
        self.assertEqual(self.decide("sharing_use_suggestions", suggestion="opaque-validated-event-token")[0], 303)
        self.assertEqual(calls, [("sharing_use_suggestions", "exact-job", "exact-revision", None,
                                 "opaque-validated-event-token")])

    def test_selected_calendar_addresses_prefill_available_form_without_sharing(self):
        self.calendar()
        self.items[0]["recipient_draft"] = "alice@example.com\nbob@example.com"
        body = self.request()[2]
        self.assertIn("alice@example.com\nbob@example.com</textarea>", body)
        self.assertIn("Prepare private sharing copy", body)
        self.assertNotIn('value="sharing_confirm"', body)
        self.assertEqual(self.calls, [])

    def test_ready_calendar_selection_requires_save_before_final_share(self):
        self.ready()
        self.calendar()
        self.items[0]["recipient_draft"] = "carol@example.com"
        body = self.request()[2]
        self.assertIn('<details open><summary>Change recipients</summary>', body)
        self.assertIn("carol@example.com</textarea>", body)
        self.assertIn("Save the suggested email addresses below", body)
        self.assertNotIn('value="sharing_confirm"', body)
        self.assertEqual(self.calls, [])

    def test_calendar_busy_error_empty_and_invalid_link_states_are_safe(self):
        self.calendar("searching")
        body = self.request()[2]
        self.assertIn("Looking for matching calendar invitations", body)
        self.assertIn("Refresh suggestions", body)
        self.assertNotIn('value="sharing_suggest"', body)
        self.assertNotIn('value="sharing_use_suggestions"', body)
        self.items[0].update(suggestion_status="needs_attention", suggestion_error="Calendar <offline>")
        body = self.request()[2]
        self.assertIn("Calendar &lt;offline&gt;", body)
        self.assertIn('value="sharing_suggest"', body)
        self.items[0].update(suggestion_status="ready")
        self.items[0]["suggestion_warnings"] = ["One calendar was unavailable <review manually>."]
        self.assertIn("One calendar was unavailable &lt;review manually&gt;.", self.request()[2])
        for link in ("javascript:alert(1)", "http://calendar.google.com/calendar/event?eid=exact-event",
                     "https://calendar.google.com.evil.example/calendar/event?eid=exact-event",
                     "https://calendar.google.com@evil.example/calendar/event?eid=exact-event"):
            self.items[0]["suggested_events"][0]["event_url"] = link
            self.assertNotIn("Open calendar invitation</a>", self.request()[2])
        self.items[0]["suggested_events"] = []
        self.assertIn("No matching calendar invitations were found", self.request()[2])
        self.items[0]["sharing_started"] = True
        self.assertNotIn("Calendar attendees", self.request()[2])
        self.assertEqual(self.calls, [])

    def test_calendar_selection_fields_and_origin_are_strict(self):
        body = urlencode({"action": "sharing_use_suggestions", "job": "exact-job", "revision": "exact-revision",
                          "suggestion": "opaque-validated-event-token"})
        for extra in ("&suggestion=other-token", "&emails=mallory%40example.com", "&event_id=unbound-id"):
            self.assertEqual(self.request("POST", body + extra)[0], 400)
        self.assertEqual(self.request("POST", body, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.decide("sharing_use_suggestions")[0], 400)
        self.assertEqual(self.decide("sharing_use_suggestions", suggestion="")[0], 400)
        self.assertEqual(self.calls, [])

    def test_calendar_time_format_preserves_explicit_offset_and_unknown_literals(self):
        self.calendar()
        event = self.items[0]["suggested_events"][0]
        for timestamp, displayed in (
                ("2026-10-09T19:00:00+08:00", "10/9/26 · 7:00 PM (UTC+08:00)"),
                ("2026-10-09T11:00:00Z", "10/9/26 · 11:00 AM (UTC+00:00)"),
                ("2026-10-09T06:30:00-04:30", "10/9/26 · 6:30 AM (UTC-04:30)"),
                ("2026-10-09T11:00:00", "2026-10-09T11:00:00"),
                ("Unknown <time>", "Unknown &lt;time&gt;")):
            event["start"] = timestamp
            self.assertIn(displayed, self.request()[2])
        event["start"] = "x" * 170
        body = self.request()[2]
        self.assertIn("x" * 160 + "…", body)
        self.assertNotIn("x" * 161, body)

    def test_calendar_uses_recording_timezone_and_shows_checked_source_calendars(self):
        self.calendar()
        event = self.items[0]["suggested_events"][0]
        event.update(start="2026-10-09T11:00:00Z", display_timezone="Asia/Singapore", calendar_name="Acme & team")
        self.items[0]["suggestion_scope"] = [{"name": "Personal", "searched": True},
                                             {"name": "Acme & team", "searched": False}]
        body = self.request()[2]
        self.assertIn("10/9/26 · 7:00 PM (UTC+08:00)", body)
        self.assertIn("Calendar: Acme &amp; team", body)
        self.assertIn("Personal · Checked", body)
        self.assertIn("Acme &amp; team · Not checked", body)
        event["display_timezone"] = "Invalid/timezone"
        self.assertIn("10/9/26 · 11:00 AM (UTC+00:00)", self.request()[2])

    def test_automatic_lookup_explanation_does_not_trigger_lookup_on_page_read(self):
        self.calendar("idle")
        self.items[0]["auto_suggestions"] = True
        body = self.request()[2]
        self.assertIn("For new meetings, calendar attendees are found automatically once notes are filed", body)
        self.assertIn("You can also look them up here", body)
        self.assertIn("Find attendees from calendar", body)
        self.assertNotIn("Use these suggestions", body)
        self.assertEqual(self.calls, [])
        self.items[0]["auto_suggestions"] = False
        self.assertNotIn("found automatically once notes are filed", self.request()[2])

    def test_automatic_results_still_require_selection_preparation_and_confirmation(self):
        self.calendar()
        self.items[0].update(auto_suggestions=True, suggestion_automatic=True)
        body = self.request()[2]
        self.assertIn("Found automatically from your calendar", body)
        self.assertIn("Refresh calendar suggestions", body)
        self.assertIn("Invited people; attendance unverified", body)
        self.assertIn("Use these suggestions", body)
        self.assertIn('spellcheck="false"></textarea>', body)
        self.assertIn("Prepare private sharing copy", body)
        self.assertNotIn('value="sharing_confirm"', body)
        # Selecting a meeting in the navigation is not recipient consent.
        # No decision form may preselect an attendee.
        for form in body.split('<form method="post"')[1:]:
            self.assertNotRegex(form.split("</form>")[0], r"<input\b[^>]*\bchecked\b")
        self.assertEqual(self.calls, [])

    def test_automatic_empty_and_failed_lookup_copy_does_not_claim_attendees_were_found(self):
        self.calendar()
        self.items[0].update(auto_suggestions=True, suggestion_automatic=True, suggested_events=[])
        body = self.request()[2]
        self.assertIn("Your calendar was checked automatically", body)
        self.assertNotIn("Found automatically from your calendar", body)
        self.assertIn("No matching calendar invitations were found", body)
        self.items[0].update(suggestion_status="needs_attention", suggestion_error="Calendar is unavailable.")
        body = self.request()[2]
        self.assertIn("Retry calendar lookup", body)
        self.assertNotIn("Found automatically from your calendar", body)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
