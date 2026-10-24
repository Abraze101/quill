"""Dependency-free, deterministic notes and full-transcript DOCX export.

The worker runs with macOS's Python, so authoring uses only the standard
library. Content is validated notes plus the exact ordered transcript; no
model call, speaker identification, source rewriting, or Drive action occurs.
"""
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .meeting_title import meeting_title
from .capture_quality import notice
from .transcript_sources import SOURCE_NOTICE, DIARIZED_NOTICE, speaker_attributions, segment_label

FORMAT = "combined_google_doc_v1"
VERSION = 1
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
ET.register_namespace("w", W)
ET.register_namespace("r", R)


def _el(parent, tag, text=None, **attrs):
    node = ET.SubElement(parent, "{" + W + "}" + tag,
                         {"{" + W + "}" + key: str(value) for key, value in attrs.items()})
    if text is not None:
        node.text = text
    return node


def _xml(node):
    return ET.tostring(node, encoding="utf-8", xml_declaration=True)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _checked_text(value):
    if not isinstance(value, str):
        raise ValueError("Document source text must be a string")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", value):
        raise ValueError("Document source contains an unsupported XML character")
    return value


def timestamp(milliseconds):
    if type(milliseconds) is not int or milliseconds < 0:
        raise ValueError("Transcript timestamps must be nonnegative integer milliseconds")
    seconds = milliseconds // 1000
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return (f"{hours:02d}:" if hours else "") + f"{minutes:02d}:{seconds:02d}"


def _date(value, zone):
    if not isinstance(value, str):
        raise ValueError("Completed recording needs a timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Meeting timestamps must include a timezone")
    return parsed.astimezone(zone)


