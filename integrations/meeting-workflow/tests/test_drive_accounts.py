"""Account setup uses synthetic profiles/folders and never connects to Drive."""
import copy
from contextlib import redirect_stdout
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import workflow
from quill_workflow.drive_accounts import list_accounts, require_verified_account, select_account
from quill_workflow.drive_upload import DOCUMENT_MIME, FOLDER_MIME, UploadNeedsReview
from quill_workflow.codex_drive_transport import DriveOperationError
from quill_workflow.pipeline import NeedsConfiguration, atomic_json


class FakeTransport:
    def __init__(self, fixture, link):
        self.fixture, self.link = fixture, link

    def discover_accounts(self):
        self.fixture.calls.append((self.link, "discover_accounts", {}))
        return copy.deepcopy(self.fixture.accounts)

    def invoke(self, tool, arguments):
        self.fixture.calls.append((self.link, tool, arguments))
        if self.fixture.on_call:
            self.fixture.on_call(tool)
        if tool == "get_profile":
            return copy.deepcopy(self.fixture.profile)
        if tool == "get_file_metadata":
            return copy.deepcopy(self.fixture.metadata[arguments["fileId"]])
        if tool == "search":
            if self.fixture.unavailable_folder:
                return {"results": []}
            query = arguments["special_filter_query_str"]
            if "'root' in parents" in query:
                return {"results": [{"id": "meetings_root_123"}]}
            return {"results": [{"id": identity} for identity, item in self.fixture.metadata.items()
                                if "name = '" + item["title"] + "'" in query
                                and "'" + item["parent_ids"][0] + "' in parents" in query
                                and item["mime_type"] == FOLDER_MIME]}
        raise AssertionError("Account setup must only read profiles and known metadata")


class DriveAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "workflow.json"
        self.state = self.root / "state"
        self.state.mkdir()
        self.original_db = self.state / "jobs.sqlite3"
        self.original_db.write_bytes(b"unchanged queue sentinel")
        self.journal = self.state / "existing-journal.json"
        self.journal.write_bytes(b"unchanged upload journal sentinel")
        self.accounts = [{"link_id": "link_selected", "profile_id": "profile_123",
                          "email": "owner@example.test", "name": "Owner"}]
        self.profile = {"id": "profile_123", "email": "Owner@example.test", "name": "Owner"}
        stage = {"enabled": False, "type": "codex_drive", "requires_route": True,
                 "meetings_root_id": "meetings_root_123", "codex": "/fixture/codex",
                 "text_format": "native_template_google_doc_v1", "timeout_seconds": 90}
        self.config = {"custom": {"preserve": [1, 2, 3]}, "notes": {"enabled": True},
                       "archive": dict(stage), "publish": dict(stage, enabled=True,
                           template_document_id="template_doc_123", template_sha256="a" * 64),
                       "route": {"enabled": True, "companies": [{"id": "example", "name": "Example",
                                  "drive_folder_id": "category_folder_123"}]}}
        self.metadata = {
            "meetings_root_123": self.item("meetings_root_123", FOLDER_MIME, "drive_root_123"),
            "category_folder_123": self.item("category_folder_123", FOLDER_MIME, "meetings_root_123"),
            "template_doc_123": self.item("template_doc_123", DOCUMENT_MIME, "template_folder_123"),
        }
        self.metadata["category_folder_123"]["title"] = "Example"
        self.unavailable_folder = False
        self.calls, self.options = [], []
        self.on_call = None
        self.auth_calls = []
        self.auth_email = "owner@example.test"
        self.write()

    @staticmethod
    def item(identity, mime, parent):
        return {"id": identity, "mime_type": mime, "title": identity,
                "parent_ids": [parent], "shared": False, "current_user_can_share": True,
                "drive_id": None, "permissions": [
                    {"role": "owner", "type": "user", "emailAddress": "owner@example.test"}]}

    def factory(self, executable, **options):
        self.options.append((executable, options))
        return FakeTransport(self, options["account_link_id"])

    def auth_factory(self, filename):
        self.auth_calls.append(filename)
        outer = self
        class Auth:
            def preflight(self):
                return {"email": outer.auth_email}
        return Auth()

    def write(self):
        atomic_json(self.config_path, self.config)

    def revision(self):
        return hashlib.sha256(self.config_path.read_bytes()).hexdigest()

    def select(self, **kwargs):
        return select_account(self.config_path, self.state, kwargs.pop("link_id", "link_selected"),
            kwargs.pop("revision", self.revision()), transport_factory=self.factory,
            expected_profile_id=kwargs.pop("expected_profile_id", "profile_123"),
            expected_email=kwargs.pop("expected_email", "owner@example.test"),
            auth_factory=self.auth_factory, clock=lambda: 1234567890, **kwargs)

    def assert_unchanged_state(self):
        self.assertEqual(self.original_db.read_bytes(), b"unchanged queue sentinel")
        self.assertEqual(self.journal.read_bytes(), b"unchanged upload journal sentinel")

    def rejected(self, pattern, error_type=NeedsConfiguration, **kwargs):
        before = self.config_path.read_bytes()
        with self.assertRaisesRegex(error_type, pattern):
            self.select(**kwargs)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assert_unchanged_state()

    def test_single_connected_account_is_offered_but_never_selected_automatically(self):
        before = self.config_path.read_bytes()
        result = list_accounts(self.config_path, transport_factory=self.factory)
        self.assertEqual(result, {"accounts": self.accounts, "revision": self.revision(), "selected_link_id": None})
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(self.calls, [(None, "discover_accounts", {})])
        self.assert_unchanged_state()

    def test_listing_works_before_configuration_exists_without_creating_state(self):
        self.config_path.unlink()
        result = list_accounts(self.config_path, transport_factory=self.factory)
        self.assertIsNone(result["selected_link_id"])
        self.assertEqual(result["revision"], hashlib.sha256(b"").hexdigest())
        self.assertFalse(self.config_path.exists())
        self.assert_unchanged_state()

    def test_listing_preserves_existing_explicit_selection(self):
        for stage in ("archive", "publish"):
            self.config[stage]["account_link_id"] = "link_selected"
        self.write()
        self.assertEqual(list_accounts(self.config_path, transport_factory=self.factory)["selected_link_id"], "link_selected")
        self.config["archive"]["account_link_id"] = "link_other"
        self.write()
        self.assertIsNone(list_accounts(self.config_path, transport_factory=self.factory)["selected_link_id"])

    def test_verified_selection_preserves_flags_unrelated_config_and_queued_work(self):
        original = copy.deepcopy(self.config)
        record = self.select()
        expected_record = {"link_id": "link_selected", "email": "Owner@example.test", "profile_id": "profile_123",
                           "verified_at": 1234567890, "meetings_root_id": "meetings_root_123"}
        self.assertEqual(record, expected_record)
        saved = json.loads(self.config_path.read_bytes())
        expected = copy.deepcopy(original)
        expected["drive_account"] = record
        for stage in ("archive", "publish"):
            expected[stage]["account_link_id"] = "link_selected"
        self.assertEqual(saved, expected)
        self.assertEqual(require_verified_account(saved), record)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        self.assert_unchanged_state()
        self.assertEqual({arguments["fileId"] for _link, tool, arguments in self.calls if tool == "get_file_metadata"},
                         set(self.metadata))
        self.assertTrue(all(link == "link_selected" for link, tool, _args in self.calls if tool != "discover_accounts"))
        self.assertEqual(self.auth_calls, [])

    def test_recording_upload_account_must_match_selected_owner(self):
        self.config["archive"]["large_files"] = {"type": "google_drive_resumable", "credentials_file": "/fixture/private.json"}
        self.write()
        self.auth_email = "another@example.test"
        self.rejected("different Google account")
        self.assertEqual(self.auth_calls, ["/fixture/private.json"])
        self.auth_email = "owner@example.test"
        self.assertEqual(self.select()["link_id"], "link_selected")

    def test_invalid_or_removed_account_selection_does_not_write(self):
        for value in (None, "", "profile_123", "link_" + "x" * 201):
            with self.subTest(link=value):
                self.rejected("Choose a connected", link_id=value)
        self.rejected("no longer connected", link_id="link_missing")

    def test_changed_profile_id_or_email_is_rejected(self):
        self.profile["id"] = "changed_profile"
        self.rejected("connected Google account changed")
        self.profile["id"] = "profile_123"
        self.profile["email"] = "another@example.test"
        self.rejected("connected Google account changed")

    def test_same_link_reauthorized_after_display_cannot_switch_selected_identity(self):
        self.accounts[0].update(profile_id="new_profile_123", email="new@example.test")
        self.profile.update(id="new_profile_123", email="new@example.test")
        self.rejected("selected Google account details changed")
        self.assertFalse(any(tool == "get_file_metadata" for _link, tool, _arguments in self.calls))

    def test_different_root_owner_or_shared_destination_is_rejected(self):
        self.metadata["meetings_root_123"]["permissions"][0]["emailAddress"] = "another@example.test"
        self.rejected("Choose the folder's owner")
        self.metadata["meetings_root_123"]["permissions"][0]["emailAddress"] = "owner@example.test"
        self.metadata["meetings_root_123"]["shared"] = True
        self.rejected("configured private Meetings folder")

    def test_inaccessible_root_has_an_actionable_error_without_raw_connector_text(self):
        def fail(tool):
            if tool == "get_file_metadata":
                raise DriveOperationError("raw remote error sentinel")
        self.on_call = fail
        self.rejected("Choose the folder's owner")

    def test_category_parent_and_template_privacy_are_verified(self):
        self.metadata["category_folder_123"]["parent_ids"] = ["different_root_123"]
        self.rejected("outside its approved parent", UploadNeedsReview)
        self.metadata["category_folder_123"]["parent_ids"] = ["meetings_root_123"]
        self.metadata["template_doc_123"]["permissions"].append({"type": "anyone", "role": "reader"})
        self.rejected("owner-only", UploadNeedsReview)

    def add_nested_category(self):
        leaf = "nested_sales_folder_123"
        self.metadata[leaf] = self.item(leaf, FOLDER_MIME, "category_folder_123")
        self.metadata[leaf]["title"] = "Sales Meetings"
        self.config["route"]["companies"].append({"id": "example-sales", "name": "Example / Sales Meetings",
            "drive_folder_id": leaf, "drive_folder_path": [
                {"name": "Example", "drive_folder_id": "category_folder_123"},
                {"name": "Sales Meetings", "drive_folder_id": leaf}]})
        self.write()
        return leaf

    def test_account_selection_verifies_the_entire_nested_folder_path(self):
        leaf = self.add_nested_category()
        original = copy.deepcopy(self.config)
        self.select()
        saved = json.loads(self.config_path.read_bytes())
        self.assertEqual(saved["route"], original["route"])
        self.assertIn(leaf, {arguments["fileId"] for _link, tool, arguments in self.calls if tool == "get_file_metadata"})
        self.assert_unchanged_state()

    def test_account_verification_allows_only_the_approved_sales_viewers(self):
        leaf = self.add_nested_category()
        self.config["route"]["companies"][-1]["drive_reader_emails"] = ["sales@example.test"]
        self.metadata[leaf]["permissions"].append({"type": "user", "role": "reader", "emailAddress": "Sales@example.test"})
        self.metadata[leaf]["shared"] = True
        self.write()
        self.select()
        self.metadata[leaf]["permissions"][-1]["role"] = "writer"
        self.rejected("approved viewers", UploadNeedsReview)

    def test_nested_leaf_must_be_private_and_under_its_configured_company(self):
        leaf = self.add_nested_category()
        self.metadata[leaf]["shared"] = True
        self.rejected("owner-only", UploadNeedsReview)
        self.metadata[leaf]["shared"] = False
        self.metadata[leaf]["parent_ids"] = ["unapproved_company_123"]
        self.rejected("outside its approved parent", UploadNeedsReview)

    def test_unavailable_root_or_renamed_category_cannot_be_verified(self):
        self.unavailable_folder = True
        self.rejected("Choose the folder's owner")
        self.unavailable_folder = False
        self.metadata["category_folder_123"]["title"] = "Unexpected folder name"
        self.rejected("destination name changed", UploadNeedsReview)

    def test_missing_or_conflicting_roots_require_configuration(self):
        self.config["archive"]["meetings_root_id"] = "different_root_123"
        self.write()
        self.rejected("same private Meetings folder")
        self.config["archive"].pop("meetings_root_id")
        self.write()
        self.rejected("Finish the Meetings folder configuration")
        self.assertEqual(self.calls, [])

    def test_busy_worker_is_not_interrupted_and_settings_stay_unchanged(self):
        with (self.state / "worker.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.rejected("An upload is running")
        self.assertEqual(self.calls, [])

    def test_stale_revision_rejected_before_any_remote_read(self):
        self.rejected("settings changed", revision="a" * 64)
        self.assertEqual(self.calls, [])

    def test_settings_changed_during_validation_are_preserved(self):
        def change(tool):
            if tool == "get_profile":
                self.config["archive"]["enabled"] = True
                self.write()
        self.on_call = change
        with self.assertRaisesRegex(NeedsConfiguration, "settings changed"):
            self.select()
        self.assertEqual(json.loads(self.config_path.read_bytes()), self.config)
        self.assert_unchanged_state()

    def test_competing_settings_writer_prevents_save(self):
        with self.config_path.with_name("workflow.json.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.rejected("Settings are being saved")

    def test_symbolic_config_is_rejected_without_following_it(self):
        original = self.root / "actual.json"
        self.config_path.rename(original)
        self.config_path.symlink_to(original)
        before = original.read_bytes()
        with self.assertRaisesRegex(NeedsConfiguration, "readable regular file"):
            list_accounts(self.config_path, transport_factory=self.factory)
        self.assertEqual(original.read_bytes(), before)

    def test_activation_gate_requires_matching_verified_account_and_root(self):
        with self.assertRaisesRegex(NeedsConfiguration, "Choose and verify"):
            require_verified_account(self.config)
        self.select()
        selected = json.loads(self.config_path.read_bytes())
        frozen = copy.deepcopy(selected["publish"])
        self.assertEqual(require_verified_account(selected, frozen)["link_id"], "link_selected")
        for changes in ({"account_link_id": "link_other"}, {"meetings_root_id": "other_root_123"}, {"account_link_id": None}):
            with self.subTest(changes=changes), self.assertRaisesRegex(NeedsConfiguration, "changed"):
                require_verified_account(selected, dict(frozen, **changes))
        for key, value in (("verified_at", True), ("verified_at", float("nan")), ("email", "bad\n@example.test"),
                           ("profile_id", "bad profile"), ("link_id", "notalink")):
            changed = copy.deepcopy(selected)
            changed["drive_account"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(NeedsConfiguration, "Choose and verify"):
                require_verified_account(changed)

    def test_cli_account_listing_never_opens_queue(self):
        stdout = io.StringIO()
        result = {"accounts": self.accounts, "revision": "a" * 64, "selected_link_id": None}
        with patch("quill_workflow.drive_accounts.list_accounts", return_value=result), \
                patch.object(workflow, "Queue", side_effect=AssertionError("must not open queue")), redirect_stdout(stdout):
            code = workflow.main(["--config", str(self.config_path), "drive-accounts"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), result)

    def test_cli_selection_errors_are_json_without_tracebacks_or_queue_access(self):
        stdout = io.StringIO()
        with patch("quill_workflow.drive_accounts.select_account", side_effect=NeedsConfiguration("Choose the folder owner.")), \
                patch.object(workflow, "Queue", side_effect=AssertionError("must not open queue")), redirect_stdout(stdout):
            code = workflow.main(["drive-select", "--link-id", "link_selected", "--revision", "a" * 64,
                                  "--profile-id", "profile_123", "--email", "owner@example.test"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), {"error": "Choose the folder owner."})


if __name__ == "__main__":
    unittest.main()
