"""Validated meeting content for a copied, styled native Google Docs template.

This module performs no network or file operations. Source evidence is checked
before rendering but remains in the source JSON, rather than reader-facing notes.
"""
from datetime import datetime, timezone as utc_timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .meeting_document import timestamp
from .meeting_title import meeting_title
from .capture_quality import with_summary_notice
from .transcript_sources import SOURCE_NOTICE, DIARIZED_NOTICE, speaker_attributions, segment_label


FORMAT = "native_template_google_doc_v1"
DATE_CHARACTER = "\ufffc"
TOKENS = tuple("{{QUILL_" + name + "}}" for name in (
    "DATE", "TITLE", "PARTICIPANTS", "ATTACHMENTS", "SUMMARY", "DECISIONS",
    "DECISION_STATUS", "ACTIONS", "DETAILS", "TRANSCRIPT"))
_TOKEN = re.compile(r"\{\{QUILL_[A-Z_]+\}\}")
_DETAILS = (("topics", "Discussion"), ("customer_priorities", "Customer priorities"),
            ("objections", "Concerns and objections"), ("competitive_intel", "Competitor mentions"),
            ("deal_implications", "Assessment"), ("open_questions", "Open questions"))


def _text(value):
    if not isinstance(value, str):
        raise ValueError("Native document content must be text")
    if re.search(r"[\x00-\x08\x0b-\x1f\ud800-\udfff\ufffe\uffff\ufffc]", value):
        raise ValueError("Native document source contains unsupported control characters")
    if "{{QUILL_" in value:
        raise ValueError("Native document source contains a reserved template token")
    return value


def _utf16(value):
    return len(value.encode("utf-16-le")) // 2


def note_sections(notes):
    """Pure presentation mapping; callers validate evidence before using it."""
    actions = []
    for item in notes.get("actions", []):
        line = "[" + (item.get("owner") or "Unassigned") + "] " + item["text"]
        if item.get("due"):
            line += " Due: " + item["due"]
        actions.append(line)
    actions.extend("Next step: " + item["text"] for item in notes.get("next_steps", []))
    details = []
    for key, label in _DETAILS:
        for item in notes.get(key, []):
            value = item["text"]
            # Keep grounded topic wording; do not add repetitive generic
            # labels or duplicate a category already present in source text.
            if key == "topics" or value.casefold().startswith(label.casefold() + ":"):
                details.append(value)
            else:
                details.append(label + ": " + value)
    draft = notes.get("follow_up_draft")
    if draft:
        details.append("Follow-up draft: " + draft["subject"] + "\n" + draft["body"])
    return {
        "SUMMARY": "\n\n".join(item["text"] for item in notes.get("summary", [])) or "No summary recorded.",
        "DECISIONS": "\n".join(item["text"] for item in notes.get("decisions", [])) or "No decisions recorded.",
        "DECISION_STATUS": "Aligned" if notes.get("decisions") else "Not recorded",
        "ACTIONS": "\n".join(actions) or "No next steps recorded.",
        "DETAILS": "\n".join(details) or "No additional details recorded.",
    }


