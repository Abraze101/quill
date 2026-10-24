"""Exact reader-only connector calls; synthetic app-server, no live sharing."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.codex_sharing_transport import (
    CodexSharingTransport, command, recipient_email, validate_arguments, _SharingRPC,
)
from quill_workflow import codex_drive_transport as private
import test_codex_drive_transport as fixtures


class SharingTransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CodexDriveTransportTests()
        self.fixture.setUp()
        self.arguments = {"url": "https://docs.google.com/document/d/notes_copy_123/edit",
                          "user_email": "max@example.test", "permission": "reader", "anyone_at_company": False}

    def tearDown(self):
        self.fixture.tearDown()

    def test_single_exact_share_rpc_and_no_model_turn(self):
        result = CodexSharingTransport(executable=self.fixture.fake(native={"success": True})).invoke("share_file", self.arguments)
        self.assertTrue(result["success"])
        messages = self.fixture.requests()
        self.assertEqual([m["method"] for m in messages], ["initialize", "initialized", "thread/start", "mcpServer/tool/call"])
        self.assertEqual(messages[-1]["params"]["tool"], "google_drive.share_file")
        self.assertEqual(messages[-1]["params"]["arguments"], self.arguments)

    def test_private_filing_transport_still_rejects_sharing(self):
        with self.assertRaises(private.DriveProtocolError):
            private.validate_arguments("share_file", self.arguments)
        with self.assertRaises(private.DriveProtocolError):
            private.command("codex", self.fixture.root, "share_file")

    def test_command_enables_only_share_without_weakening_private_defaults(self):
        argv = command("codex", self.fixture.root)
        self.assertIn(f"apps.{private.CONNECTOR}.tools.share_file.enabled=true", argv)
        self.assertIn(f'apps.{private.CONNECTOR}.tools.share_file.approval_mode="approve"', argv)
        self.assertNotIn(f"apps.{private.CONNECTOR}.tools.get_file_metadata.enabled=true", argv)
        self.assertIn("apps._default.enabled=false", argv)
        self.assertIn("apps._default.destructive_enabled=false", argv)

    def test_writer_owner_commenter_domain_folder_and_extra_fields_rejected(self):
        changes = [{"permission": role} for role in ("writer", "owner", "commenter")]
        changes += [{"anyone_at_company": True}, {"show_in_search": False}, {"user_email": None},
            {"url": "https://drive.google.com/drive/folders/folder_123456"}, {"url": "https://example.test/d/notes_copy_123/edit"}]
        for fields in changes:
            with self.subTest(fields=fields), self.assertRaises(private.DriveProtocolError):
                validate_arguments("share_file", dict(self.arguments, **fields))

    def test_transport_cannot_upload_delete_or_create_unbounded_documents(self):
        for tool in ("upload_file", "import_document", "create_document", "delete_file", "create_folder", "search"):
            with self.subTest(tool=tool), self.assertRaises(private.DriveProtocolError):
                validate_arguments(tool, {})

    def test_emails_require_explicit_individual_address(self):
        self.assertEqual(recipient_email("Max@Example.test"), "max@example.test")
        for value in ("Max", "Max <max@example.test>", "max@example.test,other@example.test", "max@example.test\n", "group@", "a..b@example.test", "a@example..test"):
            with self.subTest(value=value), self.assertRaises(private.DriveProtocolError):
                recipient_email(value)

    def test_lost_share_response_is_unknown_and_never_retried_by_transport(self):
        executable = self.fixture.fake(call="sys.exit(0)")
        with self.assertRaises(private.DriveTransportError) as caught:
            CodexSharingTransport(executable=executable).invoke("share_file", self.arguments)
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(len([m for m in self.fixture.requests() if m["method"] == "mcpServer/tool/call"]), 1)

    def test_approval_request_not_answered(self):
        executable = self.fixture.fake(call="send({'id': 'approval', 'method': 'mcpServer/elicitation/request', 'params': {}})")
        with self.assertRaises(private.DriveOperationError) as caught:
            CodexSharingTransport(executable=executable).invoke("share_file", self.arguments)
        self.assertTrue(caught.exception.outcome_unknown)

    def test_duplicate_or_invalid_response_id_rejected(self):
        for identity in (3.0, True):
            rpc = _SharingRPC(self.fixture.root, "share_file", self.arguments)
            rpc.expected_id = 3
            with self.assertRaises(private.DriveProtocolError):
                rpc.consume(json.dumps({"id": identity, "result": {"structuredContent": {"success": True}}}))


if __name__ == "__main__":
    unittest.main()
