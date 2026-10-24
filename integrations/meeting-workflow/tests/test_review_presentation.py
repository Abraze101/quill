"""Presentation checks using synthetic notes and temporary local review state."""
from html.parser import HTMLParser
import http.client
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.review_server import create_server, _selection
from quill_workflow.review_ui import notes_html, workspace
from quill_workflow.routing import RoutingReviews


class Element:
    def __init__(self, tag, attributes=()):
        self.tag = tag
        self.attributes = dict(attributes)
        self.children = []

    @property
    def text(self):
        return "".join(child.text if isinstance(child, Element) else child
                       for child in self.children)

    def find(self, tag=None, **attributes):
        found = []
        for child in self.children:
            if not isinstance(child, Element):
                continue
            if ((tag is None or child.tag == tag)
                    and all(child.attributes.get(key) == value
                            for key, value in attributes.items())):
                found.append(child)
            found.extend(child.find(tag, **attributes))
        return found


class Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attributes):
        node = Element(tag, attributes)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        self.stack[-1].children.append(value)


def parsed(source):
    return Document(source).root


def by_class(document, name):
    return [node for node in document.find()
            if name in node.attributes.get("class", "").split()]


class NotesPresentationTests(unittest.TestCase):
    def test_saved_headings_lists_and_emphasis_remain_readable(self):
        document = parsed(notes_html(
            "# Planning meeting\n\n## Decisions\n"
            "- **Owner:** Alice & Bob\n* Keep the release date\n"
            "1. Review the draft\n\nFirst paragraph line\nSecond line\n\n"
            "###### Reference\nSource retained."
        ))
        headings = [node for node in document.find()
                    if re.fullmatch(r"h[1-6]", node.tag)]
        self.assertEqual([node.text for node in headings],
                         ["Planning meeting", "Decisions", "Reference"])
        self.assertFalse(document.find("h1"))
        self.assertFalse(document.find("h2"))
        self.assertEqual([node.text for node in document.find("li")],
                         ["Owner: Alice & Bob", "Keep the release date", "Review the draft"])
        self.assertEqual([node.text for node in document.find("strong")], ["Owner:"])
        self.assertEqual(len(document.find("br")), 1)
        self.assertIn("Source retained.", document.text)

    def test_notes_never_create_html_scripts_links_images_or_event_handlers(self):
        source = (
            '# <script>alert("notes")</script>\n'
            '- <img src="https://outside.example/image" onerror="alert(1)">\n'
            '<a href="javascript:alert(1)">click</a>\n'
            '[Read source](https://outside.example/private)\n'
            '![Image](https://outside.example/pixel)\n'
            '<iframe src="https://outside.example/frame"></iframe>\n'
            '**<svg onload="alert(1)">**\n'
            '&lt;script&gt;literal escaped text&lt;/script&gt;'
        )
        rendered = notes_html(source)
        document = parsed(rendered)
        self.assertTrue(all(node.tag in {"div", "h3", "p", "ul", "li", "br", "strong"}
                            for node in document.find()))
        for node in document.find():
            self.assertFalse({"href", "src", "srcset"} & set(node.attributes))
            self.assertFalse(any(name.startswith("on") for name in node.attributes))
        self.assertIn('<script>alert("notes")</script>', document.text)
        self.assertIn('[Read source](https://outside.example/private)', document.text)
        self.assertIn('![Image](https://outside.example/pixel)', document.text)
        self.assertIn('&lt;script&gt;literal escaped text&lt;/script&gt;', document.text)


