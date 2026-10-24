import http.client
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.review_server import MAX_FORM_BYTES, MAX_NOTES_BYTES, create_server, serve_review, _selection
from quill_workflow.routing import RoutingReviews


class ReviewServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.companies = [{"id": "health", "name": 'Health <script>alert("company")</script>',
                           "drive_folder_id": "folder-health-001"}]
        self.transcript = json.dumps({"segments": [{"text": "Synthetic health meeting"}]}).encode()
        self.suggestion = {"schema_version": 1, "company_id": "health", "confidence": "high",
                           "reason": '<img src=x onerror="alert(1)">', "evidence": [0], "ambiguous": False}
        self.reviews = RoutingReviews(self.state)
        self.record = self.reviews.propose("test-job", self.transcript, self.companies, self.suggestion)
        self.decisions = []
        self.server = create_server(self.state, self.decisions.append)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.reviews.close()
        self.temporary.cleanup()

    def request(self, method="GET", path=None, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        base = {"Host": self.server.review_host}
        if method == "POST":
            base.update({"Origin": self.server.review_origin, "Content-Type": "application/x-www-form-urlencoded"})
        base.update(headers or {})
        base = {key: value for key, value in base.items() if value is not None}
        connection.request(method, self.server.review_path if path is None else path, body=body, headers=base)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        connection.close()
        return result

    def detail(self, job="test-job", tab="notes"):
        return self.request(path=self.server.review_path + "?" + urlencode({
            "meeting": _selection(job), "tab": tab}))

    def decide(self, action="approve", **changes):
        fields = {"job": "test-job", "revision": self.record["revision"], "company_id": "health", "action": action}
        if action == "resume":
            fields["stages"] = "archive,publish"
        fields.update(changes)
        return self.request("POST", self.server.review_path + "decision", urlencode(fields))

    def write_notes(self, content):
        path = self.state / "results/test-job/notes/notes.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_loopback_token_and_escaped_review_contents(self):
        self.write_notes('# Notes <script>alert("notes")</script>\nMeeting & action items')
        status, headers, body = self.detail()
        self.assertEqual(status, 200)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertGreaterEqual(len(self.server.review_path.strip("/")), 40)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("&lt;img src=x", body)
        self.assertIn("Meeting &amp; action items", body)
        self.assertNotIn("<script>", body)
        self.assertNotIn("<img ", body)
        self.assertIn("Nothing is shared with participants", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "strict-origin")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_transcript_tab_renders_validated_capture_notice_once_and_fails_closed(self):
        from quill_workflow.capture_quality import NOTICE
        from quill_workflow.pipeline import SCHEMA, atomic_json
        with sqlite3.connect(self.state / "jobs.sqlite3") as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO jobs VALUES (?,?,?)", ("test-job", str(self.state / "original"), 1))
        transcript = {"segments": [{"start_ms": 0, "speaker": "system", "text": "Exact <speech> & punctuation."}]}
        path = self.state / "recordings/test-job/transcript.json"
        atomic_json(path, transcript)
        status, _, legacy = self.detail(tab="transcript")
        self.assertEqual(status, 200)
        self.assertNotIn(NOTICE, legacy)
        exact = "System audio: Exact &lt;speech&gt; &amp; punctuation."
        self.assertIn(exact, legacy)
        transcript["capture_quality"] = {"status": "incomplete", "affected_sources": ["system"]}
        atomic_json(path, transcript)
        status, _, warned = self.detail(tab="transcript")
        self.assertEqual(status, 200)
        self.assertEqual(warned.count(NOTICE), 1)
        self.assertIn(NOTICE + "\n\n" + exact, warned)
        transcript["capture_quality"]["status"] = "unsupported"
        atomic_json(path, transcript)
        status, _, invalid = self.detail(tab="transcript")
        self.assertEqual(status, 200)
        self.assertIn("A transcript preview is not available", invalid)
        self.assertNotIn(NOTICE, invalid)
        self.assertNotIn(exact, invalid)

    def test_unknown_token_and_paths_cannot_read_state(self):
        for path in ("/", "/wrong-token/", self.server.review_path + "../../reviews.sqlite3",
                     self.server.review_path + "?file=reviews.sqlite3", "/results/test-job/notes/notes.md"):
            self.assertEqual(self.request(path=path)[0], 404)
        self.assertEqual(self.request(headers={"Host": "evil.example"})[0], 403)

    def test_general_folder_is_available_only_in_its_saved_review_and_requires_a_choice(self):
        categories = self.companies + [{"id": "general", "name": "General",
                                      "drive_folder_id": "folder-general-001"}]
        self.record = self.reviews.propose("test-job", self.transcript, categories,
            dict(self.suggestion, company_id="general", reason="A broader planning meeting."))
        status, _, body = self.detail()
        self.assertEqual(status, 200)
        self.assertIn("Suggested folder:</strong> General", body)
        self.assertIn('<option value="general">General</option>', body)
        self.assertIn("Choose a folder…", body)
        self.assertEqual(self.reviews.get("test-job")["status"], "pending")
        self.assertEqual(self.decisions, [])
        self.assertEqual(self.decide(company_id="general")[0], 303)
        self.assertEqual(self.reviews.get("test-job")["approved_route"]["drive_folder_id"],
                         "folder-general-001")

    def test_post_requires_exact_host_and_origin(self):
        body = urlencode({"job": "test-job", "revision": self.record["revision"], "company_id": "health", "action": "approve"})
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Origin": ""},
                        {"Origin": None},
                        {"Origin": "https://evil.example", "Referer": self.server.review_url},
                        {"Origin": "null", "Sec-Fetch-Site": "same-origin"},
                        {"Host": "localhost:" + str(self.server.server_address[1])}):
            self.assertEqual(self.request("POST", self.server.review_path + "decision", body, headers)[0], 403)
        self.assertEqual(self.reviews.get("test-job")["status"], "pending")
        self.assertEqual(self.decisions, [])

    def test_page_policy_preserves_origin_for_native_same_origin_form_post(self):
        status, headers, body = self.detail()
        self.assertEqual(status, 200)
        self.assertIn('<form method="post" action="' + self.server.review_path + "decision", body)
        # Native form navigation is not a CORS fetch. Fetch Standard 3.2 sets
        # its Origin to null under no-referrer, even for this same-origin POST:
        # https://fetch.spec.whatwg.org/#append-a-request-origin-header
        # Derive the request from the served policy instead of injecting a
        # trusted Origin unconditionally, which concealed the original bug.
        policy = headers["Referrer-Policy"]
        self.assertIn(policy, ("no-referrer", "strict-origin"))
        origin = "null" if policy == "no-referrer" else self.server.review_origin
        fields = {"job": "test-job", "revision": self.record["revision"],
                  "company_id": "health", "action": "approve"}
        status, response_headers, _ = self.request(
            "POST", self.server.review_path + "decision", urlencode(fields),
            {"Origin": origin, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate"})
        self.assertEqual(status, 303)
        self.assertEqual(response_headers["Location"].split("?", 1)[0], self.server.review_path)
        self.assertEqual(self.reviews.get("test-job")["status"], "approved")
        self.assertEqual(len(self.decisions), 1)

    def test_duplicate_origin_headers_cannot_approve_a_review(self):
        fields = {"job": "test-job", "revision": self.record["revision"],
                  "company_id": "health", "action": "approve"}
        body = urlencode(fields).encode()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.putrequest("POST", self.server.review_path + "decision", skip_host=True)
            connection.putheader("Host", self.server.review_host)
            connection.putheader("Origin", self.server.review_origin)
            connection.putheader("Origin", "https://evil.example")
            connection.putheader("Content-Type", "application/x-www-form-urlencoded")
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            connection.close()
        self.assertEqual(self.reviews.get("test-job")["status"], "pending")
        self.assertEqual(self.decisions, [])

    def test_approved_choice_is_persisted_before_callback_and_retry_is_safe(self):
        # Callback executes on the HTTP thread, so use a separate connection.
        def callback(record):
            reviews = RoutingReviews(self.state)
            try:
                self.assertEqual(reviews.get("test-job"), record)
            finally:
                reviews.close()
            self.decisions.append(record)
        self.server.on_decision = callback
        status, headers, _ = self.decide()
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"].split("?", 1)[0], self.server.review_path)
        self.assertEqual(self.reviews.get("test-job")["status"], "approved")
        self.assertEqual(self.decisions[0]["approved_route"]["drive_folder_id"], "folder-health-001")
        self.assertEqual(self.decide()[0], 303)
        self.assertEqual(self.decisions[0], self.decisions[1])
        self.assertIn("No meetings", self.request()[2])

    def test_keep_local_persists_without_destination(self):
        self.assertEqual(self.decide("skip", company_id="")[0], 303)
        saved = self.reviews.get("test-job")
        self.assertEqual(saved["status"], "skipped")
        self.assertIsNone(saved["approved_route"])
        self.assertEqual(self.decisions, [saved])

    def test_stale_revision_unknown_company_and_unknown_job_do_not_change_review(self):
        self.assertEqual(self.decide(revision="stale")[0], 409)
        self.assertEqual(self.decide(company_id="not-allowed")[0], 400)
        self.assertEqual(self.decide(job="../private")[0], 400)
        self.assertEqual(self.decide(job="unknown-job")[0], 400)
        self.assertEqual(self.reviews.get("test-job")["status"], "pending")

    def test_bounded_form_and_strict_fields(self):
        path = self.server.review_path + "decision"
        self.assertEqual(self.request("POST", path, "x" * (MAX_FORM_BYTES + 1))[0], 413)
        for body in ("job=a&job=b&revision=x&company_id=health&action=approve", "action=approve",
                     "job=a&revision=x&company_id=health&action=share"):
            self.assertEqual(self.request("POST", path, body)[0], 400)
        self.assertEqual(self.request("POST", path, "{}", {"Content-Type": "application/json"})[0], 415)

    def test_processing_retry_posts_only_exact_job_and_revision(self):
        self.server.processing_options = lambda: [{"title": "Synthetic <meeting>", "detail": "Offline & waiting",
            "job": "queued-job", "revision": "exact-revision", "retryable": True}]
        requests = []
        self.server.on_retry = lambda job, revision: requests.append((job, revision))
        status, _, body = self.detail("queued-job")
        self.assertEqual(status, 200)
        self.assertIn("Synthetic &lt;meeting&gt;", body)
        self.assertIn("Retry this meeting", body)
        fields = {"job": "queued-job", "revision": "exact-revision", "action": "retry"}
        self.assertEqual(self.request("POST", self.server.review_path + "decision", urlencode(fields))[0], 303)
        self.assertEqual(requests, [("queued-job", "exact-revision")])
        self.assertEqual(self.reviews.get("test-job")["status"], "pending")
        self.assertEqual(self.decisions, [])
        fields["company_id"] = "health"
        self.assertEqual(self.request("POST", self.server.review_path + "decision", urlencode(fields))[0], 400)

    def test_retry_failure_is_explained_without_saving_a_company_choice(self):
        def stale(*args):
            raise ValueError("Processing changed <reload>")
        self.server.on_retry = stale
        fields = {"job": "queued-job", "revision": "stale", "action": "retry"}
        status, _, body = self.request("POST", self.server.review_path + "decision", urlencode(fields))
        self.assertEqual(status, 409)
        self.assertIn("Processing changed &lt;reload&gt;", body)
        self.assertEqual(self.reviews.get("test-job")["status"], "pending")

    def test_notes_preview_is_bounded_and_symlinks_never_followed(self):
        notes = self.write_notes("a" * (MAX_NOTES_BYTES + 100))
        self.assertIn("[Preview truncated]", self.detail()[2])
        external = Path(self.temporary.name) / "private.md"
        external.write_text("PRIVATE OUTSIDE NOTES")
        notes.unlink()
        notes.symlink_to(external)
        self.assertNotIn("PRIVATE OUTSIDE NOTES", self.detail()[2])
        notes.unlink()
        notes.parent.rmdir()
        notes.parent.symlink_to(external.parent, target_is_directory=True)
        (external.parent / "notes.md").write_text("PRIVATE DIRECTORY NOTES")
        self.assertNotIn("PRIVATE DIRECTORY NOTES", self.detail()[2])

    def test_callback_failure_does_not_lose_saved_decision(self):
        def fail(record):
            raise RuntimeError("Synthetic callback failure")
        self.server.on_decision = fail
        status, _, body = self.decide()
        self.assertEqual(status, 200)
        self.assertIn("Your choice was saved", body)
        self.assertNotIn("Synthetic callback failure", body)
        self.assertEqual(self.reviews.get("test-job")["status"], "approved")

    def test_resume_is_shown_only_for_eligible_approved_reviews_and_uses_exact_revision(self):
        resumed = []
        self.server.resume_options = lambda record: ["archive", "publish"]
        self.server.on_resume = lambda record, stages: resumed.append((record, stages))
        self.assertNotIn("Resume waiting work", self.detail()[2])
        self.assertEqual(self.decide()[0], 303)
        saved = self.reviews.get("test-job")
        self.write_notes("# Synthetic saved notes\nUse these exact notes.")
        status, _, body = self.detail()
        self.assertEqual(status, 200)
        self.assertIn("Resume waiting work", body)
        self.assertIn("recording upload, notes upload", body)
        self.assertIn('name="stages" value="archive,publish"', body)
        self.assertIn("Use these exact notes.", body)
        self.assertNotIn("<script>", body)
        self.assertEqual(self.decide("resume")[0], 409)  # Previous, undecided revision.
        self.assertEqual(self.decide("resume", revision=saved["revision"], company_id="other")[0], 409)
        self.assertEqual(resumed, [])
        self.assertEqual(self.decide("resume", revision=saved["revision"])[0], 303)
        self.assertEqual(resumed, [(saved, ["archive", "publish"])])
        self.assertEqual(self.reviews.get("test-job"), saved)
        self.assertEqual(len(self.decisions), 1)
        self.server.resume_options = lambda record: []
        self.assertNotIn("Resume waiting work", self.detail()[2])

    def test_resume_never_overrides_keep_local_or_missing_review(self):
        resumed = []
        self.server.resume_options = lambda record: ["archive"]
        self.server.on_resume = lambda record, stages: resumed.append((record, stages))
        self.assertEqual(self.decide("resume")[0], 409)
        self.assertEqual(self.decide("skip", company_id="")[0], 303)
        saved = self.reviews.get("test-job")
        self.assertNotIn("Resume waiting work", self.detail()[2])
        self.assertEqual(self.decide("resume", revision=saved["revision"])[0], 409)
        self.assertEqual(self.decide("resume", job="missing", revision=saved["revision"])[0], 409)
        self.assertEqual(resumed, [])
        self.assertEqual(self.reviews.get("test-job"), saved)

    def test_resume_requires_a_bounded_exact_stage_list(self):
        resumed = []
        self.server.on_resume = lambda record, stages: resumed.append((record, stages))
        self.assertEqual(self.decide()[0], 303)
        saved = self.reviews.get("test-job")
        for stages in ("", "notes", "archive,archive", "archive,publish,route,notes"):
            self.assertEqual(self.decide("resume", revision=saved["revision"], stages=stages)[0], 400)
        self.assertEqual(resumed, [])

    def test_serve_review_times_out_without_opening_browser(self):
        with patch("quill_workflow.review_server.webbrowser.open") as browser:
            serve_review(self.state, open_browser=False, timeout_seconds=0.03)
            browser.assert_not_called()
        self.assertEqual(self.reviews.get("test-job")["status"], "pending")
        for timeout in (0, -1, float("inf"), True):
            with self.assertRaises(ValueError):
                serve_review(self.state, open_browser=False, timeout_seconds=timeout)


if __name__ == "__main__":
    unittest.main()