def _duration(seconds):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Meeting duration must be a nonnegative number")
    seconds = round(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return ((str(hours) + " hr " if hours else "") +
            (str(minutes) + " min " if minutes else "") + str(seconds) + " sec")


def _evidence_label(refs, segments):
    ordered = sorted(refs, key=lambda index: segments[index]["start_ms"])
    first = timestamp(segments[ordered[0]]["start_ms"])
    last = timestamp(segments[ordered[-1]]["start_ms"])
    return first + (" to " + last if first != last else ""), ordered[0]


def _styles():
    root = ET.Element("{" + W + "}styles")
    defaults = _el(root, "docDefaults")
    run = _el(_el(defaults, "rPrDefault"), "rPr")
    _el(run, "rFonts", ascii="Arial", hAnsi="Arial", cs="Arial", eastAsia="Arial")
    _el(run, "sz", val=22)
    _el(run, "color", val="000000")
    para = _el(_el(defaults, "pPrDefault"), "pPr")
    _el(para, "spacing", after=120, line=264, lineRule="auto")
    _el(para, "widowControl")
    for name, size, before, after, bold, outline in (
            ("Normal", 22, 0, 120, False, None),
            ("Title", 48, 0, 200, False, None),
            ("Heading1", 30, 240, 120, True, 0),
            ("Heading2", 24, 180, 100, True, 1),
            ("Metadata", 20, 0, 80, False, None),
            ("Source", 18, 0, 120, False, None),
            ("Transcript", 22, 0, 90, False, None),
            ("TableBody", 20, 0, 40, False, None)):
        style = _el(root, "style", type="paragraph", styleId=name)
        if name == "Normal":
            style.set("{" + W + "}default", "1")
        _el(style, "name", val={"Heading1": "heading 1", "Heading2": "heading 2"}.get(name, name))
        if name != "Normal":
            _el(style, "basedOn", val="Normal")
        _el(style, "next", val="Normal")
        p = _el(style, "pPr")
        _el(p, "spacing", before=before, after=after, line=264, lineRule="auto")
        if name in ("Title", "Heading1", "Heading2"):
            _el(p, "keepNext")
            _el(p, "keepLines")
        if outline is not None:
            _el(p, "outlineLvl", val=outline)
        props = _el(style, "rPr")
        _el(props, "sz", val=size)
        _el(props, "color", val="000000")
        if bold:
            _el(props, "b")
    return _xml(root)


class _Document:
    def __init__(self):
        self.root = ET.Element("{" + W + "}document")
        self.body = _el(self.root, "body")
        self.text = []

    def run(self, para, value, bold=False, size=None):
        value = _checked_text(value)
        run = _el(para, "r")
        if bold or size:
            props = _el(run, "rPr")
            if bold:
                _el(props, "b")
            if size:
                _el(props, "sz", val=size)
        for part in re.split(r"(\n|\t)", value):
            if part == "\n":
                _el(run, "br")
            elif part == "\t":
                _el(run, "tab")
            elif part:
                text = _el(run, "t", text=part)
                text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        return run

    def paragraph(self, value="", style="Normal", parent=None, bookmark=None, page_break=False,
                  prefix=None, keep_next=False):
        p = _el(self.body if parent is None else parent, "p")
        props = _el(p, "pPr")
        _el(props, "pStyle", val=style)
        if page_break:
            _el(props, "pageBreakBefore")
        if keep_next:
            _el(props, "keepNext")
        if bookmark is not None:
            _el(p, "bookmarkStart", id=bookmark, name="segment_" + str(bookmark))
        if prefix:
            self.run(p, prefix, bold=True, size=20)
        self.run(p, value)
        if bookmark is not None:
            _el(p, "bookmarkEnd", id=bookmark)
        self.text.append((prefix or "") + value)
        return p

    def evidence(self, refs, segments):
        p = self.paragraph("Source ", "Source")
        label, index = _evidence_label(refs, segments)
        link = _el(p, "hyperlink", anchor="segment_" + str(index), history=1)
        self.run(link, label, size=18)
        self.text[-1] += label

    def actions(self, actions, segments, notes_layout="sources"):
        self.paragraph("Action items", "Heading1")
        table = _el(self.body, "tbl")
        props = _el(table, "tblPr")
        _el(props, "tblW", w=9360, type="dxa")
        _el(props, "tblLayout", type="fixed")
        borders = _el(props, "tblBorders")
        for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
            _el(borders, side, val="single", sz=4, color="D9D9D9")
        margins = _el(props, "tblCellMar")
        for side in ("top", "bottom", "left", "right"):
            _el(margins, side, w=100, type="dxa")
        grid = _el(table, "tblGrid")
        widths = [5760, 1500, 2100]
        for width in widths:
            _el(grid, "gridCol", w=width)
        values = [["Action", "Owner", "Due"]]
        for item in actions:
            action_text = item["text"]
            if notes_layout == "sources":
                refs, _ = _evidence_label(item["evidence"], segments)
                action_text += "\nSource " + refs
            values.append([action_text,
                           item.get("owner") or "Unassigned", item.get("due") or "Not stated"])
        for number, row in enumerate(values):
            tr = _el(table, "tr")
            row_props = _el(tr, "trPr")
            _el(row_props, "cantSplit")
            if number == 0:
                _el(row_props, "tblHeader")
            for column, value in enumerate(row):
                cell = _el(tr, "tc")
                cell_props = _el(cell, "tcPr")
                _el(cell_props, "tcW", w=widths[column], type="dxa")
                _el(cell_props, "vAlign", val="center")
                if number == 0:
                    _el(cell_props, "shd", fill="EDEDED", val="clear")
                p = self.paragraph(value, "TableBody", parent=cell)
                if number == 0:
                    for run in p.findall("{" + W + "}r"):
                        properties = ET.Element("{" + W + "}rPr")
                        _el(properties, "b")
                        run.insert(0, properties)
        self.paragraph("")

    def finish(self):
        section = _el(self.body, "sectPr")
        _el(section, "pgSz", w=12240, h=15840)
        _el(section, "pgMar", top=1080, bottom=1080, left=1440, right=1440,
            header=720, footer=720, gutter=0)
        return _xml(self.root)


def render_document(notes, transcript, metadata, route, timezone_name="UTC", notes_layout="sources"):
    """Return deterministic DOCX bytes, ordered expected text, and display title."""
    if notes_layout not in ("sources", "clean"):
        raise ValueError("Document notes layout must be sources or clean")
    from .pipeline import render_notes  # Validate every note and evidence reference.
    render_notes(notes, transcript)
    attributions = speaker_attributions(transcript)
    if not isinstance(metadata, dict) or not isinstance(route, dict):
        raise ValueError("Document metadata and approved company route are required")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("Document timezone must be an IANA timezone")
    try:
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("Unknown document timezone") from error
    started = _date(metadata.get("started"), zone)
    ended = _date(metadata.get("ended"), zone)
    if ended < started:
        raise ValueError("Meeting end precedes its start")
    title = meeting_title(notes, transcript) or "Meeting notes and transcript"
    segments = transcript["segments"]
    doc = _Document()
    doc.paragraph(title, "Title")
    doc.paragraph("Meeting notes and transcript", "Metadata")
    doc.paragraph(started.strftime("%B %d, %Y at %I:%M %p %Z") + " | " +
                  _duration(metadata.get("duration_seconds", (ended - started).total_seconds())), "Metadata")
    company = route.get("company_name")
    if not isinstance(company, str) or not company.strip():
        raise ValueError("Document needs an approved company name")
    doc.paragraph("Company " + company, "Metadata")
    participants = notes.get("meeting_title", {}).get("participants", [])
    if participants:
        doc.paragraph("Identified participants " + ", ".join(person["name"] for person in participants), "Metadata")
    capture_notice = notice(transcript)
    if capture_notice:
        doc.paragraph(capture_notice)

    def section(key, heading):
        items = notes.get(key, [])
        if items:
            doc.paragraph(heading, "Heading1")
            for item in items:
                doc.paragraph(item["text"], keep_next=notes_layout == "sources")
                if notes_layout == "sources":
                    doc.evidence(item["evidence"], segments)

    section("summary", "Summary")
    if notes.get("actions"):
        doc.actions(notes["actions"], segments, notes_layout)
    for key, heading in (("decisions", "Decisions"), ("topics", "Discussion points"),
                         ("customer_priorities", "Customer priorities"),
                         ("objections", "Concerns and objections"),
                         ("competitive_intel", "Competitor mentions"),
                         ("next_steps", "Agreed next steps"),
                         ("deal_implications", "Assessment"), ("open_questions", "Open questions")):
        section(key, heading)
    if notes.get("follow_up_draft"):
        draft = notes["follow_up_draft"]
        doc.paragraph("Follow up draft", "Heading1")
        doc.paragraph("Subject " + draft["subject"])
        doc.paragraph(draft["body"], keep_next=notes_layout == "sources")
        if notes_layout == "sources":
            doc.evidence(draft["evidence"], segments)
    doc.paragraph("Full transcript", "Heading1", page_break=True)
    doc.paragraph("Timestamps are relative to the recording. " + (SOURCE_NOTICE if attributions is None else DIARIZED_NOTICE))
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError("Every transcript segment must be an object")
        text = _checked_text(segment.get("text"))
        start = timestamp(segment.get("start_ms"))
        end = timestamp(segment["end_ms"]) if "end_ms" in segment else None
        if end and segment["end_ms"] < segment["start_ms"]:
            raise ValueError("Transcript segment end precedes its start")
        source = segment_label(segment, attributions)
        label = start + " | " + source + "  "
        doc.paragraph(text, "Transcript", bookmark=index, prefix=label)
    files = {
        "[Content_Types].xml": b'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>',
        "_rels/.rels": b'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/_rels/document.xml.rels": b'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        "word/styles.xml": _styles(), "word/document.xml": doc.finish(),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name in sorted(files):
            item = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o600 << 16
            package.writestr(item, files[name])
    return buffer.getvalue(), doc.text, title


def build_document(notes, transcript, metadata, route, output_dir, timezone_name="UTC", notes_layout="sources"):
    """Save immutable export and digest manifest; retries refuse changed inputs."""
    from .pipeline import atomic_json
    payload, text, title = render_document(notes, transcript, metadata, route, timezone_name, notes_layout)
    output_dir = Path(output_dir)
    if output_dir.is_symlink():
        raise ValueError("Document output must not be a symlink")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "meeting.docx"
    manifest_path = output_dir / "manifest.json"
    inputs = {"notes": notes, "transcript": transcript, "metadata": metadata,
              "route": route, "timezone": timezone_name}
    # Existing v1 exports must retain their exact identity on retry. Only the
    # opt-in clean layout extends the historical provenance shape.
    if notes_layout == "clean":
        inputs["notes_layout"] = notes_layout
    manifest = {"schema_version": VERSION, "format": FORMAT,
                "inputs_sha256": _digest(inputs),
                "docx_sha256": hashlib.sha256(payload).hexdigest(),
                "title": title, "timezone": timezone_name,
                "transcript_segments": len(transcript["segments"]),
                "source_evidence": {key: [item["evidence"] for item in items]
                                    for key, items in notes.items()
                                    if isinstance(items, list) and items
                                    and all(isinstance(item, dict) and "evidence" in item for item in items)},
                "paragraphs": text}
    if notes_layout == "clean":
        manifest["notes_layout"] = notes_layout
    if path.is_symlink() or manifest_path.is_symlink():
        raise ValueError("Document outputs must not be symlinks")
    if path.exists() and path.read_bytes() != payload:
        raise ValueError("Combined document inputs changed; an explicit revision is required")
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
        raise ValueError("Combined document provenance changed; an explicit revision is required")
    if not path.exists():
        descriptor, temporary = tempfile.mkstemp(prefix=".meeting-", dir=output_dir)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    atomic_json(manifest_path, manifest)
    return {"path": path, "title": title, "text": text, "manifest": manifest}
