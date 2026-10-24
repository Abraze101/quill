"""Source-checked meeting names shared by notes and Drive filing."""
import re
import unicodedata


MAX_TITLE_BYTES = 160


def _plain(value):
    value = unicodedata.normalize("NFC", value)
    value = "".join(" " if unicodedata.category(char).startswith("C") else char
                    for char in value)
    value = re.sub(r'[\\/:*?"<>|]', " ", value)
    return " ".join(value.split()).strip(" .-")


def _clip(value, limit):
    if len(value.encode("utf-8")) <= limit:
        return value
    shortened = value.encode("utf-8")[:max(0, limit - 3)].decode("utf-8", "ignore")
    # Prefer whole words without throwing away an entire unspaced title.
    if " " in shortened and len(shortened.rsplit(" ", 1)[0]) >= len(shortened) // 2:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(" .-") + "..."


def meeting_title(notes, transcript):
    """Return a safe topic-and-attendee name, or None for historical notes.

    The model must establish participation from the conversation. Validation
    checks evidence, exact cited name spelling, and anonymous labels; it cannot
    independently prove that a mentioned person attended the meeting.
    """
    if "meeting_title" not in notes:
        return None
    title = notes["meeting_title"]
    if not isinstance(title, dict) or set(title) != {"text", "evidence", "participants"}:
        raise ValueError("Meeting title needs text, evidence, and participants")
    if not isinstance(title["text"], str) or not 1 <= len(title["text"]) <= 120:
        raise ValueError("Meeting title must be a concise topic")
    topic = _plain(title["text"])
    if not topic:
        raise ValueError("Meeting title has no readable topic")
    segments = transcript.get("segments") if isinstance(transcript, dict) else None
    if not isinstance(segments, list) or not segments:
        raise ValueError("Meeting title requires transcript segments")

    def evidence(refs):
        if not isinstance(refs, list) or not refs:
            raise ValueError("Meeting title and participants need transcript evidence")
        for index in refs:
            if type(index) is not int or not 0 <= index < len(segments):
                raise ValueError("Meeting title has an invalid transcript reference")
            segment = segments[index]
            if (not isinstance(segment, dict) or not isinstance(segment.get("text"), str)
                    or type(segment.get("start_ms")) is not int or segment["start_ms"] < 0):
                raise ValueError("Meeting title evidence needs transcript text and timestamps")
        return [segments[index]["text"] for index in refs]

    evidence(title["evidence"])
    people = title["participants"]
    if not isinstance(people, list) or len(people) > 8:
        raise ValueError("Meeting title participants must be an array of at most eight people")
    names, seen = [], set()
    for person in people:
        if not isinstance(person, dict) or set(person) != {"name", "evidence"}:
            raise ValueError("Meeting title participants need a name and evidence")
        name = person["name"]
        if not isinstance(name, str) or not 1 <= len(name) <= 60 or _plain(name) != name:
            raise ValueError("Meeting title participant name must be plain, exact text")
        normalized = " ".join(unicodedata.normalize("NFC", name).casefold().split())
        if normalized in {"me", "them", "speaker", "other speaker", "participant", "participants",
                          "other participant", "unknown", "unknown speaker", "host", "you", "everyone"} or re.fullmatch(
                r"(?:speaker|participant)[ _-]*(?:[0-9]+|[a-z])", normalized):
            raise ValueError("Meeting title cannot use anonymous participant names")
        cited = evidence(person["evidence"])
        pattern = r"(?<!\w)" + re.escape(normalized) + r"(?!\w)"
        if not any(re.search(pattern, " ".join(unicodedata.normalize("NFC", text).casefold().split()))
                   for text in cited):
            raise ValueError("Meeting title participant name is absent from its cited evidence")
        if normalized in seen:
            raise ValueError("Meeting title participant names must be unique")
        seen.add(normalized)
        names.append(name)

    # Keep complete names, never truncate a person's identity. A long attendee
    # list is shortened to the names that fit; full evidence remains in notes.
    included = []
    for name in names:
        candidate = ", ".join(included + [name])
        if len(candidate.encode("utf-8")) > 112:
            break
        included.append(name)
    suffix = " - " + ", ".join(included) if included else ""
    return _clip(topic, MAX_TITLE_BYTES - len(suffix.encode("utf-8"))) + suffix
