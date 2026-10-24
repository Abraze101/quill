"""Synthetic native template checks; no Drive, model, or real meeting access."""
import copy
import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.native_template import (
    DATE_CHARACTER, TOKENS, build_payload, document_text, expected_text,
    note_sections, plan_requests, style_signature, verify_document,
)
from quill_workflow.pipeline import WorkflowError
from quill_workflow.transcript_sources import SOURCE_NOTICE, source_label
from test_transcript_sources import diarized_transcript


def utf16(value):
    return len(value.encode("utf-16-le")) // 2


def _paragraphs(lines):
    content, index = [{"endIndex": 1, "sectionBreak": {"sectionStyle": {}}}], 1
    for text, style, bullet, date in lines:
        elements = []
        for value in re.split("(" + DATE_CHARACTER + ")", text):
            if not value:
                continue
            element = {"startIndex": index, "endIndex": index + utf16(value)}
            if value == DATE_CHARACTER:
                element["dateElement"] = {"dateElementProperties": copy.deepcopy(date)}
            else:
                element["textRun"] = {"content": value, "textStyle": {"weightedFontFamily": {"fontFamily": "Google Sans Flex"}}}
            elements.append(element)
            index += utf16(value)
        paragraph = {"elements": elements, "paragraphStyle": copy.deepcopy(style)}
        if bullet:
            paragraph["bullet"] = copy.deepcopy(bullet)
        content.append({"startIndex": elements[0]["startIndex"], "endIndex": index, "paragraph": paragraph})
    return content


def template_document():
    """Shared fixture for the renderer and native publisher tests."""
    style = {"namedStyleType": "NORMAL_TEXT", "direction": "LEFT_TO_RIGHT"}
    lines = []
    for token in TOKENS:
        prefix = {"{{QUILL_PARTICIPANTS}}": "Participants ", "{{QUILL_ATTACHMENTS}}": "Attachments "}.get(token, "")
        named_style = dict(style)
        if token == "{{QUILL_TITLE}}":
            named_style["namedStyleType"] = "HEADING_2"
        if token == "{{QUILL_DECISION_STATUS}}":
            named_style["namedStyleType"] = "SUBTITLE"
        bullet = {"listId": "list." + token, "nestingLevel": 0} if token in (
            "{{QUILL_DECISIONS}}", "{{QUILL_ACTIONS}}", "{{QUILL_DETAILS}}") else None
        lines.append((prefix + token + "\n", named_style, bullet, None))
    return {"documentId": "synthetic", "revisionId": "r1", "title": "Synthetic template", "tabs": [{
        "tabId": "t.synthetic", "title": "Notes", "body": {"content": _paragraphs(lines)},
        "namedStyles": {"styles": [{"namedStyleType": "NORMAL_TEXT", "textStyle": {
            "weightedFontFamily": {"fontFamily": "Google Sans Flex"}, "fontSize": {"magnitude": 12, "unit": "PT"}}}]},
        "lists": {bullet["listId"]: {"listProperties": {"nestingLevels": [{"glyphType": "BULLET"}]}}
                  for _, _, bullet, _ in lines if bullet}}]}


def filled_document(payload, template=None):
    """Model Docs' expansion without network; preserve native paragraph/list data."""
    result = copy.deepcopy(template or template_document())
    lines = []
    date = {"timestamp": payload["date"]["timestamp"],
            "locale": "en", "dateFormat": "DATE_FORMAT_MONTH_DAY_YEAR_ABBREVIATED", "timeFormat": "TIME_FORMAT_DISABLED"}
    for item in result["tabs"][0]["body"]["content"]:
        if "paragraph" not in item:
            continue
        para = item["paragraph"]
        value = "".join(element["textRun"]["content"] for element in para["elements"])
        for token, replacement in payload["replacements"].items():
            value = value.replace(token, DATE_CHARACTER if token == "{{QUILL_DATE}}" else replacement)
        lines.extend((text + "\n", para.get("paragraphStyle", {}), para.get("bullet"), date)
                     for text in value[:-1].split("\n"))
    result["tabs"][0]["body"]["content"] = _paragraphs(lines)
    for request in plan_requests(template or template_document(), payload):
        change = request.get("updateTextStyle")
        if not change:
            continue
        start, end = change["range"]["startIndex"], change["range"]["endIndex"]
        for item in result["tabs"][0]["body"]["content"]:
            paragraph = item.get("paragraph")
            if not paragraph:
                continue
            elements = []
            for element in paragraph["elements"]:
                run = element.get("textRun")
                a, b = element["startIndex"], element["endIndex"]
                if not run or a >= end or b <= start:
                    elements.append(element)
                    continue
                points = sorted({a, b, max(a, start), min(b, end)})
                encoded = run["content"].encode("utf-16-le")
                for left, right in zip(points, points[1:]):
                    piece = copy.deepcopy(element)
                    piece["startIndex"], piece["endIndex"] = left, right
                    piece["textRun"]["content"] = encoded[(left-a)*2:(right-a)*2].decode("utf-16-le")
                    if left >= start and right <= end:
                        piece["textRun"]["textStyle"].update(change["textStyle"])
                    elements.append(piece)
            paragraph["elements"] = elements
    result["revisionId"] = "r2"
    return result


