import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.meeting_title import MAX_TITLE_BYTES, meeting_title
from quill_workflow.pipeline import WorkflowError, render_notes


class MeetingTitleTests(unittest.TestCase):
    def setUp(self):
        self.transcript = {"segments": [
            {"text": "Hi Max. Let's review our AI voice agent.", "start_ms": 1000},
            {"text": "I'm Mei Chen. I will bring the agenda.", "start_ms": 3000},
            {"text": "Maxwell is unavailable today.", "start_ms": 5000}]}
        self.notes = {"schema_version": 1, "summary": [
            {"text": "AI voice agent review.", "evidence": [0]}],
            "meeting_title": {"text": "AI voice agent review", "evidence": [0], "participants": [
                {"name": "Max", "evidence": [0]}, {"name": "Mei Chen", "evidence": [1]}]}}

    def test_names_are_added_to_topic_without_inline_source_annotations(self):
        self.assertEqual(meeting_title(self.notes, self.transcript), "AI voice agent review - Max, Mei Chen")
        markdown = render_notes(self.notes, self.transcript)
        self.assertTrue(markdown.startswith("# AI voice agent review - Max, Mei Chen\n"))
        self.assertIn("Participants: Max, Mei Chen\n", markdown)
        self.assertNotIn("Title source:", markdown)
        self.assertNotIn("segment", markdown)
        self.assertNotIn("00:03", markdown)

    def test_legacy_render_preserves_title_and_participant_sources(self):
        markdown = render_notes(self.notes, self.transcript, include_sources=True)
        self.assertEqual(markdown, "# AI voice agent review - Max, Mei Chen\n\n"
                         "Title source: 00:01, segment 0\n\n"
                         "Participants identified in the transcript:\n\n"
                         "- Max (00:01, segment 0)\n- Mei Chen (00:03, segment 1)\n\n"
                         "## Summary\n\n- AI voice agent review. (00:01, segment 0)\n")

    def test_historical_notes_have_no_new_naming_side_effects(self):
        del self.notes["meeting_title"]
        self.assertIsNone(meeting_title(self.notes, self.transcript))
        self.assertTrue(render_notes(self.notes, self.transcript).startswith("# Meeting notes\n"))

    def test_anonymous_absent_and_name_substring_evidence_are_rejected(self):
        for name, refs in (("me", [0]), ("them", [0]), ("Speaker 2", [0]), ("Unknown", [0]),
                           ("Other Speaker", [0]), ("Max Smith", [0]), ("Max", [2]), ("Mei", [0])):
            with self.subTest(name=name, refs=refs):
                self.notes["meeting_title"]["participants"] = [{"name": name, "evidence": refs}]
                with self.assertRaises(ValueError):
                    meeting_title(self.notes, self.transcript)

    def test_title_and_participant_evidence_cannot_be_missing_or_invalid(self):
        for field in ("topic", "participant"):
            for refs in ([], [True], [-1], [99], "0"):
                with self.subTest(field=field, refs=refs):
                    notes = copy.deepcopy(self.notes)
                    target = notes["meeting_title"] if field == "topic" else notes["meeting_title"]["participants"][0]
                    target["evidence"] = refs
                    with self.assertRaises(WorkflowError):
                        render_notes(notes, self.transcript)

    def test_names_are_unique_and_plain_but_preserve_international_names(self):
        self.notes["meeting_title"]["participants"].append({"name": "max", "evidence": [0]})
        with self.assertRaisesRegex(ValueError, "unique"):
            meeting_title(self.notes, self.transcript)
        self.notes["meeting_title"]["participants"] = [{"name": "王小明", "evidence": [0]}]
        self.transcript["segments"][0]["text"] = "王小明, welcome to the call."
        self.assertTrue(meeting_title(self.notes, self.transcript).endswith(" - 王小明"))
        self.notes["meeting_title"]["participants"][0]["name"] = "Max\nAdmin"
        with self.assertRaisesRegex(ValueError, "plain, exact"):
            meeting_title(self.notes, self.transcript)

    def test_path_separators_controls_and_bidi_text_cannot_escape_title(self):
        self.notes["meeting_title"]["text"] = '../CRM/AI\\Calls:\nNext\x00\u202eSteps'
        title = meeting_title(self.notes, self.transcript)
        self.assertEqual(title, "CRM AI Calls Next Steps - Max, Mei Chen")
        for forbidden in ("/", "\\", "\n", "\x00", "\u202e"):
            self.assertNotIn(forbidden, title)

    def test_long_unicode_topic_is_bounded_without_truncating_names(self):
        self.notes["meeting_title"]["text"] = "会議" * 60
        title = meeting_title(self.notes, self.transcript)
        self.assertLessEqual(len(title.encode("utf-8")), MAX_TITLE_BYTES)
        self.assertTrue(title.endswith(" - Max, Mei Chen"))
        self.assertNotIn("\ufffd", title)

    def test_long_list_keeps_whole_names_in_notes(self):
        names = ["Person " + chr(65 + index) + " LongFamilyName" for index in range(8)]
        self.transcript["segments"][0]["text"] = "Welcome " + ", ".join(names)
        self.notes["meeting_title"]["participants"] = [{"name": name, "evidence": [0]} for name in names]
        title = meeting_title(self.notes, self.transcript)
        self.assertLessEqual(len(title.encode("utf-8")), MAX_TITLE_BYTES)
        self.assertTrue(any(title.endswith(name) for name in names))
        markdown = render_notes(self.notes, self.transcript)
        self.assertTrue(all(name in markdown for name in names))

    def test_empty_participants_leave_topic_only_and_header_escapes_markdown(self):
        self.notes["meeting_title"]["participants"] = []
        self.notes["meeting_title"]["text"] = "[Agenda](draft)"
        self.assertEqual(meeting_title(self.notes, self.transcript), "[Agenda](draft)")
        self.assertTrue(render_notes(self.notes, self.transcript).startswith(r"# \[Agenda\]\(draft\)"))


if __name__ == "__main__":
    unittest.main()