def build_payload(notes, transcript, metadata, route, timezone="UTC"):
    """Map every validated note section and the exact transcript into native slots."""
    from .pipeline import render_notes
    render_notes(notes, transcript)
    attributions = speaker_attributions(transcript)
    if not isinstance(metadata, dict) or not isinstance(route, dict):
        raise ValueError("Meeting metadata and approved company route are required")
    if not isinstance(timezone, str) or not timezone:
        raise ValueError("Document timezone must be an IANA timezone")
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("Unknown document timezone") from error

    def meeting_time(key):
        value = metadata.get(key)
        if not isinstance(value, str):
            raise ValueError("Meeting metadata needs start and end timestamps")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Meeting timestamps must include a timezone")
        return parsed.astimezone(zone)

    started, ended = meeting_time("started"), meeting_time("ended")
    if ended < started:
        raise ValueError("Meeting end precedes its start")
    company = _text(route.get("company_name"))
    if not company.strip():
        raise ValueError("Document needs an approved company name")
    heading = meeting_title(notes, transcript) or "Meeting notes and transcript"
    short_date = f"{started.month}/{started.day}/{started.year % 100:02d}"
    participants = notes.get("meeting_title", {}).get("participants", [])
    transcript_lines = []
    for segment in transcript["segments"]:
        if not isinstance(segment, dict):
            raise ValueError("Every transcript segment must be an object")
        value = _text(segment.get("text"))
        start = timestamp(segment.get("start_ms"))
        if "end_ms" in segment:
            timestamp(segment["end_ms"])
            if segment["end_ms"] < segment["start_ms"]:
                raise ValueError("Transcript segment end precedes its start")
        source = segment_label(segment, attributions)
        transcript_lines.append(start + " | " + source + "  " + value)
    # Older payloads must stay byte-for-byte compatible with pending Drive
    # write reconciliation. New captures explicitly declare source-only labels.
    transcript_text = "\n".join(transcript_lines)
    if transcript.get("speaker_labels") == "audio_source":
        transcript_text = SOURCE_NOTICE + "\n\n" + transcript_text
    elif attributions is not None:
        transcript_text = DIARIZED_NOTICE + "\n\n" + transcript_text
    values = {
        "DATE": short_date,
        "TITLE": heading,
        "PARTICIPANTS": ", ".join(person["name"] for person in participants) or "Not identified in the transcript",
        "ATTACHMENTS": "Full transcript below; audio saved locally.",
        **with_summary_notice(note_sections(notes), transcript),
        "TRANSCRIPT": transcript_text,
    }
    replacements = {"{{QUILL_" + name + "}}": _text(value) for name, value in values.items()}
    return {"format": FORMAT, "title": _text(heading + " - " + short_date),
            "replacements": replacements,
            "date": {"year": started.year, "month": started.month, "day": started.day,
                     # Date-only chips reject timeZoneId. Encode the already
                     # known local calendar date in UTC with time hidden.
                     "timestamp": f"{started.year:04d}-{started.month:02d}-{started.day:02d}T12:00:00Z",
                     "source_timestamp": started.astimezone(utc_timezone.utc).isoformat().replace("+00:00", "Z"),
                     "timezone": timezone},
            "company_name": company, "transcript_segments": len(transcript_lines)}


def _document(document):
    if not isinstance(document, dict):
        raise ValueError("Native document must be an object")
    value = document.get("structuredContent", document)
    if isinstance(value, dict) and "document" in value:
        value = value["document"]
    if not isinstance(value, dict):
        raise ValueError("Native document response is unsupported")
    return value


def _body(document):
    document = _document(document)
    tabs = document.get("tabs")
    if not isinstance(tabs, list) or len(tabs) != 1:
        raise ValueError("Native notes template must contain exactly one tab")
    tab = tabs[0]
    if tab.get("childTabs") or tab.get("parentTabId"):
        raise ValueError("Nested native template tabs are unsupported")
    props = tab.get("tabProperties", tab)
    tab_id = props.get("tabId")
    body = tab.get("documentTab", tab)
    if not isinstance(tab_id, str) or not tab_id or not isinstance(body, dict):
        raise ValueError("Native template tab has no readable identity")
    for scope in (document, body):
        if any(scope.get(key) for key in ("headers", "footers", "footnotes", "inlineObjects", "positionedObjects")):
            raise ValueError("Native notes template contains unsupported objects or secondary content")
    content = body.get("body", {}).get("content")
    if not isinstance(content, list):
        raise ValueError("Native template has no readable body")
    paragraphs, index = [], 1
    for item in content:
        if "sectionBreak" in item:
            if paragraphs or item.get("endIndex") != 1:
                raise ValueError("Native notes template contains an unsupported section break")
            continue
        paragraph = item.get("paragraph")
        if not isinstance(paragraph, dict) or item.get("startIndex") != index:
            raise ValueError("Native template body must contain contiguous paragraphs")
        value = ""
        dates = []
        for element in paragraph.get("elements", []):
            if element.get("startIndex") != index:
                raise ValueError("Native paragraph element indexes are inconsistent")
            if "textRun" in element:
                part = element["textRun"].get("content")
                if not isinstance(part, str):
                    raise ValueError("Native text run has no content")
            elif "dateElement" in element:
                part = DATE_CHARACTER
                dates.append(element["dateElement"])
            else:
                raise ValueError("Native notes template contains an unsupported paragraph element")
            index += _utf16(part)
            if element.get("endIndex") != index:
                raise ValueError("Native paragraph element UTF-16 length is inconsistent")
            value += part
        if not value.endswith("\n") or item.get("endIndex") != index:
            raise ValueError("Native template paragraph must end in a newline")
        paragraphs.append({"text": value, "start": item["startIndex"], "paragraph": paragraph, "dates": dates})
    if not paragraphs:
        raise ValueError("Native template body is empty")
    return tab_id, paragraphs


