"""Validate separated speakers and names without treating a track as a person."""
import copy
import hashlib
import json
import re
import unicodedata

SOURCE_NOTICE = "Microphone and system audio identify audio sources, not individual speakers."
DIARIZED_NOTICE = "Speakers are separated automatically. Names come from clear self-introductions; other voices keep anonymous labels."
REVIEW_NOTICE = "Speaker names were reviewed from this conversation and any available visible meeting names. Contextual matches are marked (suggested); they are not confirmed identities."
_DISALLOWED_NAMES = frozenset((
    "a", "an", "the", "your", "you", "me", "my", "not", "no", "yes", "ready", "sorry", "happy",
    "excited", "here", "back", "new", "good", "speaking", "testing", "test", "unknown", "speaker", "name",
    "fine", "okay", "ok", "sure", "done", "available", "well", "late", "early", "tired", "sick", "busy",
    "honored", "delighted", "glad", "afraid"))
_CONTEXT_WARNING = re.compile(r"\b(?:role.?play|pretend|imagin\w*|suppos\w*|hypothetic\w*|example|quot(?:e[ds]?|ing|ation)|script|jok(?:e|ing)|kidding|not my name|isn't my name|he said|she said|they said|customer said|said this|saying|told|act as|imitat\w*|say something like|for instance|practi[cs]\w*|rehears\w*|demo\w*|fiction\w*|character)\b", re.IGNORECASE)
_V2_SETUP = re.compile(r"\b(?:role[ -]?play(?:ing)?|pretend(?:ing)?|(?:let['’]s|let us) (?:imagine|rehearse)|imagine (?:someone|me|you|us) (?:say(?:ing)?|introduc\w*)|(?:he|she|they|(?:the )?customer) (?:said|says)|say something like|said (?:he|she|they|(?:the )?customer|(?-i:[A-Z][a-z]+))|for (?:example|instance)|for the purpose of (?:this|an) example|as (?:an example|a joke)|hypothetically|act(?:ing)? as|(?:a|the|this|that) (?:quote|script)|quot(?:ed|ing)|rehears(?:e|ing)|(?:demo|demonstrat\w*) (?:how|what) (?:to|you|I|we) (?:say|introduce))\b", re.IGNORECASE)
_V2_CORRECTION = re.compile(r"\b(?:just (?:kidding|joking)|not really|(?:i (?:am|was)|i['’]m) (?:joking|kidding|pretending)|not my name|isn['’]t my name|(?:that|this|it)(?: (?:was|is)|['’]s) (?:only |just )?(?:a |an )?(?:joke|example|quote|script|role[ -]?play|fiction)|(?:my )?real name is|i misspoke|(?:that|this) (?:name|introduction) (?:was|is) (?:wrong|incorrect|fictional))\b", re.IGNORECASE)


