"""OOXML export tests use synthetic sources and never connect to Google Drive."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.meeting_document import build_document, render_document, timestamp, W
from quill_workflow.pipeline import WorkflowError
from test_transcript_sources import diarized_transcript

NS = {"w": W}


class MeetingDocumentTests(unittest.TestCase):
    def setUp(self):
        self.transcript = {"segments": [
            {"start_ms": 500, "end_ms": 900, "speaker": "me", "text": "Hello Max."},
            {"start_ms": 1000, "end_ms": 7000, "speaker": "them",
             "text": "Send the draft tomorrow.\nKeep it private & use <this> example."},
            {"start_ms": 8000, "end_ms": 9000, "speaker": "me", "text": "Yes.  Exactly."},
            {"start_ms": 10000, "end_ms": 11000, "speaker": "them", "text": "Yes.  Exactly."}]}
        self.notes = {"schema_version": 1,
                      "meeting_title": {"text": "Draft planning", "evidence": [1],
                                        "participants": [{"name": "Max", "evidence": [0]}]},
                      "summary": [{"text": "The draft will be sent tomorrow.", "evidence": [1]}],
                      "decisions": [{"text": "Keep the draft private.", "evidence": [1]}],
                      "actions": [{"text": "Send the draft.", "owner": None,
                                   "due": "Tomorrow", "evidence": [1]}]}
        self.meta = {"started": "2026-09-14T16:30:08Z", "ended": "2026-09-14T17:09:17Z",
                     "duration_seconds": 2349}
        self.route = {"company_name": "Example Co"}

    def render(self, **changes):
        return render_document(changes.get("notes", self.notes),
                               changes.get("transcript", self.transcript),
                               changes.get("metadata", self.meta), self.route,
                               changes.get("timezone_name", "America/Los_Angeles"),
                               changes.get("notes_layout", "sources"))

    def xml(self, payload, name="word/document.xml"):
        with zipfile.ZipFile(io.BytesIO(payload)) as package:
            return ET.fromstring(package.read(name))

    def test_document_names_every_matching_separated_segment_without_guessing_unknown(self):
        transcript = diarized_transcript()
        notes = {"schema_version": 1, "meeting_title": {"text": "Agenda planning", "evidence": [2],
                 "participants": [{"name": "Jamie", "evidence": [1]}]},
                 "summary": [{"text": "The agenda will be sent.", "evidence": [2]}], "actions": []}
        payload, _text, _title = self.render(notes=notes, transcript=transcript)
        text = "".join(self.xml(payload).itertext())
        self.assertIn("00:01 | Jamie  My name is Jamie.", text)
        self.assertIn("00:02 | Jamie  I will send the agenda.", text)
        self.assertIn("00:03 | Anna  I will prepare the slides.", text)
        self.assertIn("00:04 | System audio (unidentified)", text)
        transcript["speakers"]["system_speaker_1"]["name"] = "Max"
        with self.assertRaises(ValueError):
            self.render(notes=notes, transcript=transcript)

    def test_every_segment_preserved_in_order_even_identical_text(self):
        payload, expected, title = self.render()
        root = self.xml(payload)
        paragraphs = root.findall(".//w:p", NS)
        bookmarks = root.findall(".//w:bookmarkStart", NS)
        self.assertEqual([p.get("{" + W + "}name") for p in bookmarks],
                         ["segment_0", "segment_1", "segment_2", "segment_3"])
        transcript_paragraphs = [p for p in paragraphs if p.find("w:bookmarkStart", NS) is not None]
        self.assertEqual(len(transcript_paragraphs), len(self.transcript["segments"]))
        for paragraph, segment in zip(transcript_paragraphs, self.transcript["segments"]):
            # The second run is exactly the source text, with explicit OOXML
            # breaks and tabs instead of silently stripping whitespace.
            run = paragraph.findall("w:r", NS)[1]
            actual = "".join(node.text or "" if node.tag.endswith("}t") else
                             "\n" if node.tag.endswith("}br") else
                             "\t" if node.tag.endswith("}tab") else "" for node in run)
            self.assertEqual(actual, segment["text"])
        self.assertEqual(title, "Draft planning - Max")
        self.assertIn("Microphone", expected[-2])
        self.assertIn("System audio", expected[-1])
        self.assertNotIn("Max", expected[-1])

    def test_notes_metadata_order_and_unknown_owners(self):
        _, expected, _ = self.render()
        self.assertIn("September 14, 2026 at 09:30 AM PDT | 39 min 9 sec", expected)
        self.assertIn("Identified participants Max", expected)
        self.assertIn("Unassigned", expected)
        self.assertIn("Tomorrow", expected)
        self.assertLess(expected.index("Summary"), expected.index("Action items"))
        self.assertLess(expected.index("Action items"), expected.index("Decisions"))
        self.assertLess(expected.index("Decisions"), expected.index("Full transcript"))
        self.assertNotIn("Customer priorities", expected)
        self.assertTrue(any(item.startswith("Source 00:01") for item in expected))
        self.assertFalse(any("segment " in item for item in expected))

    def test_docx_is_deterministic_and_has_real_styles_and_no_title_rule(self):
        first, _, _ = self.render()
        second, _, _ = self.render()
        self.assertEqual(first, second)
        with zipfile.ZipFile(io.BytesIO(first)) as package:
            self.assertTrue(all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in package.infolist()))
        root = self.xml(first)
        styles = self.xml(first, "word/styles.xml")
        self.assertEqual(root.find("w:body/w:p/w:pPr/w:pStyle", NS).get("{" + W + "}val"), "Title")
        self.assertIsNone(styles.find(".//w:pBdr", NS))
        self.assertIsNone(root.find(".//w:pBdr", NS))
        self.assertIsNotNone(root.find(".//w:tblHeader", NS))
        self.assertEqual(len(root.findall(".//w:tr/w:trPr/w:cantSplit", NS)), 2)
        self.assertIsNotNone(root.find(".//w:pageBreakBefore", NS))
        self.assertEqual(len(root.findall(".//w:hyperlink", NS)), 2)
        paragraphs = root.findall("w:body/w:p", NS)
        for index, paragraph in enumerate(paragraphs):
            style = paragraph.find("w:pPr/w:pStyle", NS)
            if style is not None and style.get("{" + W + "}val") == "Source":
                self.assertIsNotNone(paragraphs[index - 1].find("w:pPr/w:keepNext", NS))

    def test_legacy_export_bytes_and_manifest_stay_unchanged(self):
        # Captured from the original v1 exporter before adding layout options.
        with tempfile.TemporaryDirectory() as temporary:
            legacy = build_document(self.notes, self.transcript, self.meta, self.route,
                                    temporary, "America/Los_Angeles")
            self.assertEqual(hashlib.sha256(legacy["path"].read_bytes()).hexdigest(),
                             "46b6a624a27c45225f9fdf8e77c066871571a32ef3e3c16bedeee8c01c9c3f43")
            serialized = json.dumps(legacy["manifest"], ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()
            self.assertEqual(hashlib.sha256(serialized).hexdigest(),
                             "a1e99e45bf35afeb85a20d3b9162027c678d07b775ac359959a4561763a7517f")
            self.assertNotIn("notes_layout", legacy["manifest"])
            explicit = build_document(self.notes, self.transcript, self.meta, self.route,
                                      temporary, "America/Los_Angeles", notes_layout="sources")
            self.assertEqual(legacy, explicit)

    def test_clean_notes_keep_content_and_exact_transcript_without_inline_sources(self):
        notes = copy.deepcopy(self.notes)
        for key in ("topics", "customer_priorities", "objections", "competitive_intel",
                    "next_steps", "deal_implications", "open_questions"):
            notes[key] = [{"text": "Finding for " + key, "evidence": [1]}]
        notes["follow_up_draft"] = {"subject": "Draft update", "body": "I will send the draft.",
                                    "evidence": [1]}
        legacy_payload, legacy_text, legacy_title = self.render(notes=notes)
        payload, text, title = self.render(notes=notes, notes_layout="clean")
        notes_text = text[:text.index("Full transcript")]
        self.assertFalse(any("Source " in paragraph for paragraph in notes_text))
        self.assertFalse(any("00:01" in paragraph for paragraph in notes_text))
        self.assertEqual(title, legacy_title)
        self.assertIn("Identified participants Max", notes_text)
        self.assertIn("Unassigned", notes_text)
        self.assertIn("Tomorrow", notes_text)
        self.assertIn("Subject Draft update", notes_text)
        self.assertIn("I will send the draft.", notes_text)
        for items in notes.values():
            if isinstance(items, list):
                for item in items:
                    self.assertIn(item["text"], notes_text)
        self.assertEqual(text[text.index("Full transcript"):],
                         legacy_text[legacy_text.index("Full transcript"):])
        root, legacy_root = self.xml(payload), self.xml(legacy_payload)
        self.assertEqual(root.findall(".//w:hyperlink", NS), [])
        transcript_xml = lambda tree: [ET.tostring(p) for p in tree.findall(".//w:p", NS)
                                       if p.find("w:bookmarkStart", NS) is not None]
        self.assertEqual(transcript_xml(root), transcript_xml(legacy_root))

    def test_clean_export_preserves_evidence_and_refuses_layout_change_on_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = build_document(self.notes, self.transcript, self.meta, self.route,
                                    temporary, notes_layout="clean")
            manifest = result["manifest"]
            self.assertEqual(manifest["notes_layout"], "clean")
            self.assertEqual(manifest["source_evidence"],
                             {"summary": [[1]], "decisions": [[1]], "actions": [[1]]})
            self.assertEqual(result, build_document(self.notes, self.transcript, self.meta,
                                                    self.route, temporary, notes_layout="clean"))
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                build_document(self.notes, self.transcript, self.meta, self.route, temporary)
        with tempfile.TemporaryDirectory() as temporary:
            build_document(self.notes, self.transcript, self.meta, self.route, temporary)
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                build_document(self.notes, self.transcript, self.meta, self.route, temporary,
                               notes_layout="clean")

    def test_clean_export_still_validates_evidence_and_layout(self):
        for key in ("summary", "actions", "decisions"):
            notes = copy.deepcopy(self.notes)
            notes[key][0]["evidence"] = [99]
            with self.subTest(key=key), self.assertRaisesRegex(WorkflowError, "Invalid transcript segment reference"):
                self.render(notes=notes, notes_layout="clean")
        for layout in (None, "", "minimal", True):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaisesRegex(ValueError, "notes layout"):
                    build_document(self.notes, self.transcript, self.meta, self.route,
                                   temporary, notes_layout=layout)
                self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_historical_notes_without_title_still_make_complete_document(self):
        notes = copy.deepcopy(self.notes)
        del notes["meeting_title"]
        _, text, title = self.render(notes=notes)
        self.assertEqual(title, "Meeting notes and transcript")
        self.assertFalse(any(item.startswith("Identified participants") for item in text))
        self.assertEqual(len([item for item in text if "| System audio" in item]), 2)

    def test_invalid_source_and_timezone_fail_before_writing(self):
        for change in ({"timezone_name": "Unknown/Nowhere"},
                       {"metadata": dict(self.meta, started="2026-09-14T09:30:00")},
                       {"metadata": dict(self.meta, duration_seconds=-1)}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.render(**change)
        for field, value in (("text", "bad\x00text"), ("start_ms", -1), ("end_ms", 0)):
            transcript = copy.deepcopy(self.transcript)
            transcript["segments"][3][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.render(transcript=transcript)

    def test_immutable_document_refuses_changed_notes_and_tampered_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = build_document(self.notes, self.transcript, self.meta, self.route, temporary)
            self.assertEqual(result["manifest"]["transcript_segments"], 4)
            again = build_document(self.notes, self.transcript, self.meta, self.route, temporary)
            self.assertEqual(result, again)
            altered = copy.deepcopy(self.notes)
            altered["summary"][0]["text"] = "Different finding."
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                build_document(altered, self.transcript, self.meta, self.route, temporary)
            result["path"].write_bytes(b"wrong")
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                build_document(self.notes, self.transcript, self.meta, self.route, temporary)

    def test_timestamp_is_readable_without_losing_source_order(self):
        self.assertEqual(timestamp(155858), "02:35")
        self.assertEqual(timestamp(3700000), "01:01:40")


if __name__ == "__main__":
    unittest.main()