def document_text(document):
    """Full body text, representing every native date chip as one code unit."""
    return "".join(item["text"] for item in _body(document)[1])


def _tab_data(document):
    document = _document(document)
    tab = document["tabs"][0]
    return document, tab.get("documentTab", tab)


def _without_generated_ids(value):
    if isinstance(value, dict):
        return {key: _without_generated_ids(item) for key, item in value.items()
                if key not in ("headingId", "listId", "documentId", "namedRangeId", "rangeId")}
    if isinstance(value, list):
        return [_without_generated_ids(item) for item in value]
    return value


def style_signature(document):
    """Compare native copy styles without depending on regenerated object IDs."""
    _, paragraphs = _body(document)
    outer, tab = _tab_data(document)
    lists = tab.get("lists") or outer.get("lists") or {}
    styles = []
    for item in paragraphs:
        paragraph = item["paragraph"]
        bullet = paragraph.get("bullet")
        definition = None
        if bullet:
            definition = lists.get(bullet.get("listId"))
            if definition is None:
                raise ValueError("Native list has no readable style definition")
        styles.append({"paragraph": _without_generated_ids(paragraph.get("paragraphStyle", {})),
                       "bullet": _without_generated_ids(bullet), "list": _without_generated_ids(definition),
                       "runs": [{"text": element.get("textRun", {}).get("content", DATE_CHARACTER),
                                 "style": element.get("textRun", element.get("dateElement", {})).get("textStyle", {})}
                                for element in paragraph.get("elements", [])]})
    return {"documentStyle": tab.get("documentStyle") or outer.get("documentStyle"),
            "namedStyles": tab.get("namedStyles") or outer.get("namedStyles"), "paragraphs": styles}