def introduction_name(text, policy=None):
    """An explicit introduction; V2 also permits a following separate clause."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > 300 or "\n" in text or "\r" in text:
        return None
    if policy == "explicit_intro_v2" and (any(quote in text for quote in ('"', "“", "”"))
                                           or _V2_CORRECTION.search(text)
                                           or _V2_SETUP.search(re.split(r"[.!?]", text, maxsplit=1)[0])):
        return None
    suffix = r"(?:[.!](?:\s+.*)?|,\s+.*| (?:from|with|and I)\s+.*)?" if policy == "explicit_intro_v2" else r"[.!]?"
    patterns = (r"my name is (.+?)" + suffix, r"(?:hi|hello|hey),? (?:i am|i'm|i’m) (.+?)" + suffix,
                r"this is (.+?) speaking" + suffix)
    for pattern in patterns:
        match = re.fullmatch(pattern, text.strip(), flags=re.IGNORECASE)
        if match:
            name = match.group(1)
            words = name.split(" ")
            if not 1 <= len(words) <= 4 or len(name) > 60:
                return None
            for word in words:
                parts = re.split("[-'’]", word)
                first = parts[0]
                if (not first or unicodedata.category(first[0]) not in ("Lu", "Lt")
                        or any(unicodedata.category(char)[0] not in ("L", "M") for char in first[1:])
                        or any(not part or any(unicodedata.category(char)[0] not in ("L", "M") for char in part) for part in parts[1:])
                        or sum(unicodedata.category(char).startswith("L") for char in word) < 2
                        or word.lower() in _DISALLOWED_NAMES):
                    return None
            if policy == "explicit_intro_v2" and re.search(
                    r"\b(?:my name is|(?:hi|hello|hey),? (?:i am|i'm|i’m)|this is .{1,80} speaking)\b",
                    text.strip()[match.end(1):], re.IGNORECASE):
                return None
            return name
    return None


def _context_warning(segments, index):
    identity = segments[index]["speaker"]
    before = [item for item in reversed(segments[:index]) if item["speaker"] == identity][:2]
    after = [item for item in segments[index + 1:] if item["speaker"] == identity][:2]
    return _CONTEXT_WARNING.search(" ".join(item.get("text", "") for item in before + after)) is not None


def _v2_context_warning(segments, evidence):
    first, last = segments[evidence[0]], segments[evidence[-1]]
    def nearby(segment, before):
        start = first.get("start_ms") if before else segment.get("start_ms")
        end = segment.get("end_ms") if before else last.get("end_ms")
        return type(start) is not int or type(end) is not int or 0 <= start - end <= 15_000
    before = [item for item in reversed(segments[:evidence[0]]) if item.get("speaker") == first["speaker"]][:2]
    after = [item for item in segments[evidence[-1] + 1:] if item.get("speaker") == first["speaker"]][:2]
    return (_V2_SETUP.search(" ".join(item["text"] for item in before if nearby(item, True))) is not None
            or _V2_CORRECTION.search(" ".join(item["text"] for item in after if nearby(item, False))) is not None)


def introduction_candidates(segments, policy=None):
    """Collect source-local introductions with exact (possibly split) evidence."""
    result = {}
    for index, segment in enumerate(segments):
        identity = segment.get("speaker")
        if not isinstance(identity, str) or not re.fullmatch(r"(?:mic|system)_speaker_[1-9][0-9]*", identity):
            continue
        text, evidence = segment["text"].strip(" \t") if policy == "explicit_intro_v2" else segment["text"], [index]
        name = introduction_name(text, policy)
        if policy == "explicit_intro_v2" and name is None:
            for following in range(index + 1, min(len(segments), index + 3)):
                previous, next_segment = segments[following - 1], segments[following]
                first_start, previous_end, next_start, next_end = (segment.get("start_ms"), previous.get("end_ms"),
                                                                 next_segment.get("start_ms"), next_segment.get("end_ms"))
                if (next_segment.get("speaker") != identity or previous["text"].strip().endswith((".", "?", "!"))
                        or any(type(value) is not int for value in (first_start, previous_end, next_start, next_end))
                        or not 0 <= first_start <= previous_end <= next_start <= next_end
                        or next_start - previous_end > 1500 or next_end - first_start > 10_000):
                    break
                text += " " + next_segment["text"].strip(" \t")
                evidence.append(following)
                name = introduction_name(text, policy)
                if name is not None:
                    break
        if name is None:
            continue
        warning = _v2_context_warning(segments, evidence) if policy == "explicit_intro_v2" else _context_warning(segments, index)
        if not warning:
            refs = result.setdefault(identity, {}).setdefault(unicodedata.normalize("NFC", name), [])
            refs.extend(index for index in evidence if index not in refs)
    return result


def speaker_attributions(transcript):
    """Return validated diarized identities, or None for unchanged legacy output.

    A name-shaped label is never evidence. Every named identity must cite exact
    self-introduction text spoken by that same speaker on that same source.
    """
    if not isinstance(transcript, dict):
        raise ValueError("Transcript must be an object")
    if transcript.get("speaker_labels") != "diarized_v1":
        return None
    segments, metadata = transcript.get("segments"), transcript.get("speakers")
    if not isinstance(segments, list) or not isinstance(metadata, dict):
        raise ValueError("Separated speakers require segments and speaker metadata")
    result = {}
    for identity, person in metadata.items():
        match = re.fullmatch(r"(mic|system)_speaker_([1-9][0-9]*)", identity) if isinstance(identity, str) else None
        if not match or not isinstance(person, dict):
            raise ValueError("Invalid separated speaker identity")
        source, number = match.groups()
        label = ("Microphone speaker " if source == "mic" else "Other speaker ") + number
        if person.get("source") != source or person.get("label") != label:
            raise ValueError("Separated speaker metadata does not match its source")
        result[identity] = {"label": label, "source": source}
    for segment in segments:
        if (not isinstance(segment, dict) or segment.get("source") not in ("mic", "system")
                or not isinstance(segment.get("text"), str)):
            raise ValueError("Every separated segment needs its microphone or system source")
        identity = segment.get("speaker")
        if identity == "unknown":
            continue
        if not isinstance(identity, str) or identity not in result or result[identity]["source"] != segment["source"]:
            raise ValueError("Transcript segment does not match its separated speaker")
    policy = transcript.get("speaker_name_policy")
    if policy not in (None, "explicit_intro_v2"):
        raise ValueError("Unknown speaker name policy")
    introductions = introduction_candidates(segments, policy)
    claimed = {}
    for identity, names in introductions.items():
        for normalized_name in names:
            claimed.setdefault(normalized_name.lower(), set()).add(identity)
    for identity, person in metadata.items():
        has_name = any(key in person for key in ("name", "name_source", "evidence"))
        if not has_name:
            continue
        name, evidence = person.get("name"), person.get("evidence")
        if (not isinstance(name, str) or person.get("name_source") != "self_introduction"
                or not isinstance(evidence, list) or not evidence
                or any(type(index) is not int or not 0 <= index < len(segments) for index in evidence)
                or len(set(evidence)) != len(evidence)):
            raise ValueError("Speaker name requires exact self-introduction evidence")
        candidates = introductions.get(identity, {})
        normalized_name = unicodedata.normalize("NFC", name)
        if set(candidates) != {normalized_name} or len(claimed.get(normalized_name.lower(), set())) != 1:
            raise ValueError("Speaker name is unsupported or conflicts with another introduction")
        for index in evidence:
            segment = segments[index]
            if (segment.get("speaker") != identity or segment.get("source") != person["source"]
                    or policy is None and unicodedata.normalize("NFC", introduction_name(segment.get("text")) or "") != normalized_name):
                raise ValueError("Speaker name evidence belongs to another voice or is not a self-introduction")
        if (evidence != candidates[normalized_name]
                or policy is None and name not in {introduction_name(segments[index]["text"]) for index in evidence}):
            raise ValueError("Speaker name evidence must list its exact introduction segments")
        result[identity].update(name=name, evidence=list(evidence))
    return result


def segment_label(segment, attributions=None):
    if attributions is None:
        return source_label(segment.get("speaker"))
    person = attributions.get(segment.get("speaker"))
    if person:
        if person.get("suggested_name"):
            return person["suggested_name"] + " (suggested)"
        return person.get("name") or person["label"]
    return "Microphone (unidentified)" if segment.get("source") == "mic" else "System audio (unidentified)"


def source_label(speaker):
    # Even a name-shaped value in the legacy speaker field is not identity
    # evidence. A shared microphone and a system track can both contain more
    # than one person; this legacy field has no separated-speaker evidence.
    if not isinstance(speaker, str):
        return "Audio source unspecified"
    return {"me": "Microphone", "mic": "Microphone", "them": "System audio",
            "system": "System audio"}.get(speaker, "Audio source unspecified")


def validate_meeting_context(value):
    from .meeting_context import meeting_context
    return None if value is None else meeting_context(value)


def transcript_digest(transcript):
    return hashlib.sha256(json.dumps(transcript, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def bind_speaker_review(data, transcript, context=None):
    """Add caller-owned provenance after the model returns assignments only."""
    result = copy.deepcopy(data)
    if isinstance(result, dict) and "speaker_review" in result:
        review = result["speaker_review"]
        if not isinstance(review, dict) or set(review) != {"schema_version", "assignments"}:
            raise ValueError("Speaker review must contain only its version and assignments")
        review.update(transcript_sha256=transcript_digest(transcript),
                      context=validate_meeting_context(context))
    return result


def _name_key(name):
    return " ".join(unicodedata.normalize("NFC", name).casefold().split())


def name_in_text(name, text):
    return re.search(r"(?<!\w)" + re.escape(_name_key(name)) + r"(?!\w)", _name_key(text)) is not None


def effective_attributions(transcript, notes=None):
    """Validate a source-bound review without changing transcript IDs or text.

    Suggested names are deliberately separate from confirmed `name` values so
    an action owner cannot become definite through a contextual mapping alone.
    """
    people = speaker_attributions(transcript)
    if not isinstance(notes, dict) or "speaker_review" not in notes:
        return people
    review = notes["speaker_review"]
    if (not isinstance(review, dict)
            or set(review) != {"schema_version", "assignments", "transcript_sha256", "context"}
            or type(review.get("schema_version")) is not int or review["schema_version"] != 1
            or review.get("transcript_sha256") != transcript_digest(transcript)):
        raise ValueError("Speaker review does not match its source transcript")
    context = validate_meeting_context(review["context"])
    participants = context["participants"] if context else []
    assignments = review["assignments"]
    if not isinstance(assignments, list) or len(assignments) > 100:
        raise ValueError("Speaker review requires a bounded assignment list")
    segments = transcript["segments"]
    # This parser only confirms names actually introduced by a separated voice.
    introductions = introduction_candidates(segments, policy="explicit_intro_v2")
    seen_ids, seen_names = set(), {}
    for assignment in assignments:
        if (not isinstance(assignment, dict) or set(assignment) != {
                "speaker_id", "name", "status", "evidence", "context_participants"}):
            raise ValueError("Invalid speaker review assignment")
        identity, name, status = (assignment[key] for key in ("speaker_id", "name", "status"))
        if (not isinstance(identity, str) or people is None or identity not in people
                or identity in seen_ids or status not in ("confirmed", "suggested")):
            raise ValueError("Speaker review needs a unique existing separated voice")
        from .meeting_context import participant_name
        participant_name(name)
        normalized = _name_key(name)
        if (len(name) > 60 or normalized.endswith("(suggested)")
                or normalized in {"microphone", "mic", "system", "system audio", "other speaker", "everyone"}
                or re.fullmatch(r"(?:(?:microphone|mic|system(?: audio)?|other|remote)[ _-]+)?(?:speaker|participant)[ _-]*(?:[0-9]+|[a-z])", normalized)):
            raise ValueError("Speaker review requires a person name, not an anonymous or annotated label")
        refs, context_refs = assignment["evidence"], assignment["context_participants"]
        for indexes, size in ((refs, len(segments)), (context_refs, len(participants))):
            if (not isinstance(indexes, list) or len(indexes) > 100
                    or any(type(index) is not int or not 0 <= index < size for index in indexes)
                    or len(set(indexes)) != len(indexes)):
                raise ValueError("Speaker review contains invalid evidence references")
        if not refs or not any(segments[index].get("speaker") == identity for index in refs):
            raise ValueError("Speaker review needs evidence from the assigned voice")
        existing = people[identity].get("name")
        if existing is not None and (name != existing or status != "confirmed"):
            raise ValueError("Speaker review conflicts with an established introduction")
        key = _name_key(name)
        if key in seen_names and seen_names[key] != identity:
            raise ValueError("Speaker review assigns the same name to different voices")
        if any(_name_key(person["name"]) == key and other != identity
               for other, person in people.items() if "name" in person):
            raise ValueError("Speaker review conflicts with another named voice")
        if status == "confirmed":
            candidates = introductions.get(identity, {})
            if set(candidates) != {unicodedata.normalize("NFC", name)} or refs != candidates[unicodedata.normalize("NFC", name)]:
                raise ValueError("Confirmed speaker review requires exact self-introduction evidence")
            if any(any(_name_key(candidate) == key for candidate in names)
                   for other, names in introductions.items() if other != identity):
                raise ValueError("Confirmed speaker name occurs on more than one voice")
            people[identity].update(name=name, evidence=list(refs))
        else:
            if not (any(name_in_text(name, segments[index]["text"]) for index in refs)
                    or any(participants[index]["name"] == name for index in context_refs)):
                raise ValueError("Suggested speaker name needs exact transcript or visible-name evidence")
            people[identity].update(suggested_name=name, evidence=list(refs))
        seen_ids.add(identity)
        seen_names[key] = identity
    suggested = {_name_key(person["suggested_name"]) for person in (people or {}).values()
                 if person.get("suggested_name")}
    # Enforce this at the common rendering boundary too, not only in the Codex
    # runner: another notes adapter must not promote a suggestion to an owner.
    for action in notes.get("actions", []) if isinstance(notes.get("actions", []), list) else []:
        if not isinstance(action, dict) or not isinstance(action.get("owner"), str):
            continue
        if _name_key(action["owner"]) in suggested:
            refs = action.get("evidence")
            if (not isinstance(refs, list) or not refs
                    or any(type(index) is not int or not 0 <= index < len(segments) for index in refs)
                    or not any(name_in_text(action["owner"], segments[index]["text"]) for index in refs)):
                raise ValueError("Suggested action owner name is absent from independent cited transcript evidence")
    _validate_suggested_prose(notes, segments, people or {}, suggested)
    return people


def _validate_suggested_prose(notes, segments, people, suggested):
    """A contextual name cannot silently become a definite prose attribution."""
    if not suggested:
        return

    def check(text, refs):
        if not isinstance(text, str):
            return  # The notes schema checks the field type separately.
        cited = ([segments[index] for index in refs]
                 if isinstance(refs, list) and all(type(index) is int and 0 <= index < len(segments)
                                                  for index in refs) else [])
        normalized = _name_key(text)
        for name in suggested:
            matches = list(re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", normalized))
            if not matches:
                continue
            supported = any(name_in_text(name, segment["text"])
                            or _name_key(people.get(segment.get("speaker"), {}).get("name", "")) == name
                            for segment in cited)
            if not supported and any(not normalized[match.end():].startswith(" (suggested)") for match in matches):
                raise ValueError("Suggested speaker name in notes needs independent cited evidence or an adjacent (suggested) label")

    for section in ("summary", "topics", "customer_priorities", "objections", "competitive_intel",
                    "decisions", "next_steps", "deal_implications", "open_questions", "actions"):
        items = notes.get(section, [])
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    check(item.get("text"), item.get("evidence"))
    title = notes.get("meeting_title")
    if isinstance(title, dict):
        check(title.get("text"), title.get("evidence"))
    draft = notes.get("follow_up_draft")
    if isinstance(draft, dict):
        for field in ("subject", "body"):
            check(draft.get(field), draft.get("evidence"))


def attribution_notice(transcript, notes=None):
    if isinstance(notes, dict) and "speaker_review" in notes:
        return REVIEW_NOTICE
    return DIARIZED_NOTICE if transcript.get("speaker_labels") == "diarized_v1" else SOURCE_NOTICE


def render_reviewed_transcript(transcript, notes):
    """A derived readable view; never overwrite the immutable source transcript."""
    people = effective_attributions(transcript, notes)
    from .capture_quality import notice
    capture_notice = notice(transcript)
    lines = ([capture_notice, ""] if capture_notice else []) + [attribution_notice(transcript, notes), ""]
    for segment in transcript["segments"]:
        start = segment.get("start_ms")
        if type(start) is not int or start < 0:
            raise ValueError("Transcript segment needs a timestamp")
        seconds = start // 1000
        lines.extend([f"[{seconds // 60:02d}:{seconds % 60:02d}] " + segment_label(segment, people)
                      + ": " + segment["text"], ""])
    return "\n".join(lines)
