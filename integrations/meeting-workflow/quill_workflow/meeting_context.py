"""Bounded browser evidence; visible names never establish an email identity."""
import re
import unicodedata


def participant_name(value):
    if (not isinstance(value, str) or not value or len(value) > 100
            or len(value.encode("utf-8")) > 400
            or any(unicodedata.category(character).startswith("C") for character in value)):
        raise ValueError("Invalid visible participant name")
    name = " ".join(unicodedata.normalize("NFC", value).split())
    if (not name or name != value or "@" in name or "://" in name
            or name.casefold() in {"you", "me", "host", "participant", "participants", "unknown", "guest"}):
        raise ValueError("Invalid visible participant name")
    return name


def meeting_context(value, *, max_participants=50):
    if (not isinstance(value, dict) or set(value) != {"provider", "key", "participants", "coverage"}
            or value.get("coverage") != "visible_only"):
        raise ValueError("Invalid meeting context")
    provider, key = value.get("provider"), value.get("key")
    if not isinstance(provider, str):
        raise ValueError("Invalid meeting context provider")
    pattern = {"google-meet": r"[a-z]{3}-[a-z]{4}-[a-z]{3}", "zoom-web": r"[0-9]{9,12}"}.get(provider)
    if pattern is None or not isinstance(key, str) or re.fullmatch(pattern, key) is None:
        raise ValueError("Invalid meeting context identity")
    people = value.get("participants")
    if not isinstance(people, list) or len(people) > max_participants:
        raise ValueError("Invalid meeting participant list")
    participants = []
    for person in people:
        if not isinstance(person, dict) or set(person) != {"name", "self"} or type(person.get("self")) is not bool:
            raise ValueError("Invalid meeting participant")
        participants.append({"name": participant_name(person.get("name")), "self": person["self"]})
    return {"provider": provider, "key": key, "participants": participants, "coverage": "visible_only"}