def _payload(payload):
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise ValueError("Unsupported native document payload format")
    replacements = payload.get("replacements")
    if not isinstance(replacements, dict) or set(replacements) != set(TOKENS):
        raise ValueError("Native payload must supply every supported template token")
    for value in replacements.values():
        _text(value)
        if not value:
            raise ValueError("Native template replacements must not be empty")
    date = payload.get("date", {})
    try:
        if any(type(date[key]) is not int for key in ("year", "month", "day")):
            raise ValueError("Native payload date components must be integers")
        zone = ZoneInfo(date["timezone"])
        instant = datetime.fromisoformat(date["timestamp"].replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("Date chip timestamp requires a timezone")
        local = instant.astimezone(utc_timezone.utc)
        if (local.year, local.month, local.day) != (date["year"], date["month"], date["day"]):
            raise ValueError("Date chip timestamp does not match the meeting date")
        if "source_timestamp" in date:
            source = datetime.fromisoformat(date["source_timestamp"].replace("Z", "+00:00"))
            if source.tzinfo is None:
                raise ValueError("Original meeting start requires a timezone")
            source = source.astimezone(zone)
            if (source.year, source.month, source.day) != (date["year"], date["month"], date["day"]):
                raise ValueError("Date chip disagrees with the original meeting date")
    except (KeyError, TypeError, ZoneInfoNotFoundError) as error:
        raise ValueError("Native payload date is invalid") from error
    return replacements


def _template(document, payload):
    replacements = _payload(payload)
    tab_id, paragraphs = _body(document)
    text = "".join(item["text"] for item in paragraphs)
    if any(item["dates"] for item in paragraphs):
        raise ValueError("Unfilled native template must use the date token")
    if set(_TOKEN.findall(text)) != set(TOKENS) or any(text.count(token) != 1 for token in TOKENS):
        raise ValueError("Every native template token must occur exactly once")
    token_paragraphs = {}
    for item in paragraphs:
        found = _TOKEN.findall(item["text"])
        if not found:
            continue
        token = found[0]
        prefix = {"{{QUILL_PARTICIPANTS}}": "Participants ", "{{QUILL_ATTACHMENTS}}": "Attachments "}.get(token, "")
        if len(found) != 1 or item["text"] != prefix + token + "\n":
            raise ValueError("Each native token must occupy its own template paragraph")
        token_paragraphs[token] = item
    return tab_id, text, paragraphs, token_paragraphs, replacements


def _expanded(document, payload):
    tab_id, text, paragraphs, token_paragraphs, replacements = _template(document, payload)
    pieces, spans, last, offset = [], {}, 0, 1
    for match in _TOKEN.finditer(text):
        before = text[last:match.start()]
        pieces.append(before)
        offset += _utf16(before)
        token = match.group()
        value = DATE_CHARACTER if token == "{{QUILL_DATE}}" else replacements[token]
        spans[token] = (offset, offset + _utf16(value))
        pieces.append(value)
        offset += _utf16(value)
        last = match.end()
    pieces.append(text[last:])
    return tab_id, "".join(pieces), spans, paragraphs, token_paragraphs


def expected_text(document, payload):
    return _expanded(document, payload)[1]


def plan_requests(document, payload):
    """Fill a native copy while preserving paragraph, font, and list styling."""
    tab_id, text, spans, _, _ = _expanded(document, payload)
    replacements = payload["replacements"]
    requests = []
    for token in TOKENS:
        if token != "{{QUILL_DATE}}":
            requests.append({"replaceAllText": {
                "containsText": {"text": token, "matchCase": True},
                "replaceText": replacements[token], "tabsCriteria": {"tabIds": [tab_id]}}})
    start, _ = spans["{{QUILL_DATE}}"]
    # All other replacements precede this operation, so this is the final
    # offset; the placeholder itself still occupies its original token width.
    requests.append({"deleteContentRange": {"range": {"tabId": tab_id, "startIndex": start,
                                                       "endIndex": start + _utf16("{{QUILL_DATE}}")}}})
    requests.append({"insertDate": {"location": {"index": start, "tabId": tab_id},
                                    "dateElementProperties": {
                                        "timestamp": payload["date"]["timestamp"],
                                        "locale": "en",
                                        "dateFormat": "DATE_FORMAT_MONTH_DAY_YEAR_ABBREVIATED",
                                        "timeFormat": "TIME_FORMAT_DISABLED"}}})
    # Replacing a token inherits its first character's bold style. Normalize
    # dynamic prose, then restore only grounded leading labels, like the source.
    for token in ("{{QUILL_SUMMARY}}", "{{QUILL_DECISIONS}}", "{{QUILL_ACTIONS}}", "{{QUILL_DETAILS}}", "{{QUILL_TRANSCRIPT}}"):
        begin, end = spans[token]
        requests.append({"updateTextStyle": {"range": {"tabId": tab_id, "startIndex": begin, "endIndex": end},
                                              "textStyle": {"bold": False}, "fields": "bold"}})
        if token not in ("{{QUILL_ACTIONS}}", "{{QUILL_DETAILS}}"):
            continue
        line_offset = begin
        for line in replacements[token].splitlines(keepends=True):
            if token == "{{QUILL_ACTIONS}}":
                match = re.match(r"^(?:\[[^\]\n]+\]|Next step:)", line)
            else:
                match = re.match(r"^[^:\n]{1,140}:\s", line)
            if match:
                label = line[:match.end()].rstrip()
                requests.append({"updateTextStyle": {"range": {"tabId": tab_id, "startIndex": line_offset,
                                                                  "endIndex": line_offset + _utf16(label)},
                                                      "textStyle": {"bold": True}, "fields": "bold"}})
            line_offset += _utf16(line)
    return requests


def verify_document(document, payload, template_document):
    """Verify exact content, native date semantics, and preserved paragraph/list styles."""
    template_id, expected, _, original, _ = _expanded(template_document, payload)
    tab_id, actual = _body(document)
    if tab_id != template_id or "".join(item["text"] for item in actual) != expected:
        raise ValueError("Native document readback differs from the expected complete content")
    source_style, result_style = style_signature(template_document), style_signature(document)
    for key in ("documentStyle", "namedStyles"):
        if source_style[key] != result_style[key]:
            raise ValueError("Native document named styles or page setup changed")
    source_outer, source_tab = _tab_data(template_document)
    result_outer, result_tab = _tab_data(document)
    source_lists = source_tab.get("lists") or source_outer.get("lists") or {}
    result_lists = result_tab.get("lists") or result_outer.get("lists") or {}
    for item in original:
        bullet = item["paragraph"].get("bullet")
        if bullet and source_lists.get(bullet["listId"]) != result_lists.get(bullet["listId"]):
            raise ValueError("Native document list glyph or indentation changed")
    date_elements = [date for item in actual for date in item["dates"]]
    if len(date_elements) != 1:
        raise ValueError("Native document must retain exactly one meeting date chip")
    props = date_elements[0].get("dateElementProperties", {})
    try:
        instant = datetime.fromisoformat(props["timestamp"].replace("Z", "+00:00"))
        expected_instant = datetime.fromisoformat(payload["date"]["timestamp"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Native date chip readback is invalid") from error
    if (instant != expected_instant or props.get("timeZoneId", "etc/UTC") not in ("UTC", "etc/UTC", "Etc/UTC", None, "")
            or props.get("timeFormat", "TIME_FORMAT_DISABLED") != "TIME_FORMAT_DISABLED"):
        raise ValueError("Native date chip does not match the requested meeting date")
    # A newline inserted into a styled paragraph must retain that paragraph's
    # style and list membership; no flattening into generic body text is accepted.
    expanded_paragraphs = []
    for item in original:
        value = _TOKEN.sub(lambda match: DATE_CHARACTER if match.group() == "{{QUILL_DATE}}"
                           else payload["replacements"][match.group()], item["text"])
        expanded_paragraphs.extend((part + "\n", item["paragraph"]) for part in value[:-1].split("\n"))
    if len(expanded_paragraphs) != len(actual):
        raise ValueError("Native document paragraph topology differs from its template")
    for (value, source), result in zip(expanded_paragraphs, actual):
        paragraph = result["paragraph"]
        if (result["text"] != value or _without_generated_ids(paragraph.get("paragraphStyle", {})) != _without_generated_ids(source.get("paragraphStyle", {}))
                or paragraph.get("bullet") != source.get("bullet")):
            raise ValueError("Native document paragraph or list style changed")
        source_first = source["elements"][0].get("textRun", {}).get("textStyle", {})
        for element in paragraph.get("elements", []):
            run = element.get("textRun")
            if not run or run.get("content") == "\n":
                continue
            # Bold is intentionally reset and restored per grounded labels;
            # all explicit source font family, size, and color remain native.
            for key in ("weightedFontFamily", "fontSize", "foregroundColor"):
                if key in source_first and run.get("textStyle", {}).get(key) != source_first[key]:
                    raise ValueError("Native document text font or color changed")
    style_requests = [request["updateTextStyle"] for request in plan_requests(template_document, payload)
                      if "updateTextStyle" in request]
    bold_ranges = [(change["range"]["startIndex"], change["range"]["endIndex"])
                   for change in style_requests if change["textStyle"] == {"bold": True}]
    for change in style_requests:
        if change["textStyle"] != {"bold": True}:
            continue
        start, end = change["range"]["startIndex"], change["range"]["endIndex"]
        for item in actual:
            for element in item["paragraph"]["elements"]:
                if element["startIndex"] < end and element["endIndex"] > start:
                    if element.get("textRun", {}).get("textStyle", {}).get("bold") is not True:
                        raise ValueError("Native document leading label lost its bold style")
    for change in style_requests:
        if change["textStyle"] != {"bold": False}:
            continue
        start, end = change["range"]["startIndex"], change["range"]["endIndex"]
        for item in actual:
            for element in item["paragraph"]["elements"]:
                run = element.get("textRun", {})
                if run.get("content") == "\n" or run.get("textStyle", {}).get("bold") is not True:
                    continue
                left, right = max(start, element["startIndex"]), min(end, element["endIndex"])
                if left < right and not any(a <= left and b >= right for a, b in bold_ranges):
                    raise ValueError("Native document prose has unexpected bold styling")
    return True
