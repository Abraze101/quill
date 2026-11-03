"""Exercise the real review entry point without recording, running connectors, or touching macOS Trash."""
import fcntl
import http.client
from html.parser import HTMLParser
import json
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.local_deletion import CAPABILITY, LEASE, LocalMeetingDeletion
from quill_workflow.review_server import _selection


class Forms(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.forms, self.fields = {}, {}
        self.feed(page)

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        if tag == "form":
            self.fields = {}
        if tag == "input" and attrs.get("type") == "hidden":
            self.fields[attrs["name"]] = attrs["value"]
        if tag == "button" and attrs.get("name") == "action":
            action = attrs["value"]
            self.forms[action] = dict(self.fields, action=action)


class CancelReviewEntryPointTests(unittest.TestCase):
    def test_cancel_active_transcription_from_real_form_and_then_delete_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state, config, recordings = root / "state", root / "workflow.json", root / "recordings"
            config.write_text(json.dumps({"recordings_root": str(recordings)}))
            for name in ("First sample", "Second sample"):
                session = recordings / name
                session.mkdir(parents=True)
                for track in ("mic.caf", "system.caf"):
                    (session / track).write_bytes((name + track).encode())
                (session / "meta.json").write_text(json.dumps({
                    "started": "2026-10-21T00:00:00Z", "ended": "2026-10-21T00:01:00Z",
                    "transcription_requested": True, "files": {"mic": "mic.caf", "system": "system.caf"}}))
                (session / CAPABILITY).write_text(json.dumps({"schema_version": 1, "shared_processing_lease": True}))
                (session / LEASE).touch(mode=0o600)
            # A malformed neighboring folder must not hide the valid meetings.
            corrupt = recordings / "Incomplete metadata"
            corrupt.mkdir()
            (corrupt / "meta.json").write_text(json.dumps({
                "started": 123, "ended": "2026-10-21T00:01:00Z", "transcription_requested": False}))
            first = recordings / "First sample"
            original = {p.name: p.read_bytes() for p in first.iterdir()}
            proc = subprocess.Popen([sys.executable, str(ROOT / "workflow.py"), "--config", str(config),
                                     "--state", str(state), "review", "--no-open", "--print-url", "--timeout", "30"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                readable, _, _ = select.select([proc.stdout], [], [], 10)
                address = proc.stdout.readline().strip() if readable else ""
                if not address:
                    status = proc.poll()
                    outcome = f"exited with status {status}" if status is not None else "did not announce a URL within 10 seconds"
                    if status is None:
                        proc.terminate()
                    _, error = proc.communicate(timeout=5)
                    self.fail(f"Review server {outcome}. stderr: {error[-2000:].strip() or '(empty)'}")
                url = urlsplit(address)
                self.assertEqual(url.hostname, "127.0.0.1")
                def request(path, form=None):
                    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
                    try:
                        if form is None:
                            conn.request("GET", path)
                        else:
                            conn.request("POST", path, urlencode(form), {
                                "Content-Type": "application/x-www-form-urlencoded", "Origin": "http://" + url.netloc})
                        response = conn.getresponse()
                        return response.status, response.getheader("Location"), response.read().decode()
                    finally:
                        conn.close()
                store = LocalMeetingDeletion(state, config, trash=lambda p: shutil.move(p, root / "test-trash"))
                try:
                    jobs = {item["title"]: item["job"] for item in store.cancellation_options()}
                    selected = lambda name: url.path + "?" + urlencode({"meeting": _selection(jobs[name])})
                    first_url, second_url = selected("First sample"), selected("Second sample")
                    with (first / LEASE).open("rb") as lease:
                        fcntl.flock(lease, fcntl.LOCK_SH)
                        status, _, page = request(first_url)
                        self.assertEqual(status, 200)
                        form = Forms(page).forms["cancel_processing"]
                        self.assertEqual(form["job"], jobs["First sample"])
                        status, redirect, _ = request(url.path + "decision", form)
                        self.assertEqual(status, 303)
                        self.assertIn("Stopping processing…", request(redirect)[2])
                    page = request(first_url)[2]
                    self.assertIn("Processing cancelled", page)
                    self.assertNotIn("cancel_processing", Forms(page).forms)
                    self.assertEqual({k: (first / k).read_bytes() for k in original}, original)
                    self.assertIn("cancel_processing", Forms(request(second_url)[2]).forms)
                    status, _, confirmation = request(url.path + "decision", Forms(page).forms["delete_prepare"])
                    self.assertEqual(status, 200)
                    form = Forms(confirmation).forms["delete_confirm"]
                    removed = store.confirm(form["job"], form["revision"], form["confirmation"])
                    self.assertEqual(removed["status"], "trashed")
                    moved = Path(removed["moves"][0]["trash_path"]) / removed["moves"][0]["member"]
                    self.assertEqual({k: (moved / k).read_bytes() for k in original}, original)
                    self.assertTrue((recordings / "Second sample" / "mic.caf").is_file())
                finally:
                    store.close()
            finally:
                proc.terminate()
                proc.communicate(timeout=5)