class WorkspacePresentationTests(unittest.TestCase):
    def entries(self):
        return [
            {"title": 'Same <meeting> "title"', "status": "Choose a folder",
             "selection": "a" * 24, "attention": True,
             "body": '<form method="post"><button>First decision</button></form>',
             "notes": "First private note"},
            {"title": 'Same <meeting> "title"', "status": "Processing",
             "selection": "b" * 24, "attention": False,
             "body": "<p>Second review</p>", "notes": "Second private note"},
            {"title": "Third meeting", "status": "Waiting", "selection": "c" * 24,
             "attention": False, "body": "<p>Third review</p>", "notes": "Third private note"},
        ]

    def test_library_uses_distinct_native_links_without_loading_meeting_forms(self):
        entries = self.entries()
        document = parsed(workspace(entries, "/local-token/"))
        rows = by_class(document, "meeting-row")
        self.assertEqual(len(rows), 3)
        self.assertFalse(document.find("input", type="radio"))
        self.assertFalse(document.find("form", method="post"))
        self.assertFalse(by_class(document, "meeting-panel"))
        links = [link for link in document.find("a")
                 if "?meeting=" in link.attributes.get("href", "")]
        self.assertEqual(len(links), 3)
        self.assertEqual(len({link.attributes["href"] for link in links}), 3)
        for entry in entries:
            self.assertTrue(any(entry["selection"] in link.attributes["href"] for link in links))
            self.assertNotIn(entry["notes"], document.text)
        ids = [node.attributes["id"] for node in document.find() if "id" in node.attributes]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse(document.find("meeting"))

    def test_detail_renders_only_selected_meeting_and_has_a_library_link(self):
        document = parsed(workspace(self.entries(), "/local-token/", selected="b" * 24))
        self.assertIn("Second private note", document.text)
        self.assertNotIn("First private note", document.text)
        self.assertNotIn("Third private note", document.text)
        self.assertNotIn("First decision", document.text)
        self.assertFalse(document.find("input", type="radio"))
        self.assertTrue(document.find("a", href="/local-token/"))

    def test_empty_workspace_has_no_dangling_keyboard_or_panel_associations(self):
        document = parsed(workspace([], "/local-token/"))
        self.assertFalse(document.find("input", type="radio"))
        self.assertFalse(by_class(document, "meeting-panel"))
        self.assertFalse(document.find("form", method="post"))
        self.assertIn("No meetings", document.text)
        self.assertTrue(document.find("a", href="/local-token/"))


class ReviewPresentationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.processing = []
        self.sharing = []
        self.calls = []
        self.server = create_server(
            self.state, on_decision=lambda record: self.calls.append(record),
            processing_options=lambda: self.processing,
            on_retry=lambda *args: self.calls.append(args),
            sharing_options=lambda: self.sharing,
            on_sharing=lambda *args, **kwargs: self.calls.append((args, kwargs)),
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary.cleanup()

    def page(self, job=None, tab="notes"):
        path = self.server.review_path
        if job is not None:
            path += "?" + urlencode({"meeting": _selection(job), "tab": tab})
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("GET", path,
                               headers={"Host": self.server.review_host})
            response = connection.getresponse()
            source = response.read().decode()
            self.assertEqual(response.status, 200, source)
            return parsed(source), dict(response.getheaders())
        finally:
            connection.close()

    def folder_review(self, job="folder-job"):
        reviews = RoutingReviews(self.state)
        try:
            record = reviews.propose(
                job, b'{"segments":[{"text":"Synthetic planning meeting."}]}',
                [{"id": "company", "name": "Company", "drive_folder_id": "folder-123"}],
                {"schema_version": 1, "company_id": "company", "confidence": "high",
                 "reason": "Company named in the notes.", "evidence": [0], "ambiguous": False},
            )
        finally:
            reviews.close()
        path = self.state / "results" / job / "notes" / "notes.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Synthetic planning meeting\n\n- Review the budget.")
        return record

    def processing_item(self, state, **changes):
        item = {"job": state + "-job", "title": "Meeting " + state,
                "state": state, "detail": "Saved detail for " + state,
                "retryable": False, "revision": "exact-processing-revision"}
        item.update(changes)
        return item

    def test_folder_review_and_waiting_state_share_one_library_entry_and_decision(self):
        record = self.folder_review()
        self.processing = [self.processing_item(
            "waiting_for_company", job=record["job"],
            detail="Choose this meeting's company folder")]
        library, _ = self.page()
        self.assertEqual(len(by_class(library, "meeting-row")), 1)
        self.assertFalse(library.find("form", method="post"))
        self.assertNotIn("Review the budget.", library.text)
        document, _ = self.page(record["job"])
        self.assertEqual(len(document.find("form", method="post")), 1)
        self.assertEqual(len(document.find("button", value="approve")), 1)
        self.assertEqual(len(document.find("button", value="skip")), 1)
        self.assertTrue(document.find("input", name="revision", value=record["revision"]))
        self.assertTrue(document.find("select", name="company_id"))
        self.assertIn("Review the budget.", document.text)
        self.assertEqual(self.calls, [])

    def test_processing_failure_has_one_recovery_action_and_hides_stale_folder_choice(self):
        record = self.folder_review()
        self.processing = [self.processing_item(
            "needs_attention", job=record["job"], retryable=True,
            detail="The saved processing attempt needs review.")]
        document, _ = self.page(record["job"])
        self.assertIn("The saved processing attempt needs review.", document.text)
        self.assertFalse(document.find("button", value="approve"))
        self.assertFalse(document.find("button", value="resume"))
        retry = next(form for form in document.find("form", method="post")
                     if form.find("button", value="retry"))
        self.assertEqual(
            {node.attributes["name"] for node in retry.find("input")}
            | {node.attributes["name"] for node in retry.find("button")},
            {"job", "revision", "action"},
        )
        self.assertTrue(retry.find("input", name="revision", value="exact-processing-revision"))
        self.assertEqual(self.calls, [])

    def assert_processing_suppresses_stale_resume(self, state, expected_status, retryable):
        record = self.folder_review()
        reviews = RoutingReviews(self.state)
        try:
            reviews.approve(record["job"], "company", record["revision"])
        finally:
            reviews.close()
        self.server.resume_options = lambda saved: ["publish"]
        self.server.on_resume = lambda *args: self.calls.append(args)
        self.processing = [self.processing_item(
            state, job=record["job"], retryable=retryable,
            detail="Processing state: " + state)]
        library, _ = self.page()
        self.assertEqual([node.text for node in by_class(library, "row-status")], [expected_status])
        document, _ = self.page(record["job"])
        activity, _ = self.page(record["job"], "activity")
        self.assertIn(self.processing[0]["detail"], activity.text)
        self.assertIn(expected_status, document.text)
        self.assertFalse(document.find("button", value="approve"))
        self.assertFalse(document.find("button", value="resume"))
        expected_retry = state == "needs_attention" and retryable
        self.assertEqual(len(document.find("button", value="retry")), int(expected_retry))
        if expected_retry:
            self.assertTrue(document.find("input", name="revision", value="exact-processing-revision"))
        self.assertEqual(self.calls, [])

    def test_running_meeting_preserves_status_without_a_stale_resume_action(self):
        self.assert_processing_suppresses_stale_resume("running", "Processing", False)

    def test_automatic_retry_preserves_status_without_duplicate_recovery_actions(self):
        self.assert_processing_suppresses_stale_resume("retrying", "Waiting to retry", True)

    def test_processing_failure_preserves_retry_without_a_stale_resume_action(self):
        self.assert_processing_suppresses_stale_resume("needs_attention", "Needs attention", True)

    def test_worker_failure_without_meeting_identity_is_visible_without_retry_form(self):
        self.processing = [{"title": "Meeting processing stopped",
                            "detail": "Automatic recovery needs attention",
                            "retryable": False}]
        document, _ = self.page()
        self.assertIn("Meeting processing stopped", document.text)
        self.assertIn("Needs attention", document.text)
        detail, _ = self.page(("worker", 0))
        self.assertIn("Automatic recovery needs attention", detail.text)
        self.assertFalse(document.find("form", method="post"))
        self.assertEqual(self.calls, [])

    def test_processing_labels_distinguish_active_waiting_and_attention_states(self):
        labels = {"running": "Processing", "retrying": "Waiting to retry",
                  "needs_attention": "Needs attention",
                  "waiting_for_company": "Check status", "queued": "Waiting"}
        self.processing = [self.processing_item(state) for state in labels]
        document, _ = self.page()
        rows = by_class(document, "meeting-row")
        self.assertEqual(len(rows), len(labels))
        for state, expected in labels.items():
            row = next(row for row in rows if "Meeting " + state in row.text)
            self.assertEqual(by_class(row, "row-status")[0].text, expected)
            detail, _ = self.page(state + "-job")
            if state == "waiting_for_company":
                self.assertIn("saved folder review is unavailable", detail.text)
            else:
                self.assertIn("Saved detail for " + state, detail.text)
            self.assertNotRegex(by_class(detail, "meeting-panel")[0].text, r"\d+\s*%")
            self.assertFalse(detail.find("progress"))
            self.assertFalse(detail.find(role="progressbar"))
        self.assertFalse(document.find("form", method="post"))
        self.assertEqual(self.calls, [])

    def test_unknown_processing_state_preserves_detail_without_claiming_completion(self):
        self.processing = [self.processing_item(
            "future_state", detail="A new worker state needs interpretation.")]
        document, _ = self.page("future_state-job")
        self.assertIn("Check status", document.text)
        self.assertIn("A new worker state needs interpretation.", document.text)
        self.assertNotIn("Finished", document.text)
        self.assertNotIn("complete", document.text.lower())
        self.assertFalse(document.find("form", method="post"))

    def test_each_sharing_detail_has_unique_labelled_fields_and_preserves_its_notes(self):
        for index, status in enumerate(("available", "available", "ready", "ready")):
            item = {"job": f"sharing-job-{index}", "revision": f"sharing-revision-{index}",
                    "title": f"Sharing meeting {index}", "status": status,
                    "preview": f"# Notes {index}\nKeep sharing-emails-0 and sharing-change-0 verbatim."}
            if status == "ready":
                item.update(recipients=["alice@example.com"], cancellable=True,
                            draft_url=f"https://docs.google.com/document/d/document-{index}/edit")
            self.sharing.append(item)
        library, _ = self.page()
        self.assertFalse(library.find("textarea", name="emails"))
        field_ids = []
        for item in self.sharing:
            document, _ = self.page(item["job"], "sharing")
            fields = document.find("textarea", name="emails")
            self.assertEqual(len(fields), 1)
            field_ids.extend(field.attributes["id"] for field in fields)
            for field in fields:
                self.assertEqual(len(document.find("label", **{"for": field.attributes["id"]})), 1)
            self.assertIn("Keep sharing-emails-0 and sharing-change-0 verbatim.", document.text)
            all_ids = [node.attributes["id"] for node in document.find() if "id" in node.attributes]
            self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertEqual(len(field_ids), len(set(field_ids)))
        self.assertEqual(self.calls, [])

    def test_pages_introduce_no_scripts_external_resources_or_automatic_refresh(self):
        record = self.folder_review()
        self.processing = [self.processing_item("running", detail="Writing notes")]
        pages = [(None, "notes")] + [(record["job"], tab) for tab in
                  ("notes", "transcript", "recording", "sharing", "activity")]
        for job, tab in pages:
            with self.subTest(job=job, tab=tab):
                document, headers = self.page(job, tab)
                for tag in ("script", "iframe", "object", "embed", "img", "audio", "video", "link", "base"):
                    self.assertFalse(document.find(tag), tag)
                for node in document.find():
                    self.assertFalse(any(name.startswith("on") for name in node.attributes))
                    self.assertFalse({"src", "srcset"} & set(node.attributes))
                    if node.tag == "meta":
                        self.assertNotEqual(node.attributes.get("http-equiv", "").lower(), "refresh")
                for style in document.find("style"):
                    self.assertNotRegex(style.text.lower(), r"@import|url\s*\(")
                self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
                self.assertIn("form-action 'self'", headers["Content-Security-Policy"])
                self.assertNotIn("script-src", headers["Content-Security-Policy"])
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["Referrer-Policy"], "strict-origin")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
