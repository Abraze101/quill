"""Browser evidence schema; every identity and name below is synthetic."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.meeting_context import meeting_context

spec = importlib.util.spec_from_file_location("context_native_host", ROOT / "native-host.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class MeetingContextTests(unittest.TestCase):
    def setUp(self):
        self.context = {"provider": "google-meet", "key": "abc-defg-hij", "coverage": "visible_only",
                        "participants": [{"name": "Casey Rivera", "self": False}]}

    def test_wire_and_metadata_share_the_exact_schema(self):
        for context in (self.context, dict(self.context, provider="zoom-web", key="12345678901"),
                        dict(self.context, participants=[]),
                        dict(self.context, participants=[{"name": "李明", "self": True}])):
            self.assertEqual(meeting_context(context), context)
            self.assertEqual(native.meeting_context(context), context)

    def test_invalid_evidence_is_rejected_consistently(self):
        cases = [None, [], {}, dict(self.context, key="https://meet.google.com/abc-defg-hij"),
                 dict(self.context, key="abc-defg-hij?authuser=1"), dict(self.context, provider=[]),
                 dict(self.context, provider="zoom-web"), dict(self.context, coverage="complete"),
                 dict(self.context, url="https://private.example"), dict(self.context, participants="Casey")]
        for name in ("", " Casey", "Casey  Rivera", "a\nb", "a\u202eb", "@someone", "https://example.test",
                     "x" * 101, "Jose\u0301", "You", "unknown"):
            cases.append(dict(self.context, participants=[{"name": name, "self": False}]))
        for person in ({"name": "Casey", "self": "false"}, {"name": "Casey"},
                       {"name": "Casey", "self": False, "email": "casey@example.test"}):
            cases.append(dict(self.context, participants=[person]))
        for context in cases:
            for validate in (meeting_context, native.meeting_context):
                with self.subTest(value=context, validator=validate.__module__), self.assertRaises(ValueError):
                    validate(context)

    def test_signal_and_accumulated_metadata_have_separate_bounds(self):
        value = dict(self.context, participants=[{"name": "Person " + str(i), "self": False} for i in range(21)])
        self.assertEqual(len(meeting_context(value)["participants"]), 21)
        with self.assertRaises(ValueError):
            native.meeting_context(value)
        value["participants"] *= 3
        with self.assertRaises(ValueError):
            meeting_context(value)


if __name__ == "__main__":
    unittest.main()
