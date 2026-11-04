"""Local plugin checks use synthetic state and never launch Quill or workers."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_review_library as fixtures


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "plugins/quill/scripts/quill.py"
spec = importlib.util.spec_from_file_location("quill_plugin_cli", SCRIPT)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class ChatGPTPluginTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReviewLibraryTests("runTest")
        self.fixture.setUp()
        self.root = self.fixture.root
        self.state = self.fixture.state
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        self.home_patch = patch.object(plugin.Path, "home", return_value=self.home)
        self.home_patch.start()
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        os.environ.pop("QUILL_REPO", None)

    def tearDown(self):
        self.env_patch.stop()
        self.home_patch.stop()
        self.fixture.tearDown()

    def invoke(self, *arguments, repo=REPO):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = plugin.main(["--repo", str(repo), "--state", str(self.state),
                                "--recording-state", str(self.state.parent), *arguments])
        return code, json.loads(output.getvalue())

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def files(self, root):
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def test_status_accepts_fresh_recording_and_only_allowlisted_saved_processing_fields(self):
        now = time.time()
        self.write(self.state.parent / "recording-status.json", {
            "version": 1, "timestamp": now, "phase": "paused", "deliberatelySkipped": []})
        self.write(self.state / "status.json", {
            "updated": now - 600, "processing": {"state": "running", "detail": "secret command"},
            "tasks": {"running": 1, "pending": 2, "auth_token": "private", "failed": True},
            "pending_reviews": 3, "active": {"title": "private title"}, "settings": {"token": "secret"}})
        code, result = self.invoke("status")
        self.assertEqual(code, 0)
        self.assertEqual(result["recording"], {"state": "paused"})
        processing = result["processing"]
        self.assertEqual(processing["source"], "saved_snapshot")
        self.assertGreaterEqual(processing["age_seconds"], 600)
        self.assertEqual(processing["tasks"], {"running": 1, "pending": 2})
        self.assertEqual(processing["pending_reviews"], 3)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    def test_status_never_calls_stale_recording_active(self):
        for timestamp in (time.time() - 30, time.time() + 30, "now", True):
            self.write(self.state.parent / "recording-status.json", {
                "version": 1, "timestamp": timestamp, "phase": "recording", "deliberatelySkipped": []})
            self.assertEqual(self.invoke("status")[1]["recording"], {"state": "unavailable"})

    def test_default_recording_heartbeat_is_independent_of_custom_workflow_state(self):
        self.write(self.home / ".local/state/quill/recording-status.json", {
            "version": 1, "timestamp": time.time(), "phase": "recording", "deliberatelySkipped": []})
        self.write(self.state.parent / "recording-status.json", {
            "version": 1, "timestamp": time.time(), "phase": "idle", "deliberatelySkipped": []})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = plugin.main(["--repo", str(REPO), "--state", str(self.state), "status"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["recording"], {"state": "recording"})
        self.assertEqual(self.invoke("status")[1]["recording"], {"state": "not-recording"})

    def test_bad_or_public_processing_snapshots_are_unavailable(self):
        path = self.state / "status.json"
        for saved in ([], {}, {"updated": time.time() + 10, "processing": {"state": "running"}},
                      {"updated": time.time(), "processing": {"state": "invented"}}):
            self.write(path, saved)
            self.assertFalse(self.invoke("status")[1]["processing"]["available"])
        self.write(path, {"updated": time.time(), "processing": {"state": "running"}})
        path.chmod(0o644)
        self.assertFalse(self.invoke("status")[1]["processing"]["available"])
        path.unlink()
        outside = self.root / "outside.json"
        self.write(outside, {"updated": time.time(), "processing": {"state": "running"}})
        path.symlink_to(outside)
        self.assertFalse(self.invoke("status")[1]["processing"]["available"])

    def test_meeting_list_omits_content_settings_receipts_and_local_paths(self):
        code, result = self.invoke("meetings")
        self.assertEqual(code, 0)
        self.assertEqual(result["total"], 1)
        meeting = result["meetings"][0]
        self.assertEqual(meeting["job"], self.fixture.job)
        self.assertEqual(meeting["storage_status"], "saved_in_drive")
        self.assertEqual(result["storage_evidence"], "saved_receipts_not_live_drive_check")
        for forbidden in ("transcript_text", "notes_text", "notes.json", "settings", "receipt", "local_folder", "input_sha256"):
            self.assertNotIn(forbidden, json.dumps(meeting))
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(self.invoke("meetings", "--query", "No such topic")[1]["total"], 0)
        self.assertEqual(self.invoke("meetings", "--offset", "1")[1]["meetings"], [])

    def test_meetings_sort_filter_and_page_whitelisted_metadata(self):
        entries = {
            "old": {"title": "Roadmap", "recorded_at": 1, "participants": ["Alex"]},
            "b-new": {"title": "Roadmap next", "recorded_at": 2},
            "a-new": {"title": "Roadmap today", "recorded_at": 2},
            "unrelated": {"title": "Dinner", "recorded_at": 3},
        }
        with patch.object(plugin.library(REPO), "meeting_library", return_value=entries):
            result = self.invoke("meetings", "--query", "roadmap", "--limit", "1", "--offset", "1")[1]
        self.assertEqual([entry["job"] for entry in result["meetings"]], ["b-new"])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["next_offset"], 2)

    def test_get_reads_exact_meeting_and_paginates_selected_content(self):
        detail = plugin.library(REPO).meeting_detail(self.state, self.fixture.job)
        with patch.object(plugin.library(REPO), "meeting_library", side_effect=AssertionError("Do not read full library")):
            code, result = self.invoke("get", self.fixture.job, "--content", "notes", "--limit", "7", "--offset", "2")
        self.assertEqual(code, 0)
        self.assertEqual(result["text"], detail["notes_text"][2:9])
        self.assertEqual(result["next_offset"], 9)
        self.assertEqual(result["total_characters"], len(detail["notes_text"]))
        transcript = self.invoke("get", self.fixture.job, "--content", "transcript")[1]
        self.assertEqual(transcript["text"], detail["transcript_text"])
        self.assertIsNone(transcript["next_offset"])
        self.assertFalse(transcript["preview_shortened"])
        with patch.object(plugin.library(REPO), "MAX_NOTES_CHARACTERS", 20):
            shortened = self.invoke("get", self.fixture.job, "--limit", "2")[1]
        self.assertTrue(shortened["preview_shortened"])
        self.assertNotIn("preview shortened", shortened["text"])

    def test_get_unverified_notes_and_deleted_ids_do_not_expose_content(self):
        (self.fixture.result / "notes/provenance.json").unlink()
        result = self.invoke("get", self.fixture.job)[1]
        self.assertFalse(result["available"])
        self.assertIsNone(result["text"])
        self.fixture.db.execute("INSERT INTO local_deletions VALUES (?,?)", (self.fixture.job, '{}'))
        self.fixture.db.commit()
        code, result = self.invoke("get", self.fixture.job)
        self.assertEqual(code, 1)
        self.assertEqual(set(result), {"error"})
        self.assertEqual(self.invoke("get", "../private")[0], 1)

    def test_read_commands_do_not_change_state_or_create_config(self):
        before = self.files(self.state)
        for args in (("doctor",), ("status",), ("meetings",), ("get", self.fixture.job)):
            self.assertEqual(self.invoke(*args)[0], 0)
        self.assertEqual(self.files(self.state), before)
        self.assertFalse((self.home / ".config").exists())
        self.assertTrue(sys.dont_write_bytecode)

    def test_missing_state_read_does_not_create_data(self):
        missing = self.root / "missing state"
        with contextlib.redirect_stdout(io.StringIO()):
            code = plugin.main(["--repo", str(REPO), "--state", str(missing), "meetings"])
        self.assertEqual(code, 0)
        self.assertFalse(missing.exists())

    def test_repo_selection_precedence_and_paths_with_spaces(self):
        alternate = self.root / "alternate checkout with spaces"
        integration = alternate / "integrations/meeting-workflow"
        (integration / "quill_workflow").mkdir(parents=True)
        for name in ("workflow.py", "native-host.py", "quill_workflow/review_library.py"):
            (integration / name).touch()
        self.write(plugin._settings_path(), {"repo_root": str(alternate)})
        self.assertEqual(plugin.resolve_repo(), alternate)
        os.environ["QUILL_REPO"] = str(REPO)
        self.assertEqual(plugin.resolve_repo(), REPO)
        self.assertEqual(plugin.resolve_repo(str(alternate)), alternate)
        with self.assertRaises(plugin.PluginError):
            plugin.resolve_repo(str(self.root / "not a checkout"))

    def test_old_checkout_reports_compatibility_error_instead_of_traceback(self):
        with patch.object(plugin.library(REPO), "meeting_detail", None):
            code, result = self.invoke("doctor")
            self.assertEqual(code, 0)
            self.assertFalse(result["reader_compatible"])
            code, result = self.invoke("get", self.fixture.job)
            self.assertEqual(code, 1)
            self.assertEqual(set(result), {"error"})
            self.assertIn("needs an update", result["error"])

    def test_old_native_host_returns_json_update_error_for_status(self):
        for old_host in (SimpleNamespace(), SimpleNamespace(recording_status=None)):
            with patch.object(plugin.importlib.util, "module_from_spec", return_value=old_host), \
                    patch("importlib.machinery.SourceFileLoader.exec_module"):
                code, result = self.invoke("status")
            self.assertEqual(code, 1)
            self.assertEqual(set(result), {"error"})
            self.assertIn("needs an update for recording status", result["error"])

    def test_setup_preserves_other_settings_and_writes_private_atomic_file(self):
        settings = plugin._settings_path()
        self.write(settings, {"future_option": {"value": "keep me"}})
        unrelated = settings.parent / "config.json"
        unrelated.write_text("Unrelated native configuration")
        code, result = self.invoke("setup")
        self.assertEqual(code, 0)
        self.assertTrue(result["configured"])
        self.assertEqual(json.loads(settings.read_text()), {"future_option": {"value": "keep me"}, "repo_root": str(REPO)})
        self.assertEqual(stat.S_IMODE(settings.stat().st_mode), 0o600)
        self.assertEqual(unrelated.read_text(), "Unrelated native configuration")
        self.assertEqual(sorted(path.name for path in settings.parent.iterdir()), ["chatgpt-plugin.json", "config.json"])

    def test_setup_refuses_invalid_settings_instead_of_replacing_them(self):
        settings = plugin._settings_path()
        self.write(settings, {"keep": "private"})
        settings.write_text('{"repo_root": "a", "repo_root": "b"}')
        before = settings.read_bytes()
        self.assertEqual(self.invoke("setup")[0], 1)
        self.assertEqual(settings.read_bytes(), before)

    def test_review_executes_existing_workflow_with_argument_list_and_spaces(self):
        config = self.root / "config with spaces.json"
        with patch.object(plugin.os, "execv") as execute, contextlib.redirect_stdout(io.StringIO()):
            code = plugin.main(["--repo", str(REPO), "--state", str(self.state), "--config", str(config), "review"])
        self.assertEqual(code, 0)
        execute.assert_called_once_with(sys.executable, [sys.executable,
            str(REPO / "integrations/meeting-workflow/workflow.py"), "--config", str(config),
            "--state", str(self.state), "review", "--no-open", "--print-url", "--timeout", "1800"])

    def test_invalid_options_produce_bounded_json_errors(self):
        for args in (("meetings", "--limit", "100000"), ("get", "x", "--offset", "-1"),
                     ("meetings", "--query", "a" * 161), ("bogus",), ("meetings", "--limit", "abc")):
            code, result = self.invoke(*args)
            self.assertEqual(code, 1)
            self.assertEqual(set(result), {"error"})
            self.assertLess(len(result["error"]), 250)


if __name__ == "__main__":
    unittest.main()
