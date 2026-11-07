"""Resolve guest names for a new title without changing the attendee or voice records."""
import re

from .meeting_title import _plain
from .speaker_review import _name


def validate_guest_names(names):
    if not isinstance(names, list) or len(names) > 8:
        raise ValueError("Meeting title needs a bounded guest list")
    for name in names:
        if _plain(_name(name)) != name:
            raise ValueError("Invalid meeting title guest")
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("Duplicate meeting title guest")
    return names


def _spoken_role(name, text, role):
    # A third-party mention ("we should invite Alex") supplies no role. Limit
    # support to affirmative introductions and direct greetings in this call.
    name = re.escape(name)
    boundary = r"(?:^|[.!?]\s+|\n)"
    if role == "host":
        phrases = [rf"(?:our|your) (?:co[- ]?)?host (?:today |for this (?:meeting|call) )?is {name}\b",
                   rf"(?:hi[, ]+)?(?:i am|i'm|my name is) {name}[, .]+(?:and )?(?:i'm|i am|i'll be|i will be) (?:your |the )?(?:co[- ]?)?host\b"]
    else:
        phrases = [rf"(?:our|my) (?:guest|client|customer)(?: today)? is {name}\b",
                   rf"(?:welcome|thanks for joining us|thank you for joining us)[, ]+{name}(?=[.!?,]|$)",
                   rf"{name}[, ]+(?:welcome to (?:our|the) (?:meeting|call)|(?:thanks|thank you) for joining us)\b"]
    return any(re.search(boundary + pattern, text, re.IGNORECASE) for pattern in phrases)


def title_guests(notes, transcript, identity_context=None, meeting_context=None, *, owner_names=()):
    """Use positive guest evidence. An invite, microphone or 'You' badge alone is insufficient."""
    identity_context = identity_context or {}
    organizer = identity_context.get("organizer") or {}
    organizer_email = organizer.get("email")
    address_names = {}
    invited = set()
    for person in identity_context.get("candidates", []):
        if not person.get("email"):
            continue
        email = person["email"].casefold()
        if person["source"] == "calendar":
            invited.add(email)
        address_names.setdefault(email, set()).update(n.casefold() for n in
            [person["name"], *person["aliases"]] if n)
    if organizer_email and organizer.get("name"):
        address_names.setdefault(organizer_email, set()).add(organizer["name"].casefold())
    for person in notes.get("_speaker_identity", {}).get("corrections", {}).get("assignments", {}).values():
        address = (person.get("email") or "").casefold()
        if address and (address == organizer_email or address in invited):
            address_names.setdefault(address, set()).add(person["name"].casefold())

    def addresses(name):
        # Allow a spoken first name only when it identifies one account. An
        # exact short alias is still ambiguous if another person shares it.
        key = name.casefold()
        return {email for email, spellings in address_names.items() if any(
            spelling == key or (" " not in key and spelling.split()[0] == key)
            for spelling in spellings)}

    people = notes["meeting_title"]["participants"]
    named = [p["name"] for p in people]
    platform_people = list((meeting_context or {}).get("participants", []))
    platform_people.extend((identity_context.get("timeline") or {}).get("participants", []))
    visible = [p["name"] for p in platform_people]
    # A platform "You" label identifies the recorder’s account, not everybody on
    # the microphone. Explicit local aliases also work for non-browser calls.
    owner_keys = {name.casefold() for name in validate_guest_names(list(owner_names))}
    owner_keys.update(p["name"].casefold() for p in platform_people if p["self"])
    owner_first = {name.split()[0] for name in owner_keys}
    # An unqualified first name cannot bind an account. Match full identities
    # or exact aliases explicitly associated with a confirmed identity.
    owner_addresses = {email for email, spellings in address_names.items()
                       if any(name in owner_keys and " " in name for name in spellings)}
    for email in owner_addresses:
        owner_keys.update(address_names[email])

    visible.extend(p["name"] for p in identity_context.get("candidates", []) if p["source"] == "visible")
    # Only the transcript-validated attendee list may use spoken role cues.
    # A visible platform label + a matched guest email is independent evidence.
    speech = "\n".join(segment["text"].strip().replace("’", "'") for segment in transcript["segments"]
                       if isinstance(segment, dict) and isinstance(segment.get("text"), str))
    observed = list(dict.fromkeys([*named, *visible]))
    hosted = {n.casefold() for n in observed if _spoken_role(n, speech, "host")} if "host" in speech.casefold() else set()
    host_addresses = {address for n in hosted for address in addresses(n)}
    if organizer_email:
        host_addresses.add(organizer_email)
    result, used_names, used_addresses = [], set(), set()
    for name in observed:
        try:
            validate_guest_names([name])
        except ValueError:
            continue
        key = name.casefold()
        matches = addresses(name)
        if key in owner_keys or key in owner_first or owner_addresses & matches:
            continue
        if key in hosted or host_addresses & matches or len(matches) > 1:
            continue
        if not (organizer_email and len(matches) == 1 and matches <= invited
                or name in named and _spoken_role(name, speech, "guest")):
            continue
        if key in used_names or used_addresses & matches:
            continue
        result.append(name)
        used_names.add(key)
        used_addresses.update(matches)
        if len(result) == 8:
            break
    return result


def bind_title_guests(notes, transcript, identity_context=None, meeting_context=None, *, owner_names=()):
    from .meeting_title import meeting_title
    if "_title_guests" in notes:
        notes = dict(notes)
        notes.pop("_title_guests")
    # Validate the model roster and evidence before deriving or using its names.
    meeting_title(notes, transcript)
    if "meeting_title" in notes:
        guests = title_guests(notes, transcript, identity_context, meeting_context, owner_names=owner_names)
        return dict(notes, _title_guests=guests)
    return notes
