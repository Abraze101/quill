"""Source-bound speaker reviews and explicit user corrections.

This is a derived view. Neither audio, words, timestamps nor voice IDs change.
Model output cannot manufacture a user correction: the notes runner attaches
this envelope only after it has checked the model's strict output schema.
"""
import copy
import re

MAX_BUNDLE_BYTES = 10_000_000


def prompt_bundle(bundle):
    """Keep the full source-bound timeline local; send only reviewed conclusions."""
    if bundle is None:
        return None
    result = copy.deepcopy({key: value for key, value in bundle.items() if key != "context"})
    result["context"] = copy.deepcopy({key: value for key, value in bundle["context"].items() if key != "timeline"})
    if bundle["context"]["schema_version"] == 2:
        result["local_participants"] = [dict(person) for person in bundle["context"]["timeline"]["participants"]
                                        if person["self"]]
    return result


def validate_corrections(value, transcript):
    from .transcript_sources import speaker_attributions, transcript_digest
    from .meeting_context import participant_name
    from .recipient_suggestions import _email
    if (not isinstance(value, dict) or set(value) != {"schema_version", "transcript_sha256", "assignments"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("transcript_sha256") != transcript_digest(transcript)
            or not isinstance(value.get("assignments"), dict) or len(value["assignments"]) > 100):
        raise ValueError("Speaker corrections do not match this transcript")
    people = speaker_attributions(transcript) or {}
    for identity, person in value["assignments"].items():
        if identity not in people or not isinstance(person, dict) or set(person) != {"name", "email", "status"}:
            raise ValueError("Correction requires an existing separated voice")
        participant_name(person["name"])
        if (len(person["name"]) > 60 or person["status"] != "user_confirmed"
                or re.fullmatch(r"(?i)(?:(?:mic|system|microphone|other) )?speaker\s*\d*", person["name"])
                or person["email"] is not None and _email(person["email"]) is None):
            raise ValueError("Enter a person name and a valid optional email")
    return value


def validate_bundle(value, transcript):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "context", "report", "corrections"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1):
        raise ValueError("Invalid speaker identity review")
    from .speaker_review import validate_context, validate_report
    context = validate_context(value["context"])
    if value["report"] is not None:
        validate_report(value["report"], transcript, context)
    validate_corrections(value["corrections"], transcript)
    return value


def empty_bundle(transcript, context=None):
    from .transcript_sources import transcript_digest
    return {"schema_version": 1, "context": context or {"schema_version": 1, "candidates": []},
            "report": None, "corrections": {"schema_version": 1,
                "transcript_sha256": transcript_digest(transcript), "assignments": {}}}


def apply_bundle(people, transcript, bundle):
    validate_bundle(bundle, transcript)
    people = copy.deepcopy(people or {})
    for item in (bundle["report"] or {}).get("speakers", []):
        person = people[item["speaker_id"]]
        person["review_reason"] = item["reason"]
        if item["status"] == "unresolved":
            continue
        if person.get("name"):
            person.update(email=item["email"], review_status="confirmed")
            continue
        person.pop("suggested_name", None)
        key = "name" if item["status"] in ("confirmed", "matched") else "suggested_name"
        person.update({key: item["name"], "evidence": item["evidence"], "email": item["email"],
                       "review_status": item["status"]})
    for identity, item in bundle["corrections"]["assignments"].items():
        person = people[identity]
        person.pop("suggested_name", None)
        person.update(name=item["name"], email=item["email"], review_status="user_confirmed",
                      review_reason="Confirmed by you for this voice.")
    return people


def attach(notes, transcript, bundle):
    validate_bundle(bundle, transcript)
    result = copy.deepcopy(notes or {})
    result["_speaker_identity"] = copy.deepcopy(bundle)
    return result
