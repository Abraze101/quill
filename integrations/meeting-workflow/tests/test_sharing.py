import copy
import fcntl
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.pipeline import WorkflowError
from quill_workflow.sharing import SharingReviews, digest, recipients


class Adapter:
    def __init__(self):
        self.calls = []
        self.fail = False
    def prepare(self, snapshot, journal, save, guard):
        guard()
        self.calls.append("prepare")
        journal["copied"] = True
        save()
        if self.fail:
            raise RuntimeError("Interrupted copy")
        return {"document_id": "synthetic-note-copy", "url": "https://docs.google.com/document/d/synthetic-note-copy/edit",
                "revision": "v1", "content_hash": "exactnotes"}
    def share(self, snapshot, draft, chosen, journal, save, guard):
        guard()
        self.calls.append(("share", chosen))
        if self.fail:
            journal["attempted"] = True
            save()
            raise RuntimeError("Interrupted permission readback")
        return {"recipients": {email: {"state": "verified"} for email in chosen}}


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.source = {"title": "Synthetic notes", "preview": "Summary\nA next step.", "owner": "owner@example.com", "binding": "fixed"}
        self.reviews = SharingReviews(self.state, self.state / "config.json", lambda job: copy.deepcopy(self.source))
        self.adapter = Adapter()
    def tearDown(self):
        self.reviews.close()
        self.temp.cleanup()
    def prepare(self):
        card = self.reviews.card("test")
        return self.reviews.decide("sharing_prepare", "test", card["revision"], "Alice@example.com, bob@example.com")
    def ready(self):
        record = self.prepare()
        return self.reviews.run("test", record["revision"], self.adapter)
    def confirm(self):
        record = self.ready()
        return self.reviews.decide("sharing_confirm", "test", record["revision"])
    def test_prepare_is_private_until_separate_confirmation(self):
        record = self.ready()
        self.assertEqual(record["status"], "ready")
        self.assertEqual(self.adapter.calls, ["prepare"])
        self.assertNotIn("approval", record)
        card = self.reviews.card("test")
        self.assertEqual(card["recipients"], ["alice@example.com", "bob@example.com"])
        self.assertEqual(card["preview"], self.source["preview"])
    def test_confirm_binds_copy_source_and_emails(self):
        record = self.confirm()
        self.assertEqual(record["approval"]["draft_hash"], digest(record["draft"]))
        result = self.reviews.run("test", record["revision"], self.adapter)
        self.assertEqual(result["status"], "shared")
        self.assertEqual(set(result["receipt"]["recipients"]), {"alice@example.com", "bob@example.com"})
        with self.assertRaises(WorkflowError):
            self.reviews.run("test", record["revision"], self.adapter)
        with self.assertRaises(WorkflowError):
            self.reviews.decide("sharing_confirm", "test", result["revision"])
        self.assertEqual(len(self.adapter.calls), 2)
    def test_confirmation_cannot_supply_new_emails(self):
        record = self.ready()
        with self.assertRaises(WorkflowError):
            self.reviews.decide("sharing_confirm", "test", record["revision"], "eve@example.com")
    def test_editing_recipient_requires_fresh_confirmation_without_new_copy(self):
        ready = self.ready()
        changed = self.reviews.decide("sharing_prepare", "test", ready["revision"], "carol@example.com")
        self.assertEqual(changed["status"], "ready")
        self.assertEqual(changed["draft"], ready["draft"])
        self.assertNotIn("approval", changed)
        with self.assertRaises(WorkflowError):
            self.reviews.decide("sharing_confirm", "test", ready["revision"])
        confirmed = self.reviews.decide("sharing_confirm", "test", changed["revision"])
        self.assertEqual(confirmed["approval"]["recipients"], ["carol@example.com"])
        self.assertEqual(self.adapter.calls, ["prepare"])
    def test_notes_changed_after_preview_blocks_prepare(self):
        card = self.reviews.card("test")
        self.source["preview"] = "Changed"
        with self.assertRaises(WorkflowError):
            self.reviews.decide("sharing_prepare", "test", card["revision"], "alice@example.com")
    def test_notes_changed_after_prepare_blocks_confirmation(self):
        record = self.ready()
        self.source["binding"] = "changed"
        with self.assertRaises(WorkflowError):
            self.reviews.decide("sharing_confirm", "test", record["revision"])
        self.assertFalse(self.reviews.card("test")["retryable"])
        # A changed or deleted source does not prevent keeping the copy private.
        result = self.reviews.decide("sharing_private", "test", record["revision"])
        self.assertEqual(result["status"], "private")
    def test_notes_changed_after_confirmation_blocks_worker(self):
        record = self.confirm()
        self.source["binding"] = "changed"
        result = self.reviews.run("test", record["revision"], self.adapter)
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(self.adapter.calls, ["prepare"])
    def test_altered_saved_copy_or_emails_fail_approval_check(self):
        record = self.confirm()
        record["draft"]["revision"] = "new"
        self.reviews.put(record)
        result = self.reviews.run("test", record["revision"], self.adapter)
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(self.adapter.calls, ["prepare"])
    def test_guard_rechecks_source_at_each_operation(self):
        record = self.confirm()
        outer = self
        class ChangingAdapter:
            def share(self, snapshot, draft, chosen, journal, save, guard):
                guard()
                outer.source["binding"] = "changed"
                guard()
                raise AssertionError("A second write must be blocked")
        result = self.reviews.run("test", record["revision"], ChangingAdapter())
        self.assertEqual(result["status"], "needs_attention")
        self.assertIn("changed", result["error"])
    def test_keep_private_never_starts_adapter(self):
        card = self.reviews.card("test")
        result = self.reviews.decide("sharing_private", "test", card["revision"])
        self.assertEqual(result["status"], "private")
        with self.assertRaises(WorkflowError):
            self.reviews.run("test", result["revision"], self.adapter)
        self.assertEqual(self.adapter.calls, [])
    def test_keep_private_cannot_claim_to_revoke_approved_access(self):
        record = self.confirm()
        with self.assertRaises(WorkflowError):
            self.reviews.decide("sharing_private", "test", record["revision"])
    def test_partial_result_survives_reopening_and_retry(self):
        record = self.confirm()
        self.adapter.fail = True
        failed = self.reviews.run("test", record["revision"], self.adapter)
        self.assertTrue(failed["journal"]["attempted"])
        self.reviews.close()
        self.reviews = SharingReviews(self.state, self.state / "config.json", lambda job: copy.deepcopy(self.source))
        retry = self.reviews.decide("sharing_retry", "test", failed["revision"])
        self.assertEqual(retry["approval"], record["approval"])
        self.assertTrue(retry["journal"]["attempted"])
        self.adapter.fail = False
        self.assertEqual(self.reviews.run("test", retry["revision"], self.adapter)["status"], "shared")
    def test_attempts_are_bounded(self):
        record = self.prepare()
        self.adapter.fail = True
        for attempt in range(3):
            record = self.reviews.run("test", record["revision"], self.adapter)
            if attempt < 2:
                record = self.reviews.decide("sharing_retry", "test", record["revision"])
        self.assertFalse(self.reviews.card("test")["retryable"])
        with self.assertRaises(WorkflowError):
            self.reviews.decide("sharing_retry", "test", record["revision"])
    def test_busy_operation_lock_prevents_duplicate_or_false_recovery(self):
        record = self.prepare()
        with self.reviews.locked("test"):
            with self.assertRaises(WorkflowError):
                self.reviews.run("test", record["revision"], self.adapter)
            with patch("quill_workflow.sharing.time.time", return_value=record["updated"] + 100):
                self.assertEqual(self.reviews.card("test")["status"], "preparing")
    def test_interrupted_worker_becomes_manual_retry(self):
        record = self.prepare()
        with patch("quill_workflow.sharing.time.time", return_value=record["updated"] + 100):
            card = self.reviews.card("test")
        self.assertEqual(card["status"], "needs_attention")
        self.assertTrue(card["retryable"])
        self.assertNotEqual(card["revision"], record["revision"])
        self.assertEqual(self.adapter.calls, [])
    def test_changed_source_after_worker_crash_can_still_be_kept_private(self):
        record = self.prepare()
        self.source["binding"] = "changed"
        with patch("quill_workflow.sharing.time.time", return_value=record["updated"] + 100):
            card = self.reviews.card("test")
        self.assertEqual(card["status"], "needs_attention")
        self.assertFalse(card["retryable"])
        self.assertEqual(self.reviews.decide("sharing_private", "test", card["revision"])["status"], "private")
    def test_software_recovery_blocks_new_sharing_actions(self):
        with (self.state / "sharing-work.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(WorkflowError, "recovery"):
                self.prepare()
        self.assertIsNone(self.reviews.get("test"))
    def test_owner_is_not_a_recipient(self):
        with self.assertRaises(WorkflowError):
            self.reviews.decide("sharing_prepare", "test", digest(self.source), "owner@example.com")
    def test_recipient_validation_and_normalization(self):
        self.assertEqual(recipients("Alice@example.com; alice@example.com\nbob@example.com"), ["alice@example.com", "bob@example.com"])
        for value in ("", "Alice <alice@example.com>", "all", "@example.com", "https://example.com"):
            with self.assertRaises(WorkflowError):
                recipients(value)


if __name__ == "__main__":
    unittest.main()
