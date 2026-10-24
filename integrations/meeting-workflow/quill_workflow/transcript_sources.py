"""Validate separated speakers and names without treating a track as a person."""
import re
import unicodedata

SOURCE_NOTICE = "Microphone and system audio identify audio sources, not individual speakers."
DIARIZED_NOTICE = "Speakers are separated automatically. Names come from clear self-introductions; other voices keep anonymous labels."
_DISALLOWED_NAMES = frozenset((
    "a", "an", "the", "your", "you", "me", "my", "not", "no", "yes", "ready", "sorry", "happy",
    "excited", "here", "back", "new", "good", "speaking", "testing", "test", "unknown", "speaker", "name",
    "fine", "okay", "ok", "sure", "done", "available", "well", "late", "early", "tired", "sick", "busy",
    "honored", "delighted", "glad", "afraid"))
_CONTEXT_WARNING = re.compile(r"\b(?:role.?play|pretend|imagin\w*|suppos\w*|hypothetic\w*|example|quot(?:e[ds]?|ing|ation)|script|jok(?:e|ing)|kidding|not my name|isn't my name|he said|she said|they said|customer said|said this|saying|told|act as|imitat\w*|say something like|for instance|practi[cs]\w*|rehears\w*|demo\w*|fiction\w*|character)\b", re.IGNORECASE)


def introduction_name(text):
    """An entire, explicit self-introduction; never a quoted or partial mention."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > 300 or "\n" in text or "\r" in text:
        return None
    patterns = (r"my name is (.+?)[.!]?", r"(?:hi|hello|hey),? (?:i am|i'm|i’m) (.+?)[.!]?",
                r"this is (.+?) speaking[.!]?")
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
            return name
    return None


def _context_warning(segments, index):
    identity = segments[index]["speaker"]
    before = [item for item in reversed(segments[:index]) if item["speaker"] == identity][:2]
    after = [item for item in segments[index + 1:] if item["speaker"] == identity][:2]
    return _CONTEXT_WARNING.search(" ".join(item.get("text", "") for item in before + after)) is not None


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
    introductions = {}
    claimed = {}
    for index, segment in enumerate(segments):
        identity = segment["speaker"]
        name = introduction_name(segment.get("text"))
        if identity in result and name is not None and not _context_warning(segments, index):
            normalized_name = unicodedata.normalize("NFC", name)
            introductions.setdefault(identity, {}).setdefault(normalized_name, []).append(index)
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
                    or unicodedata.normalize("NFC", introduction_name(segment.get("text")) or "") != normalized_name):
                raise ValueError("Speaker name evidence belongs to another voice or is not a self-introduction")
        if (evidence != candidates[normalized_name]
                or name not in {introduction_name(segments[index]["text"]) for index in evidence}):
            raise ValueError("Speaker name evidence must list its exact introduction segments")
        result[identity].update(name=name, evidence=list(evidence))
    return result


def segment_label(segment, attributions=None):
    if attributions is None:
        return source_label(segment.get("speaker"))
    person = attributions.get(segment.get("speaker"))
    if person:
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