class NativeTemplateTests(unittest.TestCase):
    def setUp(self):
        self.transcript = {"segments": [
            {"start_ms": 0, "end_ms": 900, "speaker": "me", "text": "Hi Max. 😀"},
            {"start_ms": 1000, "end_ms": 8000, "speaker": "them", "text": "Draft scope: Build a portal.\nKeep it private."},
            {"start_ms": 3600123, "end_ms": 3601900, "speaker": "other", "text": "Yes.  Exactly."}]}
        self.notes = {"schema_version": 1, "meeting_title": {"text": "Portal planning", "evidence": [1],
            "participants": [{"name": "Max", "evidence": [0]}]},
            "summary": [{"text": "Discussed the portal. 😀", "evidence": [1]}],
            "decisions": [{"text": "Keep it private.", "evidence": [1]}],
            "actions": [{"text": "Draft scope: Build a portal.", "owner": "Max", "due": "Tomorrow", "evidence": [1]}],
            "topics": [{"text": "Portal scope: Keep the draft private.", "evidence": [1]}],
            "next_steps": [{"text": "Review the portal.", "evidence": [1]}]}
        self.metadata = {"started": "2026-09-15T02:30:08Z", "ended": "2026-09-15T03:30:08Z"}
        self.route = {"company_name": "Example Co"}

    def payload(self, **values):
        return build_payload(values.get("notes", self.notes), values.get("transcript", self.transcript),
                             values.get("metadata", self.metadata), self.route, values.get("timezone", "America/Los_Angeles"))

    def test_new_source_label_notice_preserves_legacy_pending_payloads(self):
        legacy = self.payload()["replacements"]["{{QUILL_TRANSCRIPT}}"]
        self.assertNotIn(SOURCE_NOTICE, legacy)
        transcript = dict(self.transcript, speaker_labels="audio_source")
        payload = self.payload(transcript=transcript)
        self.assertEqual(payload["replacements"]["{{QUILL_TRANSCRIPT}}"], SOURCE_NOTICE + "\n\n" + legacy)
        self.assertEqual(payload["transcript_segments"], len(self.transcript["segments"]))
        self.assertTrue(verify_document(filled_document(payload), payload, template_document()))

    def test_track_labels_never_turn_names_or_metadata_into_voice_identity(self):
        for value in ("Max", "Speaker 1", None, {"name": "Max"}):
            with self.subTest(value=value):
                self.assertEqual(source_label(value), "Audio source unspecified")
        self.assertEqual(source_label("me"), "Microphone")
        self.assertEqual(source_label("them"), "System audio")
        transcript = copy.deepcopy(self.transcript)
        transcript["segments"][1]["speaker"] = "Max"
        text = self.payload(transcript=transcript)["replacements"]["{{QUILL_TRANSCRIPT}}"]
        self.assertIn("00:01 | Audio source unspecified", text)
        self.assertNotIn("| Max", text)

    def test_separated_names_are_reused_only_for_matching_voice_and_source(self):
        transcript = diarized_transcript()
        notes = {"schema_version": 1, "meeting_title": {"text": "Agenda planning", "evidence": [2],
                 "participants": [{"name": "Jamie", "evidence": [1]}]},
                 "summary": [{"text": "The agenda will be sent.", "evidence": [2]}], "actions": []}
        text = self.payload(notes=notes, transcript=transcript)["replacements"]["{{QUILL_TRANSCRIPT}}"]
        self.assertIn("00:01 | Jamie  My name is Jamie.", text)
        self.assertIn("00:02 | Jamie  I will send the agenda.", text)
        self.assertIn("00:03 | Anna  I will prepare the slides.", text)
        self.assertIn("00:04 | System audio (unidentified)", text)
        transcript["speakers"]["system_speaker_1"]["evidence"] = [0]
        with self.assertRaises(ValueError):
            self.payload(notes=notes, transcript=transcript)

    def test_payload_maps_every_section_without_inline_evidence(self):
        notes = copy.deepcopy(self.notes)
        for key in ("customer_priorities", "objections", "competitive_intel", "deal_implications", "open_questions"):
            notes[key] = [{"text": "Original " + key + " content.", "evidence": [1]}]
        notes["follow_up_draft"] = {"subject": "Portal draft", "body": "Original\nfollow-up body.", "evidence": [1]}
        payload = self.payload(notes=notes)
        self.assertEqual(payload["title"], "Portal planning - Max - 9/14/26")
        self.assertEqual(payload["date"]["day"], 14)
        text = "\n".join(value for key, value in payload["replacements"].items() if key != "{{QUILL_TRANSCRIPT}}")
        for key, values in notes.items():
            if isinstance(values, list):
                for item in values:
                    self.assertIn(item["text"], text)
        self.assertIn("[Max] Draft scope: Build a portal. Due: Tomorrow", text)
        self.assertIn("Next step: Review the portal.", text)
        self.assertIn("Follow-up draft: Portal draft\nOriginal\nfollow-up body.", text)
        self.assertNotIn("segment", text)
        self.assertNotIn("00:01", text)
        self.assertEqual(payload["replacements"]["{{QUILL_DECISION_STATUS}}"], "Aligned")

    def test_full_transcript_exact_order_text_labels_and_hours(self):
        value = self.payload()["replacements"]["{{QUILL_TRANSCRIPT}}"]
        self.assertEqual(value, "00:00 | Microphone  Hi Max. 😀\n00:01 | System audio  Draft scope: Build a portal.\nKeep it private.\n01:00:00 | Audio source unspecified  Yes.  Exactly.")

    def test_empty_decisions_and_missing_owner_do_not_invent_status(self):
        notes = copy.deepcopy(self.notes)
        notes["decisions"] = []
        notes["actions"][0]["owner"] = None
        notes["actions"][0]["due"] = None
        sections = note_sections(notes)
        self.assertEqual(sections["DECISIONS"], "No decisions recorded.")
        self.assertEqual(sections["DECISION_STATUS"], "Not recorded")
        self.assertIn("[Unassigned]", sections["ACTIONS"])
        self.assertNotIn("Due:", sections["ACTIONS"])

    def test_plan_is_tab_scoped_and_inserts_real_date(self):
        template, payload = template_document(), self.payload()
        requests = plan_requests(template, payload)
        replacements = [request["replaceAllText"] for request in requests if "replaceAllText" in request]
        self.assertEqual(len(replacements), len(TOKENS) - 1)
        self.assertTrue(all(item["tabsCriteria"] == {"tabIds": ["t.synthetic"]} for item in replacements))
        date = next(request["insertDate"] for request in requests if "insertDate" in request)
        self.assertEqual(date["location"], {"index": 1, "tabId": "t.synthetic"})
        self.assertNotIn("timeZoneId", date["dateElementProperties"])
        self.assertEqual(date["dateElementProperties"]["timestamp"], "2026-09-14T12:00:00Z")
        self.assertEqual(document_text(filled_document(payload)), expected_text(template, payload))
        self.assertTrue(verify_document(filled_document(payload), payload, template))

    def test_bold_label_ranges_use_utf16_after_expansion(self):
        payload = self.payload()
        text = expected_text(template_document(), payload)
        requests = plan_requests(template_document(), payload)
        bold = [request["updateTextStyle"]["range"] for request in requests
                if request.get("updateTextStyle", {}).get("textStyle") == {"bold": True}]
        labels = []
        for region in bold:
            encoded = text.encode("utf-16-le")
            labels.append(encoded[(region["startIndex"] - 1) * 2:(region["endIndex"] - 1) * 2].decode("utf-16-le"))
        self.assertIn("[Max]", labels)
        self.assertNotIn("[Max] Draft scope:", labels)
        self.assertFalse(any("Due:" in label for label in labels))
        self.assertIn("Portal scope:", labels)
        self.assertIn("Next step:", labels)

    def test_detail_labels_do_not_repeat_existing_category_or_generic_topics(self):
        notes = copy.deepcopy(self.notes)
        notes["topics"].append({"text": "Talked about another part of the portal.", "evidence": [1]})
        notes["deal_implications"] = [{"text": "Assessment: The portal remains private.", "evidence": [1]}]
        details = note_sections(notes)["DETAILS"]
        self.assertIn("Assessment: The portal remains private.", details)
        self.assertNotIn("Assessment: Assessment:", details)
        self.assertNotIn("Discussion:", details)
        self.assertIn("Talked about another part of the portal.", details)

    def test_duplicate_or_missing_or_nested_tokens_fail(self):
        for change in ("duplicate", "missing", "table", "tabs", "inline", "wrong_paragraph"):
            doc = template_document()
            content = doc["tabs"][0]["body"]["content"]
            if change in ("duplicate", "missing", "wrong_paragraph"):
                token = TOKENS[0]
                first = content[1]["paragraph"]["elements"][0]["textRun"]
                first["content"] = (token + token + "\n" if change == "duplicate" else
                                    "No date\n" if change == "missing" else "Date: " + token + "\n")
                # Rebuild indexes so failure comes from placeholder validation.
                lines = [("".join(e["textRun"]["content"] for e in p["paragraph"]["elements"]),
                          p["paragraph"].get("paragraphStyle", {}), p["paragraph"].get("bullet"), None) for p in content[1:]]
                doc["tabs"][0]["body"]["content"] = _paragraphs(lines)
            elif change == "table":
                content[1]["table"] = {}
                del content[1]["paragraph"]
            elif change == "tabs":
                doc["tabs"].append(copy.deepcopy(doc["tabs"][0]))
            else:
                doc["tabs"][0]["inlineObjects"] = {"object": {}}
            with self.subTest(change=change), self.assertRaises(ValueError):
                plan_requests(doc, self.payload())

    def test_bad_evidence_reserved_tokens_and_time_ranges_fail(self):
        notes = copy.deepcopy(self.notes)
        notes["summary"][0]["evidence"] = [99]
        with self.assertRaises(WorkflowError):
            self.payload(notes=notes)
        transcript = copy.deepcopy(self.transcript)
        transcript["segments"][1]["text"] += " {{QUILL_DATE}}"
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.payload(transcript=transcript)
        transcript = copy.deepcopy(self.transcript)
        transcript["segments"][1]["end_ms"] = 1
        with self.assertRaisesRegex(ValueError, "end precedes"):
            self.payload(transcript=transcript)

    def test_verify_rejects_missing_transcript_native_date_or_lists(self):
        payload, template = self.payload(), template_document()
        for change in ("text", "date", "list", "style", "font", "label"):
            doc = filled_document(payload)
            paragraphs = doc["tabs"][0]["body"]["content"]
            if change == "text":
                paragraphs[-1]["paragraph"]["elements"][0]["textRun"]["content"] = "omitted\n"
            elif change == "date":
                paragraphs[1]["paragraph"]["elements"][0]["dateElement"]["dateElementProperties"]["timestamp"] = "2026-09-16T02:30:08Z"
            elif change == "list":
                next(p["paragraph"] for p in paragraphs if "bullet" in p.get("paragraph", {})).pop("bullet")
            elif change == "style":
                paragraphs[2]["paragraph"]["paragraphStyle"]["namedStyleType"] = "NORMAL_TEXT"
            elif change == "font":
                doc["tabs"][0]["namedStyles"]["styles"][0]["textStyle"]["fontSize"]["magnitude"] = 99
            else:
                run = next(e["textRun"] for p in paragraphs if "paragraph" in p
                           for e in p["paragraph"]["elements"] if e.get("textRun", {}).get("textStyle", {}).get("bold"))
                run["textStyle"]["bold"] = False
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_document(doc, payload, template)

    def test_raw_api_and_connector_wrappers_supported(self):
        doc, payload = template_document(), self.payload()
        expected = expected_text(doc, payload)
        raw = copy.deepcopy(doc)
        tab = raw["tabs"][0]
        raw["tabs"][0] = {"tabProperties": {"tabId": tab["tabId"], "title": tab["title"]},
                          "documentTab": {key: value for key, value in tab.items() if key not in ("tabId", "title")}}
        self.assertEqual(expected_text(raw, payload), expected)
        self.assertEqual(expected_text({"structuredContent": doc}, payload), expected)
        self.assertEqual(expected_text({"structuredContent": {"document": doc}}, payload), expected)

    def test_style_signature_ignores_generated_heading_and_list_ids(self):
        original = template_document()
        changed = copy.deepcopy(original)
        tab = changed["tabs"][0]
        tab["tabId"] = "t.changed"
        tab["lists"] = {"new-" + key: value for key, value in tab["lists"].items()}
        for item in tab["body"]["content"]:
            para = item.get("paragraph", {})
            if "bullet" in para:
                para["bullet"]["listId"] = "new-" + para["bullet"]["listId"]
            para.setdefault("paragraphStyle", {})["headingId"] = "new-generated-id"
        self.assertEqual(style_signature(original), style_signature(changed))


if __name__ == "__main__":
    unittest.main()
