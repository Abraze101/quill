"""Synthetic coverage for the processing center's read-only meeting index."""
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_review_library as fixtures
from quill_workflow.pipeline import atomic_json
from quill_workflow.review_library import _Sources, meeting_detail, meeting_index
from quill_workflow.review_server import _reviews_page, _selection, create_server


class ReviewIndexTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReviewLibraryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_list_uses_capture_metadata_and_cached_filing_receipts_without_opening_content(self):
        f = self.fixture
        f.metadata["meeting_context"] = {"provider": "google-meet", "key": "abc-defg-hij", "coverage": "visible_only",
                                         "participants": [{"name": "Jordan", "self": False}]}
        f.metadata["calendar_attendees"] = ["Invitee"]
        atomic_json(f.recording / "meta.json", f.metadata)
        opened = []
        read = _Sources.read

        def metadata_only(sources, relative, *args):
            opened.append(relative)
            self.assertEqual(relative, f"recordings/{f.job}/meta.json")
            return read(sources, relative, *args)

        with patch.object(_Sources, "read", metadata_only):
            row = meeting_index(f.state)[f.job]
        self.assertEqual(len(opened), 1)
        self.assertEqual(row["title"], "Dashboard review")
        self.assertEqual(row["participants"], ["Jordan"])
        self.assertEqual(row["participants_source"], "visible_in_call")
        self.assertEqual(row["display_date"], "10/21/26")
        self.assertEqual((row["company_id"], row["folder_name"]), ("example", "Example"))
        self.assertEqual(row["storage_status"], "filing_recorded")
        self.assertEqual(row["storage"]["document"], {"status": "recorded", "file_ids": []})
        self.assertNotIn("transcript_text", row)
        self.assertNotIn("notes_text", row)

    def test_completed_local_rename_precedes_historical_receipt_and_removes_collision_suffix(self):
        f = self.fixture
        destination = str(f.root / ("Jordan - Planning - 10-21-26 - " + f.job[:8]))
        with f.db:
            f.db.execute("INSERT INTO local_names (job,settings,destination,status,device,inode) "
                         "VALUES (?,'{}',?,'succeeded',1,1)",
                         (f.job, destination))
        self.assertEqual(meeting_index(f.state)[f.job]["title"], "Jordan - Planning")
        with f.db:
            f.db.execute("INSERT INTO local_title_labels VALUES (?,?,?)", (f.job, "New reviewed title", "2026-10-21"))
        self.assertEqual(meeting_index(f.state)[f.job]["title"], "New reviewed title")
        with f.db:
            f.db.execute("UPDATE local_names SET status='planned' WHERE job=?", (f.job,))
        self.assertEqual(meeting_index(f.state)[f.job]["title"], "Dashboard review")

    def test_invalid_receipt_shape_and_tombstones_cannot_make_a_filed_meeting(self):
        f = self.fixture
        damaged = dict(f.receipts["publish"], meeting_folder_id="x")
        f.task("publish", "succeeded", damaged)
        row = meeting_index(f.state)[f.job]
        self.assertEqual(row["storage_status"], "partial_filing_recorded")
        self.assertEqual(row["storage"]["document"]["status"], "unknown")
        with f.db:
            f.db.execute("INSERT INTO local_deletions VALUES (?, '{}')", (f.job,))
        with patch.object(_Sources, "read", side_effect=AssertionError("Deleted job opened")):
            self.assertEqual(meeting_index(f.state), {})

    def test_transcript_only_rows_are_listed_without_opening_text_and_can_be_selected(self):
        f = self.fixture
        source = {"segments": [{"speaker": "me", "start_ms": 0, "text": "Synthetic legacy speech."}]}
        jobs = ["captured", "attached", "empty", "missing", "linked-file", "linked-parent", "invalid-attachment", "removed"]
        with f.db:
            f.db.executemany("INSERT INTO jobs VALUES (?, '/no-original', 1)", [(job,) for job in jobs])
            f.db.execute("INSERT INTO local_deletions VALUES ('removed', '{}')")
        captured = f.state / "recordings"
        for job in ("captured", "removed", "invalid-attachment"):
            atomic_json(captured / job / "transcript.json", source)
        atomic_json(f.state / "results/attached/input/transcript.json", source)
        (f.state / "results/invalid-attachment/input").mkdir(parents=True)
        (f.state / "results/invalid-attachment/input/transcript.json").touch()
        (captured / "empty").mkdir()
        (captured / "empty/transcript.json").touch()
        outside = f.root / "unrelated"
        atomic_json(outside / "transcript.json", source)
        (captured / "linked-parent").symlink_to(outside)
        (captured / "linked-file").mkdir()
        (captured / "linked-file/transcript.json").symlink_to(outside / "transcript.json")
        server = create_server(f.state)
        self.addCleanup(server.server_close)
        read = _Sources.read
        def summary_read(sources, relative, *args):
            self.assertNotEqual(Path(relative).name, "transcript.json")
            return read(sources, relative, *args)
        with patch.object(_Sources, "read", summary_read):
            listing = _reviews_page(server)
        for job in jobs:
            with self.subTest(job=job):
                self.assertEqual(_selection(job) in listing, job in ("captured", "attached"))
        self.assertNotIn("Synthetic legacy speech", listing)
        detail = _reviews_page(server, selected=_selection("attached"), tab="transcript")
        self.assertIn("Synthetic legacy speech", detail)

    def test_index_reads_tasks_once_and_detail_queries_only_the_selected_job(self):
        f = self.fixture
        with f.db:
            f.db.executemany("INSERT INTO jobs VALUES (?, '/never-read', 1)",
                             [(f"unselected-{i}",) for i in range(30)])
        statements = []
        connect = sqlite3.connect

        def traced(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with patch("quill_workflow.review_library.sqlite3.connect", traced):
            rows = meeting_index(f.state)
            self.assertEqual(len(rows), 31)
            task_queries = [q for q in statements if " FROM tasks" in q]
            self.assertEqual(len(task_queries), 1)
            self.assertIn("stage IN", task_queries[0])
            self.assertIsNone(rows["unselected-0"]["title"])
            statements.clear()
            self.assertIsNotNone(meeting_detail(f.state, f.job))
        scoped = [q for q in statements if any(f" FROM {table}" in q for table in (
            "jobs ", "tasks", "local_title_labels", "local_deletions"))]
        self.assertEqual(len(scoped), 4)
        self.assertTrue(all(" WHERE " in q and f.job in q for q in scoped))
