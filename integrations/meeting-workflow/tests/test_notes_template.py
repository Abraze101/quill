import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.pipeline import WorkflowError, notes_markdown_matches, render_notes


class NotesTemplateTests(unittest.TestCase):
    def setUp(self):
        self.transcript = {"segments": [
            {"start_ms": 1200, "text": "Hi Max, we agreed to review the dashboard on Friday."}]}
        self.notes = {
            "schema_version": 1,
            "meeting_title": {"text": "Dashboard review", "evidence": [0],
                              "participants": [{"name": "Max", "evidence": [0]}]},
            "summary": [{"text": "The dashboard review is planned.", "evidence": [0]}],
            "topics": [{"text": "Dashboard: Review the current layout.", "evidence": [0]}],
            "decisions": [{"text": "Review the dashboard on Friday.", "evidence": [0]}],
            "actions": [{"text": "Send the dashboard", "owner": "Max", "due": "Friday", "evidence": [0]}],
            "next_steps": [{"text": "Proposed: Ask for a usability review.", "evidence": [0]}],
            "customer_priorities": [{"text": "Readable appointment dates.", "evidence": [0]}],
            "objections": [{"text": "Small labels are hard to read.", "evidence": [0]}],
            "competitive_intel": [{"text": "A competitor has a calendar view.", "evidence": [0]}],
            "deal_implications": [{"text": "Assessment: The layout may affect adoption.", "evidence": [0]}],
            "open_questions": [{"text": "The owner of the review is unclear.", "evidence": [0]}],
            "follow_up_draft": {"subject": "Dashboard review", "body": "Please review the layout.\nThanks.", "evidence": [0]},
        }

    def test_template_order_and_all_supported_content_are_preserved(self):
        before = copy.deepcopy(self.notes)
        markdown = render_notes(self.notes, self.transcript)
        headings = [line for line in markdown.splitlines() if line.startswith("## ")]
        self.assertEqual(headings, ["## Summary", "## Decisions", "## Next steps", "## Details"])
        self.assertIn("# Dashboard review - Max\n\nParticipants: Max", markdown)
        self.assertIn("**Aligned**\n\n- Review the dashboard on Friday.", markdown)
        self.assertIn("- [Max] Send the dashboard Due: Friday", markdown)
        self.assertIn("- Next step: Proposed: Ask for a usability review.", markdown)
        self.assertIn("- **Dashboard:** Review the current layout.", markdown)
        for label, text in (("Customer priorities", "Readable appointment dates."),
                            ("Concerns and objections", "Small labels are hard to read."),
                            ("Competitor mentions", "A competitor has a calendar view."),
                            ("Assessment", "The layout may affect adoption."),
                            ("Open questions", "The owner of the review is unclear.")):
            self.assertIn("- **" + label + ":** " + text, markdown)
        self.assertNotIn("**Assessment:** Assessment:", markdown)
        self.assertIn("- **Follow-up draft:** Dashboard review\n\nPlease review the layout.\n\nThanks.", markdown)
        self.assertNotIn("00:01", markdown)
        self.assertNotIn("segment 0", markdown)
        self.assertEqual(self.notes, before)

    def test_empty_sections_do_not_invent_alignment_owners_or_due_dates(self):
        notes = {"schema_version": 1, "summary": [{"text": "Planning discussion.", "evidence": [0]}]}
        markdown = render_notes(notes, self.transcript)
        self.assertIn("No decisions recorded.", markdown)
        self.assertIn("No next steps recorded.", markdown)
        self.assertNotIn("Aligned", markdown)
        notes["actions"] = [{"text": "Prepare the agenda", "owner": None, "due": None, "evidence": [0]}]
        markdown = render_notes(notes, self.transcript)
        self.assertIn("- [Unassigned] Prepare the agenda\n", markdown)
        self.assertNotIn("Due:", markdown)

    def test_exact_previous_clean_and_source_notes_remain_reviewable(self):
        notes = {"schema_version": 1,
                 "summary": [{"text": "The dashboard review is planned.", "evidence": [0]}],
                 "actions": [{"text": "Send the dashboard", "owner": "Max", "due": "Friday", "evidence": [0]}]}
        clean = ("# Meeting notes\n\n## Summary\n\n- The dashboard review is planned.\n\n"
                 "## Action items\n\n- [ ] Send the dashboard — Owner: Max; due: Friday\n")
        sources = ("# Meeting notes\n\n## Summary\n\n- The dashboard review is planned. (00:01, segment 0)\n\n"
                   "## Action items\n\n- [ ] Send the dashboard — Owner: Max; due: Friday (00:01, segment 0)\n")
        current = render_notes(notes, self.transcript)
        for markdown in (clean, sources, current):
            with self.subTest(markdown=markdown):
                self.assertTrue(notes_markdown_matches(markdown, notes, self.transcript))
                self.assertFalse(notes_markdown_matches(markdown.replace("Friday", "Monday"), notes, self.transcript))
                self.assertFalse(notes_markdown_matches(markdown + "Unreviewed extra finding.\n", notes, self.transcript))
        notes["actions"][0]["evidence"] = [9]
        for markdown in (clean, sources, current):
            with self.assertRaisesRegex(WorkflowError, "Invalid transcript segment reference"):
                notes_markdown_matches(markdown, notes, self.transcript)

    def test_invalid_hidden_evidence_in_any_section_is_rejected(self):
        for key in ("summary", "topics", "decisions", "actions", "next_steps", "customer_priorities",
                    "objections", "competitive_intel", "deal_implications", "open_questions"):
            notes = copy.deepcopy(self.notes)
            notes[key][0]["evidence"] = [99]
            with self.subTest(key=key), self.assertRaisesRegex(WorkflowError, "Invalid transcript segment reference"):
                render_notes(notes, self.transcript)


if __name__ == "__main__":
    unittest.main()
