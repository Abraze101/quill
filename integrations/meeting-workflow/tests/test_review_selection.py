"""Keep the user's selected meeting through the native form round trip."""
import http.client
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.review_server import create_server, _selection
from test_review_presentation import by_class, parsed


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.calls = []
        self.items = [
            {"job": job, "revision": "saved-revision", "title": title,
             "status": "available", "preview": "# " + title,
             "recipients": [], "cancellable": True}
            for job, title in (("earlier", "Earlier meeting"), ("later", "Later meeting"))
        ]
        self.server = create_server(
            Path(self.temporary.name), sharing_options=lambda: self.items,
            on_sharing=lambda *args, **kwargs: self.calls.append((args, kwargs)),
            on_decision=lambda *args: self.calls.append(("decision", args)),
            on_resume=lambda *args: self.calls.append(("resume", args)),
            on_retry=lambda *args: self.calls.append(("retry", args)),
            on_delete=lambda *args, **kwargs: self.calls.append(("delete", args, kwargs)))
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()
        self.temporary.cleanup()

    def request(self, path=None, fields=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {"Host": self.server.review_host}
        body = None
        if fields is not None:
            body = urlencode(fields)
            headers.update({"Content-Type": "application/x-www-form-urlencoded",
                            "Origin": self.server.review_origin})
        try:
            connection.request("POST" if fields is not None else "GET",
                               path or self.server.review_path, body, headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), parsed(response.read().decode())
        finally:
            connection.close()

    def detail_path(self, job="later", tab="sharing"):
        return self.server.review_path + "?" + urlencode({
            "meeting": _selection(job), "tab": tab})

    def assert_selected(self, document, title="Later meeting"):
        self.assertIn(title, document.text)
        self.assertNotIn("Earlier meeting", document.text)
        self.assertFalse(document.find("input", type="radio"))

    def assert_library(self, document):
        self.assertIn("Earlier meeting", document.text)
        self.assertFalse(document.find("form", method="post"))
        self.assertFalse(document.find("textarea", name="emails"))
        self.assertFalse(document.find("input", type="radio"))

    def test_prepare_returns_to_submitted_meeting_without_changing_form_contract(self):
        status, headers, _ = self.request(self.server.review_path + "decision", {
            "job": "later", "revision": "saved-revision", "action": "sharing_prepare",
            "emails": "person@example.com",
        })
        self.assertEqual(status, 303)
        destination = headers["Location"]
        self.assertTrue(destination.startswith(self.server.review_path + "?meeting="))
        self.assertNotIn("later", destination)
        self.assertNotIn("person", destination)
        self.assertEqual(self.calls, [
            (("sharing_prepare", "later", "saved-revision"), {"emails": "person@example.com"})])
        status, _, document = self.request(destination)
        self.assertEqual(status, 200)
        self.assert_selected(document)
        self.assertEqual(len(self.calls), 1)
        refresh = next(link for link in document.find("a")
                       if link.text.strip() == "Refresh status")
        self.assertEqual(refresh.attributes["href"], destination)
        self.assert_selected(self.request(refresh.attributes["href"])[2])

    def test_expired_selection_returns_library_without_opening_another_meeting(self):
        _, headers, _ = self.request(self.server.review_path + "decision", {
            "job": "later", "revision": "saved-revision", "action": "sharing_private",
        })
        self.items.pop()
        status, _, document = self.request(headers["Location"])
        self.assertEqual(status, 200)
        self.assert_library(document)
        self.assertIn("no longer available", document.text.lower())

    def test_inline_calendar_and_busy_sharing_refresh_preserve_meeting(self):
        self.items[1]["suggestion_status"] = "searching"
        _, _, document = self.request(self.detail_path())
        refresh = next(link for link in document.find("a") if link.text == "Refresh suggestions")
        self.assert_selected(self.request(refresh.attributes["href"])[2])
        self.items[1]["status"] = "preparing"
        _, _, document = self.request(self.detail_path())
        refresh = next(link for link in document.find("a") if link.text == "Refresh status")
        self.assert_selected(self.request(refresh.attributes["href"])[2])
        self.assertEqual(self.calls, [])

    def test_only_exact_opaque_selection_query_is_accepted(self):
        for query in ("?meeting=", "?meeting=../notes", "?file=notes",
                      "?meeting=" + "a" * 24 + "&meeting=" + "b" * 24,
                      "?meeting=" + "a" * 24 + "&other=1"):
            with self.subTest(query=query):
                self.assertEqual(self.request(self.server.review_path + query)[0], 404)
        self.assertEqual(self.calls, [])
        status, _, document = self.request(self.server.review_path + "?meeting=" + "0" * 24)
        self.assertEqual(status, 200)
        self.assert_library(document)
        self.assertIn("no longer available", document.text.lower())

    def test_get_accepts_bounded_navigation_without_running_actions(self):
        queries = [
            {"view": "all"}, {"view": "attention"}, {"q": ""},
            {"q": "é" * 160}, {"folder": "A" + "b" * 127},
            {"meeting": _selection("later"), "view": "attention", "folder": "acme",
             "q": "Later meeting", "tab": "notes"},
        ]
        queries += [{"meeting": _selection("later"), "tab": tab}
                    for tab in ("notes", "transcript", "recording", "sharing", "activity")]
        for query in queries:
            with self.subTest(query=query):
                status, headers, _ = self.request(self.server.review_path + "?" + urlencode(query))
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.calls, [])

    def test_get_rejects_unknown_repeated_or_unbounded_navigation(self):
        invalid = [
            "view=", "view=finished", "view=all&view=attention", "folder=",
            "folder=../acme", "folder=acme%2Fprivate", "folder=" + "a" * 129,
            "folder=a&folder=b", "q=a&q=b", "q=" + "a" * 161,
            "q=hidden%00text", "q=hidden%09text", "q=hidden%0Atext", "q=hidden%7Ftext",
            "q=%FF", "q=" + "%61" * 801, "unknown=value", "q",
            "tab=notes", "meeting=" + _selection("later") + "&tab=unknown",
            "meeting=" + _selection("later") + "&tab=notes&tab=sharing",
            "meeting=" + "A" * 24,
            "meeting=" + _selection("later") + "&view=all&folder=a&q=x&tab=notes&extra=x",
        ]
        for query in invalid:
            with self.subTest(query=query):
                status, _, document = self.request(self.server.review_path + "?" + query)
                self.assertEqual(status, 404)
                self.assertFalse(document.find("form", method="post"))
                self.assertNotIn("Earlier meeting", document.text)
        self.assertEqual(self.calls, [])

    def library_metadata(self):
        return {
            "earlier": {"title": "Earlier meeting", "company_id": "acme",
                        "folder_name": "Acme", "participants": ["Alice"],
                        "display_date": "10/20/26", "recorded_at": 200,
                        "storage_status": "saved_in_drive"},
            "later": {"title": "Later meeting", "company_id": "acme-health",
                      "folder_name": "Acme Health", "participants": ["Bob"],
                      "display_date": "10/21/26", "recorded_at": 300,
                      "storage_status": "saved_in_drive"},
        }

    def test_search_text_is_an_escaped_value_and_never_markup(self):
        search = '\"><img src="https://outside.example/pixel" onerror="alert(1)">'
        status, _, document = self.request(self.server.review_path + "?" + urlencode({"q": search}))
        self.assertEqual(status, 200)
        self.assertEqual(document.find("input", name="q")[0].attributes["value"], search)
        self.assertFalse(document.find("img"))
        self.assertFalse(document.find("script"))
        self.assertFalse(any(key.startswith("on") for element in document.find()
                             for key in element.attributes))
        self.assertEqual(self.calls, [])

    def test_library_filters_compose_and_search_matches_saved_metadata(self):
        self.items[1]["status"] = "needs_attention"
        cases = [
            ({}, ["Later meeting", "Earlier meeting"]),
            ({"view": "attention"}, ["Later meeting"]),
            ({"folder": "acme"}, ["Earlier meeting"]),
            ({"q": "aLiCe"}, ["Earlier meeting"]),
            ({"q": "10/21/26"}, ["Later meeting"]),
            ({"q": "Acme Health"}, ["Later meeting"]),
            ({"view": "attention", "folder": "acme-health", "q": "bOb"}, ["Later meeting"]),
            ({"view": "attention", "folder": "acme"}, []),
            ({"folder": "unknown-company"}, []),
        ]
        with patch("quill_workflow.review_server.meeting_library", return_value=self.library_metadata()):
            for fields, expected in cases:
                with self.subTest(fields=fields):
                    path = self.server.review_path + ("?" + urlencode(fields) if fields else "")
                    status, _, document = self.request(path)
                    self.assertEqual(status, 200)
                    self.assertEqual([row.find("strong")[0].text for row in by_class(document, "meeting-row")], expected)
                    self.assertFalse(document.find("form", method="post"))
                    if not expected:
                        self.assertTrue(by_class(document, "empty"))
        self.assertEqual(self.calls, [])

    def test_filtered_detail_tabs_refresh_and_back_preserve_library_context(self):
        self.items[1]["status"] = "needs_attention"
        filters = {"view": "attention", "folder": "acme-health", "q": "Bob"}
        expected = {key: [value] for key, value in filters.items()}
        with patch("quill_workflow.review_server.meeting_library", return_value=self.library_metadata()):
            _, _, library = self.request(self.server.review_path + "?" + urlencode(filters))
            rows = by_class(library, "meeting-row")
            self.assertEqual(len(rows), 1)
            row_path = rows[0].attributes["href"]
            self.assertEqual(parse_qs(urlsplit(row_path).query),
                             {**expected, "meeting": [_selection("later")]})
            _, _, detail = self.request(row_path)
            self.assert_selected(detail)
            back = next(link for link in detail.find("a") if link.text == "Meetings")
            self.assertEqual(parse_qs(urlsplit(back.attributes["href"]).query), expected)
            self.assertEqual(len(by_class(self.request(back.attributes["href"])[2], "meeting-row")), 1)
            for tab in by_class(detail, "tab"):
                query = parse_qs(urlsplit(tab.attributes["href"]).query)
                for key, value in expected.items():
                    self.assertEqual(query[key], value)
                self.assertEqual(query["meeting"], [_selection("later")])
                status, _, selected_tab = self.request(tab.attributes["href"])
                self.assertEqual(status, 200)
                self.assert_selected(selected_tab)
                refresh = next(link for link in selected_tab.find("a") if link.text == "Refresh status")
                self.assertEqual(refresh.attributes["href"], tab.attributes["href"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
