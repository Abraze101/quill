"""Synthetic warning propagation, validation, and immutable export checks."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.capture_quality import NOTICE, validated_quality
from quill_workflow.codex_notes import correction_prompt
from quill_workflow.meeting_document import build_document, render_document
from quill_workflow.native_template import build_payload, verify_document
from quill_workflow.pipeline import WorkflowError, notes_markdown_matches, render_notes
from quill_workflow.review_ui import notes_html
from test_native_template import filled_document, template_document


class CaptureQualityTests(unittest.TestCase):
    def setUp(self):
        self.transcript = {"capture_quality": {"status": "incomplete", "affected_sources": ["system", "mic"]},
                           "segments": [{"start_ms": 1200, "end_ms": 2100, "speaker": "mic",
                                         "text": "We agreed to review the dashboard on Friday."}]}
        self.notes = {"schema_version": 1,
                      "summary": [{"text": "The dashboard review is planned.", "evidence": [0]}],
                      "decisions": [{"text": "Review the dashboard on Friday.", "evidence": [0]}],
                      "actions": [{"text": "Prepare the dashboard.", "owner": None, "due": "Friday", "evidence": [0]}]}
        self.metadata = {"started": "2026-10-24T09:00:00+08:00", "ended": "2026-10-24T09:01:00+08:00"}
        self.route = {"company_name": "Example Co"}

    def test_quality_is_bounded_canonical_and_deduplicated(self):
        original = copy.deepcopy(self.transcript)
        self.assertEqual(validated_quality(self.transcript),
                         {"status": "incomplete", "affected_sources": ["mic", "system"]})
        self.assertEqual(self.transcript, original)
        self.transcript["capture_quality"]["affected_sources"] = ["system", "system"]
        self.assertEqual(validated_quality(self.transcript)["affected_sources"], ["system"])

    def test_bad_quality_never_reaches_notes_documents_or_model_prompt(self):
        for quality in (None, True, [], {}, {"status": "complete", "affected_sources": ["mic"]},
                        {"status": "incomplete", "affected_sources": []},
                        {"status": "incomplete", "affected_sources": "mic"},
                        {"status": "incomplete", "affected_sources": ["mic"] * 1000},
                        {"status": "incomplete", "affected_sources": [False]},
                        {"status": "incomplete", "affected_sources": [["mic"]]},
                        {"status": "incomplete", "affected_sources": ["screen"]},
                        {"status": "incomplete", "affected_sources": ["system"], "notice": "RAW DEVICE ERROR"}):
            transcript = dict(self.transcript, capture_quality=quality)
            for render in (
                    lambda: render_notes(self.notes, transcript),
                    lambda: render_document(self.notes, transcript, self.metadata, self.route),
                    lambda: build_payload(self.notes, transcript, self.metadata, self.route),
                    lambda: correction_prompt("Supplied skill", transcript, "Rejected draft", "error")):
                with self.subTest(quality=quality, render=render), self.assertRaises(WorkflowError):
                    render()

    def test_markdown_and_review_show_one_notice_without_timestamp_clutter(self):
        before = copy.deepcopy((self.notes, self.transcript))
        for include_sources in (False, True):
            markdown = render_notes(self.notes, self.transcript, include_sources=include_sources)
            self.assertEqual(markdown.count(NOTICE), 1)
            self.assertLess(markdown.index(NOTICE), markdown.index("## Summary"))
            self.assertTrue(notes_markdown_matches(markdown, self.notes, self.transcript))
            self.assertFalse(notes_markdown_matches(markdown.replace(NOTICE + "\n\n", ""), self.notes, self.transcript))
            self.assertEqual(notes_html(markdown).count(NOTICE), 1)
            if not include_sources:
                self.assertNotIn("00:01", markdown)
        self.assertEqual((self.notes, self.transcript), before)

    def test_legacy_transcripts_remain_unwarned(self):
        transcript = dict(self.transcript)
        transcript.pop("capture_quality")
        self.assertIsNone(validated_quality(transcript))
        self.assertNotIn(NOTICE, render_notes(self.notes, transcript))
        payload = build_payload(self.notes, transcript, self.metadata, self.route)
        self.assertNotIn(NOTICE, json.dumps(payload))
        prompt = json.loads(correction_prompt("skill", transcript, "draft", "error"))
        self.assertNotIn("capture_quality", prompt)
        self.assertEqual(prompt["transcript"], transcript)

    def test_combined_docx_warning_is_once_and_transcript_remains_exact(self):
        for layout in ("clean", "sources"):
            payload, text, _ = render_document(self.notes, self.transcript, self.metadata, self.route,
                                                notes_layout=layout)
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                content = ET.fromstring(archive.read("word/document.xml"))
                self.assertEqual("".join(content.itertext()).count(NOTICE), 1)
            self.assertEqual(text.count(NOTICE), 1)
            self.assertLess(text.index(NOTICE), text.index("Summary"))
            self.assertEqual(text[-1], "00:01 | Microphone  " + self.transcript["segments"][0]["text"])
            self.assertNotIn(NOTICE, "\n".join(text[text.index("Full transcript") + 1:]))

    def test_native_template_has_one_notice_and_preserves_styles_and_segments(self):
        payload = build_payload(self.notes, self.transcript, self.metadata, self.route)
        replacements = payload["replacements"]
        self.assertTrue(replacements["{{QUILL_SUMMARY}}"].startswith(NOTICE + "\n\n"))
        self.assertEqual("\n".join(replacements.values()).count(NOTICE), 1)
        self.assertEqual(replacements["{{QUILL_TRANSCRIPT}}"],
                         "00:01 | Microphone  " + self.transcript["segments"][0]["text"])
        template = template_document()
        self.assertTrue(verify_document(filled_document(payload, template), payload, template))

    def test_quality_change_invalidates_export_revision_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = build_document(self.notes, self.transcript, self.metadata, self.route, temporary)
            original = first["path"].read_bytes()
            repeated = build_document(self.notes, self.transcript, self.metadata, self.route, temporary)
            self.assertEqual(repeated["manifest"], first["manifest"])
            transcript = dict(self.transcript)
            transcript.pop("capture_quality")
            with self.assertRaisesRegex(ValueError, "explicit revision"):
                build_document(self.notes, transcript, self.metadata, self.route, temporary)
            self.assertEqual(first["path"].read_bytes(), original)

    def test_model_cannot_duplicate_fixed_notice_as_a_finding(self):
        notes = copy.deepcopy(self.notes)
        notes["summary"][0]["text"] += " " + NOTICE
        with self.assertRaisesRegex(WorkflowError, "exporter adds it once"):
            render_notes(notes, self.transcript)

    def test_correction_prompt_keeps_quality_structured_and_segments_exact(self):
        payload = json.loads(correction_prompt("skill", self.transcript, "draft", "error"))
        self.assertEqual(payload["capture_quality"], {"status": "incomplete", "affected_sources": ["mic", "system"]})
        self.assertEqual(payload["transcript"]["capture_quality"], payload["capture_quality"])
        self.assertEqual(payload["transcript"]["segments"], self.transcript["segments"])
        self.assertIn("never infer or invent missing speech", payload["capture_quality_instruction"])
        self.assertIn("do not repeat", payload["capture_quality_instruction"])


if __name__ == "__main__":
    unittest.main()
