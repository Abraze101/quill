"""Optional platform evidence survives queue snapshots without breaking old jobs."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.pipeline import (ConnectorRunner, NeedsConfiguration, Queue, WorkflowError,
                                    atomic_json, optional_speaker_timeline, snapshot_session)
from quill_workflow.speaker_context import build_context


class SpeakerTimelinePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = self.root / "recordings" / "synthetic"
        self.session.mkdir(parents=True)
        self.metadata = {"started": "2026-01-01T10:00:00Z", "ended": "2026-01-01T10:01:00Z",
                         "files": {"mic": "mic.caf", "system": "system.caf"},
                         "speaker_activity_file": "speaker-activity.json",
                         "meeting_context": {"provider": "google-meet", "key": "abc-defg-hij",
                             "coverage": "visible_only", "participants": [
                                 {"name": "Alex Owner", "self": True}]}}
        self.timeline = {"schema_version": 1, "provider": "google-meet", "key": "abc-defg-hij",
                         "participants": [{"id": "document:p1", "name": "Casey Rivera", "self": False}],
                         "intervals": [{"start_ms": 0, "end_ms": 500, "active": ["document:p1"]}],
                         "truncated": False}
        self.transcript = {"segments": [{"speaker": "mic", "text": "Synthetic test", "start_ms": 0}]}
        for filename in ("mic.caf", "system.caf"):
            (self.session / filename).write_bytes(b"synthetic fixture")
        atomic_json(self.session / "meta.json", self.metadata)
        atomic_json(self.session / "speaker-activity.json", self.timeline)

    def tearDown(self):
        self.temp.cleanup()

    def context(self, timeline=None):
        def disconnected(*args):
            raise ValueError("Synthetic disconnected calendar")
        return build_context("synthetic", self.metadata, self.transcript,
                             timeline=timeline, invoke=disconnected)[0]

    def test_snapshot_preserves_and_hashes_declared_timeline(self):
        before = (self.session / "speaker-activity.json").read_bytes()
        _, manifest = snapshot_session(self.session, self.session.parent, self.root / "snapshot")
        self.assertIn("speaker-activity.json", manifest)
        self.assertEqual((self.root / "snapshot/speaker-activity.json").read_bytes(), before)
        self.assertEqual((self.session / "speaker-activity.json").read_bytes(), before)

    def test_invalid_optional_evidence_never_blocks_queueing(self):
        variants = [dict(self.timeline, key="zzz-yyyy-xxx"),
                    dict(self.timeline, intervals=[{"start_ms": 0, "end_ms": 500, "active": ["missing"]}]),
                    {"unrecognized": "shape"}]
        for index, variant in enumerate(variants):
            atomic_json(self.session / "speaker-activity.json", variant)
            _, manifest = snapshot_session(self.session, self.session.parent, self.root / ("snapshot-" + str(index)))
            self.assertNotIn("speaker-activity.json", manifest)

    def test_missing_undeclared_linked_and_path_traversal_evidence_is_ignored(self):
        self.assertIsNone(optional_speaker_timeline(self.session, dict(self.metadata, speaker_activity_file="../private.json")))
        self.assertIsNone(optional_speaker_timeline(self.session, {"files": self.metadata["files"]}))
        path = self.session / "speaker-activity.json"
        path.unlink()
        self.assertIsNone(optional_speaker_timeline(self.session, self.metadata))
        atomic_json(self.root / "outside.json", self.timeline)
        path.symlink_to(self.root / "outside.json")
        self.assertIsNone(optional_speaker_timeline(self.session, self.metadata))

    def test_local_identity_survives_without_invented_speaking_times(self):
        context = self.context()
        self.assertEqual(context["schema_version"], 2)
        self.assertEqual(context["timeline"]["intervals"], [])
        self.assertEqual(context["timeline"]["participants"], [
            {"id": "visible-self-0", "name": "Alex Owner", "self": True}])

    def test_wrong_meeting_cannot_supply_voice_evidence(self):
        context = self.context(dict(self.timeline, key="zzz-yyyy-xxx"))
        self.assertEqual(context["timeline"]["intervals"], [])
        self.assertNotIn("Casey Rivera", [person["name"] for person in context["candidates"]])

    def test_valid_timeline_keeps_original_and_self_evidence_separate(self):
        original = copy.deepcopy(self.timeline)
        context = self.context(self.timeline)
        self.assertEqual(self.timeline, original)
        self.assertEqual(context["timeline"]["intervals"], self.timeline["intervals"])
        self.assertEqual(context["timeline"]["participants"][-1]["name"], "Alex Owner")
        self.assertTrue(context["timeline"]["participants"][-1]["self"])

    def test_older_context_without_explicit_self_remains_v1(self):
        self.metadata["meeting_context"]["participants"] = []
        self.assertEqual(self.context(), {"schema_version": 1, "candidates": []})

    def prepared_runner(self):
        atomic_json(self.session / "transcript.json", self.transcript)
        settings = {"enabled": True, "command": ["synthetic-notes", "{input}", "{output}"]}
        queue = Queue(self.root / "state")
        try:
            job = queue.enqueue(self.session, {"recordings_root": str(self.session.parent), "notes": settings})
        finally:
            queue.close()
        runner = ConnectorRunner(self.root / "state", self.root / "synthetic-skill.md")
        def execute(argv, *_):
            atomic_json(Path(argv[2]), {"schema_version": 1, "summary": [
                {"text": "Synthetic test", "evidence": [0]}], "actions": []})
        runner.execute = execute
        return job, runner, settings, execute

    def test_timeline_change_during_notes_is_rejected(self):
        job, runner, settings, execute = self.prepared_runner()
        def changed(argv, *args):
            execute(argv, *args)
            altered = dict(self.timeline, intervals=[])
            atomic_json(self.root / "state/recordings" / job / "speaker-activity.json", altered)
        runner.execute = changed
        with self.assertRaisesRegex(WorkflowError, "Speaker timeline changed"):
            runner("notes", job, settings)
        self.assertFalse((self.root / "state/results" / job / "notes").exists())

    def test_completed_notes_retry_is_bound_to_original_timeline(self):
        job, runner, settings, _ = self.prepared_runner()
        receipt = runner("notes", job, settings)
        self.assertTrue(receipt["speaker_timeline_sha256"])
        runner.execute = lambda *_: self.fail("Completed notes ran twice")
        self.assertEqual(runner("notes", job, settings), receipt)
        atomic_json(self.root / "state/recordings" / job / "speaker-activity.json",
                    dict(self.timeline, intervals=[]))
        with self.assertRaisesRegex(NeedsConfiguration, "explicit revision"):
            runner("notes", job, settings)


if __name__ == "__main__":
    unittest.main()
