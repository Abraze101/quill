"""Notes-only sharing checks. All documents, permissions, and sends are synthetic."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow import native_template as native
from quill_workflow.drive_upload import DOCUMENT_MIME, FOLDER_MIME, native_template_sha256
from quill_workflow.sharing_drive import SharingDrive, SharingNeedsReview, notes_plan, verify_notes, _snapshot
from test_drive_upload import FakeDrive, SimulatedTransportFailure
from test_native_template import template_document, filled_document, _paragraphs
import test_native_template as native_fixtures


class SharingFake(FakeDrive):
    TEMPLATE = "blank_template_123"
    PRIVATE = "private_combined_123"
    MY_DRIVE = "my_drive_root_123"

    def __init__(self, snapshot, journal):
        super().__init__()
        self.snapshot, self.journal = snapshot, journal
        self.add(self.MY_DRIVE, "My Drive", None, FOLDER_MIME)
        self.items[self.MY_DRIVE]["parent_ids"] = None
        self.add(self.TEMPLATE, "Blank approved template", self.ROOT, DOCUMENT_MIME)
        self.add(self.PRIVATE, "Private meeting", self.COMPANY, DOCUMENT_MIME)
        self.documents = {self.TEMPLATE: template_document()}
        self.documents[self.TEMPLATE]["documentId"] = self.TEMPLATE
        self.fail_copy = self.fail_edit = self.fail_share = None
        self.permission_read_override = None

    def __call__(self, tool, arguments):
        args = copy.deepcopy(arguments)
        if tool == "get_file_metadata":
            if args["fileId"] == "root":
                self.calls.append((tool, args))
                return copy.deepcopy(self.items[self.MY_DRIVE])
            item = super().__call__(tool, args)
            if self.permission_read_override and args["fileId"].startswith("shared_notes_"):
                self.permission_read_override(item)
            return item
        self.calls.append((tool, args))
        if tool == "get_document":
            return copy.deepcopy(self.documents[args["document_id"]])
        if tool == "copy_file":
            assert self.journal["copy"]["state"] == "dispatched"
            assert args["url"].endswith("/" + self.TEMPLATE + "/edit")
            self.counter += 1
            identity = "shared_notes_" + str(self.counter)
            self.add(identity, args["new_title"], args["parent_folder"], DOCUMENT_MIME)
            self.documents[identity] = copy.deepcopy(self.documents[self.TEMPLATE])
            self.documents[identity].update(documentId=identity, revisionId="blank", title=args["new_title"])
            if self.fail_copy:
                raise SimulatedTransportFailure()
            return {"success": True, "id": identity}
        if tool == "batch_update_document":
            assert self.journal["fill"]["state"] == "dispatched"
            identity = args["document_id"]
            blank = self.documents[identity]
            assert args["write_control"] == {"requiredRevisionId": blank["revisionId"]}
            if self.fail_edit == "before":
                raise SimulatedTransportFailure()
            payload = _snapshot(self.snapshot)["notes_payload"]
            plan = notes_plan(blank, payload)
            assert args["requests"] == plan["requests"]
            # Model Docs' native paragraph expansion, then the validated tail
            # deletion. No source transcript ever reaches a request.
            result = filled_document(payload, blank)
            result["tabs"][0]["body"]["content"] = [entry for entry in result["tabs"][0]["body"]["content"]
                if entry.get("endIndex", 0) <= native._utf16(plan["text"]) + 1]
            self.documents[identity] = result
            if self.fail_edit == "after":
                raise SimulatedTransportFailure()
            return {"documentId": identity, "replies": []}
        if tool == "share_file":
            email = args["user_email"]
            assert self.journal["share"]["recipients"][email]["state"] == "dispatched"
            identity = args["url"].split("/d/")[1].split("/")[0]
            assert identity not in {self.PRIVATE, self.TEMPLATE}
            assert args["permission"] == "reader" and args["anyone_at_company"] is False
            if self.fail_share == "before":
                raise SimulatedTransportFailure()
            self.items[identity]["permissions"].append({"id": "permission-" + email,
                "type": "user", "role": "reader", "emailAddress": email})
            self.items[identity]["shared"] = True
            if self.fail_share == "after":
                raise SimulatedTransportFailure()
            return {"success": True}
        raise AssertionError(tool)

    @property
    def sends(self):
        return [args for tool, args in self.calls if tool == "share_file"]


class SharingDriveTests(unittest.TestCase):
    def setUp(self):
        native_fixture = native_fixtures.NativeTemplateTests()
        native_fixture.setUp()
        self.snapshot = {"title": "Portal planning - Max - 9/14/26 - Shared notes",
            "owner": FakeDrive.OWNER, "template_document_id": SharingFake.TEMPLATE,
            "private_document_id": SharingFake.PRIVATE, "destination_folder_id": FakeDrive.COMPANY,
            "notes_payload": native_fixture.payload()}
        self.journal, self.saves = {}, []
        self.drive = SharingFake(self.snapshot, self.journal)
        self.snapshot["template_sha256"] = native_template_sha256(self.drive.documents[SharingFake.TEMPLATE])
        self.adapter = SharingDrive(self.drive)
        self.guard = lambda: None

    def save(self):
        self.saves.append(copy.deepcopy(self.journal))

    def prepare(self):
        return self.adapter.prepare(self.snapshot, self.journal, self.save, self.guard)

    def share(self, draft, recipients=("max@example.test",)):
        return self.adapter.share(self.snapshot, draft, recipients, self.journal, self.save, self.guard)

    def test_prepare_native_notes_only_idempotent_never_copies_private_document(self):
        draft = self.prepare()
        self.assertEqual(draft, self.prepare())
        document = self.drive.documents[draft["document_id"]]
        text = native.document_text(document)
        self.assertIn("Discussed the portal.", text)
        self.assertNotIn("00:00", text)
        self.assertNotIn("Hi Max", text)
        self.assertNotIn("Not included.", text)
        self.assertNotIn("{{QUILL_", text)
        self.assertEqual(len([1 for tool, _ in self.drive.calls if tool == "copy_file"]), 1)
        self.assertFalse(self.drive.sends)
        self.assertEqual(len(self.drive.items[draft["document_id"]]["permissions"]), 1)

    def test_transcript_heading_removed_with_placeholder(self):
        template = self.drive.documents[SharingFake.TEMPLATE]
        content = template["tabs"][0]["body"]["content"]
        heading = copy.deepcopy(content[-1])
        heading["paragraph"]["elements"][0]["textRun"]["content"] = "Transcript\n"
        content.insert(-1, heading)
        lines = [("".join(e["textRun"]["content"] for e in p["paragraph"]["elements"]),
                  p["paragraph"]["paragraphStyle"], p["paragraph"].get("bullet"), None)
                 for p in content if "paragraph" in p]
        template["tabs"][0]["body"]["content"] = _paragraphs(lines)
        self.snapshot["template_sha256"] = native_template_sha256(template)
        draft = self.prepare()
        self.assertNotIn("Transcript", native.document_text(self.drive.documents[draft["document_id"]]))

    def test_exact_reader_grants_and_repeat_no_reinvitation(self):
        draft = self.prepare()
        receipt = self.share(draft, ("max@example.test", "second@example.test"))
        self.share(draft, ("second@example.test", "max@example.test"))
        self.assertEqual(len(self.drive.sends), 2)
        self.assertEqual(set(receipt["recipients"]), {"max@example.test", "second@example.test"})
        self.assertEqual(len(self.drive.items[SharingFake.PRIVATE]["permissions"]), 1)
        self.assertEqual(len(self.drive.items[FakeDrive.COMPANY]["permissions"]), 1)

    def test_lost_copy_result_never_duplicates_or_adopts_name_match(self):
        self.drive.fail_copy = True
        with self.assertRaises(SimulatedTransportFailure):
            self.prepare()
        self.drive.fail_copy = False
        with self.assertRaisesRegex(SharingNeedsReview, "without a saved ID"):
            self.prepare()
        self.assertEqual(self.drive.counter, 1)

    def test_lost_edit_after_write_reconciles_exact_copy(self):
        self.drive.fail_edit = "after"
        with self.assertRaises(SimulatedTransportFailure):
            self.prepare()
        self.drive.fail_edit = None
        draft = self.prepare()
        self.assertTrue(draft["content_hash"])
        self.assertEqual(len([1 for tool, _ in self.drive.calls if tool == "batch_update_document"]), 1)

    def test_lost_edit_before_write_holds_without_repeat(self):
        self.drive.fail_edit = "before"
        with self.assertRaises(SimulatedTransportFailure):
            self.prepare()
        self.drive.fail_edit = None
        with self.assertRaisesRegex(SharingNeedsReview, "uncertain"):
            self.prepare()
        self.assertEqual(len([1 for tool, _ in self.drive.calls if tool == "batch_update_document"]), 1)

    def test_lost_share_response_reconciles_permission_without_resending(self):
        draft = self.prepare()
        self.drive.fail_share = "after"
        receipt = self.share(draft)
        self.share(draft)
        self.assertEqual(len(self.drive.sends), 1)
        self.assertTrue(receipt["recipients"]["max@example.test"]["reconciled"])

    def test_lost_share_before_write_holds_and_does_not_reinvite(self):
        draft = self.prepare()
        self.drive.fail_share = "before"
        with self.assertRaisesRegex(SharingNeedsReview, "uncertain"):
            self.share(draft)
        self.drive.fail_share = None
        with self.assertRaisesRegex(SharingNeedsReview, "uncertain"):
            self.share(draft)
        self.assertEqual(len(self.drive.sends), 1)

    def test_changed_recipient_approval_cannot_continue_partial_attempt(self):
        draft = self.prepare()
        self.share(draft)
        with self.assertRaisesRegex(SharingNeedsReview, "different recipients"):
            self.share(draft, ("other@example.test",))
        self.assertEqual(len(self.drive.sends), 1)

    def test_external_document_change_blocks_share(self):
        draft = self.prepare()
        self.drive.documents[draft["document_id"]]["revisionId"] = "edited"
        with self.assertRaisesRegex(SharingNeedsReview, "changed after preview"):
            self.share(draft)
        self.assertFalse(self.drive.sends)

    def test_public_or_shared_ancestor_blocks_copy(self):
        self.drive.items[FakeDrive.ROOT]["shared"] = True
        self.drive.items[FakeDrive.ROOT]["permissions"].append({"type": "anyone", "role": "reader"})
        with self.assertRaises(SharingNeedsReview):
            self.prepare()
        self.assertEqual(self.drive.counter, 0)

    def test_concrete_private_drive_root_with_null_parents_supports_prepare_and_share(self):
        self.drive.items[FakeDrive.ROOT]["parent_ids"] = [SharingFake.MY_DRIVE]
        draft = self.prepare()
        receipt = self.share(draft)
        self.assertEqual(receipt["document_id"], draft["document_id"])
        self.assertTrue(any(tool == "get_file_metadata" and args["fileId"] == "root"
                            for tool, args in self.drive.calls))
        self.assertEqual(len(self.drive.items[SharingFake.PRIVATE]["permissions"]), 1)
        self.assertEqual(len(self.drive.items[SharingFake.MY_DRIVE]["permissions"]), 1)
        self.assertEqual(len(self.drive.sends), 1)

    def test_parentless_nonroot_folder_cannot_be_treated_as_drive_root(self):
        for parents in (None, []):
            with self.subTest(parents=parents):
                self.drive.items[FakeDrive.ROOT]["parent_ids"] = parents
                self.drive.items[FakeDrive.ROOT]["title"] = "My Drive"
                with self.assertRaisesRegex(SharingNeedsReview, "authenticated Drive root"):
                    self.prepare()
        self.assertEqual(self.drive.counter, 0)
        self.assertFalse(self.drive.sends)

    def test_missing_parent_metadata_is_not_accepted_even_for_root(self):
        self.drive.items[FakeDrive.ROOT]["parent_ids"] = [SharingFake.MY_DRIVE]
        del self.drive.items[SharingFake.MY_DRIVE]["parent_ids"]
        with self.assertRaisesRegex(SharingNeedsReview, "ancestry is unreadable"):
            self.prepare()
        self.assertEqual(self.drive.counter, 0)

    def test_root_alias_must_resolve_to_private_owner_folder(self):
        original = copy.deepcopy(self.drive.items[SharingFake.MY_DRIVE])
        for changes in ({"id": "root"}, {"mime_type": DOCUMENT_MIME}, {"parent_ids": ["other_root_123"]},
                        {"shared": True}, {"drive_id": "shared_drive_123"},
                        {"permissions": [{"type": "user", "role": "owner", "emailAddress": "other@example.test"}]}):
            self.drive.items[SharingFake.MY_DRIVE] = dict(original, **changes)
            with self.subTest(changes=changes), self.assertRaises(SharingNeedsReview):
                self.prepare()
        self.assertEqual(self.drive.counter, 0)
        self.assertFalse(self.drive.sends)

    def test_root_permission_change_after_preview_blocks_invitation(self):
        draft = self.prepare()
        self.drive.items[SharingFake.MY_DRIVE]["shared"] = True
        self.drive.items[SharingFake.MY_DRIVE]["permissions"].append({"type": "anyone", "role": "reader"})
        with self.assertRaises(SharingNeedsReview):
            self.share(draft)
        self.assertFalse(self.drive.sends)

    def test_shared_folder_between_preview_and_share_blocks_invitation(self):
        draft = self.prepare()
        self.drive.items[FakeDrive.COMPANY]["shared"] = True
        with self.assertRaises(SharingNeedsReview):
            self.share(draft)
        self.assertFalse(self.drive.sends)

    def test_nonapproved_reader_or_elevated_permission_blocks_sharing(self):
        draft = self.prepare()
        item = self.drive.items[draft["document_id"]]
        item["shared"] = True
        item["permissions"].append({"type": "user", "role": "writer", "emailAddress": "max@example.test"})
        with self.assertRaises(SharingNeedsReview):
            self.share(draft)
        self.assertFalse(self.drive.sends)

    def test_draft_url_cannot_target_private_original(self):
        draft = self.prepare()
        draft["url"] = "https://docs.google.com/document/d/" + SharingFake.PRIVATE + "/edit"
        self.journal["draft"] = copy.deepcopy(draft)
        with self.assertRaisesRegex(SharingNeedsReview, "URL"):
            self.share(draft)
        self.assertFalse(self.drive.sends)

    def test_guard_revocation_prevents_any_write(self):
        def rejected():
            raise SharingNeedsReview("Approval revoked")
        self.guard = rejected
        with self.assertRaisesRegex(SharingNeedsReview, "Approval revoked"):
            self.prepare()
        self.assertEqual(self.drive.counter, 0)

    def test_private_original_cannot_be_template(self):
        self.snapshot["template_document_id"] = SharingFake.PRIVATE
        with self.assertRaisesRegex(SharingNeedsReview, "cannot be used"):
            self.prepare()
        self.assertEqual(self.drive.counter, 0)

    def test_snapshot_change_does_not_reuse_draft(self):
        self.prepare()
        self.snapshot["notes_payload"]["replacements"]["{{QUILL_SUMMARY}}"] = "Changed notes"
        with self.assertRaisesRegex(SharingNeedsReview, "different reviewed notes"):
            self.prepare()

    def test_saved_intents_exist_before_each_mutation(self):
        draft = self.prepare()
        self.share(draft)
        self.assertTrue(any(s["copy"]["state"] == "dispatched" for s in self.saves))
        self.assertTrue(any(s["fill"]["state"] == "dispatched" for s in self.saves))
        self.assertTrue(any(s.get("share", {}).get("recipients", {}).get("max@example.test", {}).get("state") == "dispatched" for s in self.saves))

    def test_review_controller_and_native_adapter_end_to_end_stay_private_until_confirmed(self):
        from quill_workflow.sharing import SharingReviews
        self.snapshot["preview"] = "Reviewed notes only"
        with tempfile.TemporaryDirectory() as directory:
            reviews = SharingReviews(directory, Path(directory) / "config.json", lambda job: copy.deepcopy(self.snapshot))
            try:
                card = reviews.card("synthetic")
                requested = reviews.decide("sharing_prepare", "synthetic", card["revision"], "max@example.test")
                # The fake asserts saved intent at the moment of each write.
                # Its journal reference follows the controller's current row.
                real_invoke = self.drive.__call__
                def invoke(tool, arguments):
                    self.drive.journal = reviews.get("synthetic")["journal"]
                    return real_invoke(tool, arguments)
                adapter = SharingDrive(invoke)
                ready = reviews.run("synthetic", requested["revision"], adapter)
                self.assertEqual(ready["status"], "ready", ready.get("error"))
                self.assertFalse(self.drive.sends)
                self.assertEqual(native.document_text(self.drive.documents[ready["draft"]["document_id"]]),
                                 notes_plan(self.drive.documents[SharingFake.TEMPLATE], _snapshot(self.snapshot)["notes_payload"])["text"])
                confirmed = reviews.decide("sharing_confirm", "synthetic", ready["revision"])
                completed = reviews.run("synthetic", confirmed["revision"], adapter)
                self.assertEqual(completed["status"], "shared", completed.get("error"))
                self.assertEqual(len(self.drive.sends), 1)
                self.assertEqual(completed["receipt"]["recipients"]["max@example.test"]["state"], "verified")
                self.assertEqual(len(self.drive.items[SharingFake.PRIVATE]["permissions"]), 1)
            finally:
                reviews.close()


if __name__ == "__main__":
    unittest.main()
