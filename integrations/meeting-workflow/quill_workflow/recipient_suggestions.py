"""Bounded, read-only calendar suggestions for a notes-sharing review.

Calendar invitations establish suggested addresses, not meeting attendance.
Every address is returned unchecked. No names are inferred from email handles,
and no event text is ever treated as an instruction.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


MAX_CALENDARS = 20
MAX_SEARCH_EVENTS = 25
MAX_READ_EVENTS = 10
MAX_RECIPIENTS = 20
MAX_RECIPIENT_TEXT = 4000
_ROLES = {"owner", "writer", "reader"}
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}")
_STOP_WORDS = {"a", "an", "and", "at", "between", "call", "for", "in", "meeting", "notes", "of", "on",
               "or", "review", "session", "shared", "the", "to", "with"}
ATTENDANCE_NOTE = "Calendar invitees are suggestions, not confirmed attendees. Select the event and review each address."


class RecipientSuggestionError(ValueError):
    """The frozen meeting context cannot support a bounded calendar lookup."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _text(value, limit=500):
    return value if isinstance(value, str) and 0 < len(value) <= limit and not re.search(r"[\x00-\x1f\x7f]", value) else None


def _email(value):
    if not isinstance(value, str) or len(value) > 254 or not _EMAIL.fullmatch(value):
        return None
    local, domain = value.rsplit("@", 1)
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local or ".." in domain:
        return None
    if any(part.startswith("-") or part.endswith("-") or not part for part in domain.split(".")):
        return None
    return value


def _time(value):
    zone = None
    if isinstance(value, dict):
        zone = value.get("timeZone") or value.get("time_zone")
        value = value.get("dateTime") or value.get("date_time")
    if not isinstance(value, str) or len(value) > 100 or "T" not in value:
        raise ValueError("Expected an explicit event time")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None and isinstance(zone, str):
        result = result.replace(tzinfo=ZoneInfo(zone))
    if result.tzinfo is None:
        raise ValueError("Event time needs a timezone")
    return result.astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _unwrap(value):
    if not isinstance(value, dict) or value.get("isError") is True:
        raise ValueError("Calendar returned an unreadable response")
    result = value.get("structuredContent", value)
    if not isinstance(result, dict) or "error" in result:
        raise ValueError("Calendar returned an unreadable response")
    return result


def _page(value, key):
    result = _unwrap(value)
    rows = result.get(key, result.get("items"))
    if not isinstance(rows, list):
        raise ValueError("Calendar returned an unreadable list")
    return rows, bool(result.get("next_page_token") or result.get("nextPageToken"))


def _words(value):
    return {word for word in re.findall(r"[^\W_]+", value.casefold(), re.UNICODE)
            if len(word) > 1 and not word.isdigit() and word not in _STOP_WORDS}


def _name_mentioned(name, text):
    # Exact supplied names only. A first name supplied by the notes may match
    # that same whole word in the title; email handles are never considered.
    return re.search(r"(?<!\w)" + re.escape(name.casefold()) + r"(?!\w)", text.casefold()) is not None


def _meeting_key(value):
    if not isinstance(value, dict):
        return None
    provider, key = value.get("provider"), value.get("key")
    if provider in {"google-meet", "google_meet", "meet"} and isinstance(key, str) and re.fullmatch(r"[a-z]{3}-[a-z]{4}-[a-z]{3}", key):
        return ("meet", key)
    if provider in {"zoom-web", "zoom_web", "zoom"} and isinstance(key, str) and re.fullmatch(r"[0-9]{9,12}", key):
        return ("zoom", key)
    return None


def _link_key(value):
    try:
        url = urlsplit(value)
        if url.scheme != "https" or url.username or url.password or url.port not in (None, 443):
            return None
        host = (url.hostname or "").lower()
        if host == "meet.google.com":
            # Match both Meet routes accepted by the browser detector.
            match = re.fullmatch(r"/(?:meet/)?([a-z]{3}-[a-z]{4}-[a-z]{3})/?", url.path)
            return ("meet", match.group(1)) if match else None
        if host == "zoom.us" or host.endswith(".zoom.us"):
            match = re.fullmatch(r"/(?:j/|wc/join/)([0-9]{9,12})/?", url.path)
            if not match:
                match = re.fullmatch(r"/wc/([0-9]{9,12})/(?:join|start)/?", url.path)
            return ("zoom", match.group(1)) if match else None
    except (ValueError, AttributeError):
        pass
    return None


def _event_meeting_keys(event):
    values = [event.get("hangout_link"), event.get("hangoutLink"), event.get("location")]
    conference = event.get("conference_data", event.get("conferenceData", {}))
    if isinstance(conference, dict):
        entries = conference.get("entry_points", conference.get("entryPoints", []))
        if isinstance(entries, list):
            values.extend(entry.get("uri") for entry in entries[:20] if isinstance(entry, dict))
    description = event.get("description")
    if isinstance(description, str) and len(description) <= 100_000:
        values.extend(re.findall(r"https://[^\s<>\"']+", html.unescape(description))[:50])
    keys = set()
    for value in values:
        if not isinstance(value, str) or len(value) > 4000:
            continue
        key = _link_key(value.rstrip(".,;)]}"))
        if key:
            keys.add(key)
    return keys


def _times(event):
    try:
        start, end = _time(event.get("start")), _time(event.get("end"))
        return (start, end) if start < end else None
    except (ValueError, TypeError, OverflowError, KeyError):
        return None


def _match(context, event, start, end):
    times = _times(event)
    if times is None or event.get("status") == "cancelled":
        return None
    event_start, event_end = times
    # A rescheduled readback must still overlap the exact lookup window. A
    # forgotten overnight recording cannot make a later invitation eligible.
    if event_end <= start - timedelta(minutes=90) or event_start >= start + timedelta(minutes=90):
        return None
    overlap = max(timedelta(0), min(end, event_end) - max(start, event_start)).total_seconds()
    distance = abs((event_start - start).total_seconds())
    if overlap == 0 and distance > 90 * 60:
        return None
    key = _meeting_key(context.get("meeting"))
    exact = key is not None and key in _event_meeting_keys(event)
    title = _text(event.get("summary", event.get("title"))) or "Untitled calendar event"
    names = [name for name in context["participants"] if _name_mentioned(name, title)]
    attendees = event.get("attendees", [])
    if isinstance(attendees, list):
        for attendee in attendees[:200]:
            if not isinstance(attendee, dict):
                continue
            name = attendee.get("display_name", attendee.get("displayName"))
            if isinstance(name, str):
                names.extend(candidate for candidate in context["participants"]
                             if candidate not in names and _name_mentioned(candidate, name))
    common = _words(context["title"]) & _words(title)
    if exact and overlap:
        strength, reason, rank = "strong", "Same meeting link and overlapping times; invitees still need review.", 4
    elif exact:
        strength, reason, rank = "tentative", "Same meeting link near the recording time; scheduled times do not overlap.", 3
    elif names or len(common) >= 2:
        signals = []
        if names:
            signals.append("a participant name in the event title or attendee display name")
        if len(common) >= 2:
            signals.append("similar title wording")
        reason = ("Overlapping times" if overlap else "Nearby scheduled time") + " and " + " plus ".join(signals) + "; confirm this is the right event."
        strength, rank = "tentative", 2
    else:
        strength, reason, rank = "tentative", ("Overlapping times only" if overlap else "Nearby scheduled time only") + "; no matching meeting link or participant name.", 1
    return {"strength": strength, "reason": reason, "rank": rank, "overlap": overlap,
            "distance": distance, "start": event_start, "end": event_end, "title": title}


def _recipients(event, self_addresses):
    attendees = event.get("attendees", [])
    if not isinstance(attendees, list):
        return [], True, False
    recipients, seen = [], set()
    incomplete = len(attendees) > 200 or event.get("attendees_omitted") is True or event.get("attendeesOmitted") is True
    for attendee in attendees[:200]:
        if not isinstance(attendee, dict):
            continue
        email = _email(attendee.get("email"))
        response = attendee.get("response_status", attendee.get("responseStatus"))
        if (email is None or email.casefold() in self_addresses or email.casefold() in seen
                or email.casefold().endswith("@resource.calendar.google.com")
                or attendee.get("resource") is True or attendee.get("is_self") is True or attendee.get("self") is True
                or response == "declined"):
            continue
        seen.add(email.casefold())
        name = _text(attendee.get("display_name", attendee.get("displayName")), 200)
        recipients.append({"name": name, "email": email, "source": "Calendar invite",
                           "response_status": response if response in {"accepted", "tentative", "needsAction", "declined"} else "unknown",
                           "optional": attendee.get("optional") is True, "selected": False})
    recipients.sort(key=lambda item: ((item["name"] or item["email"]).casefold(), item["email"].casefold()))
    bounded, text_size = [], 0
    for recipient in recipients:
        added = len(recipient["email"]) + (2 if bounded else 0)
        if len(bounded) == MAX_RECIPIENTS or text_size + added > MAX_RECIPIENT_TEXT:
            break
        bounded.append(recipient)
        text_size += added
    limited = len(bounded) < len(recipients)
    return bounded, incomplete or limited, limited


def _self_addresses(owner, calendars):
    addresses = {owner.casefold()}
    for calendar in calendars:
        email = _email(calendar["calendar_id"])
        if email is None or not (calendar["primary"] or calendar["access_role"] == "owner"):
            continue
        domain = email.rsplit("@", 1)[1].casefold()
        # Google assigns address-shaped IDs to shared/group/resource calendars.
        # Those identify calendars, not another address of the account holder.
        if domain == "googlegroups.com" or domain == "calendar.google.com" or domain.endswith(".calendar.google.com"):
            continue
        addresses.add(email.casefold())
    return addresses


def _event_url(value):
    if not isinstance(value, str) or len(value) > 4000:
        return None
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and url.hostname in {"www.google.com", "google.com", "calendar.google.com"}
                and not url.username and not url.password and url.port in (None, 443)
                and url.path.startswith("/calendar/")):
            return value
    except ValueError:
        pass
    return None


def suggest_recipients(context, invoke):
    """Search at most 20 calendars and read at most 10 candidate events.

    ``invoke`` accepts a read-only operation name and its structured arguments:
    list_calendars, search_events, or read_event. The service has no file or
    database access and never calls a sharing or messaging operation.
    """
    if (not isinstance(context, dict) or not _text(context.get("job"), 128)
            or not isinstance(context.get("source_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", context["source_hash"])
            or not _text(context.get("title"), 500) or _email(context.get("owner")) is None
            or not isinstance(context.get("participants"), list) or len(context["participants"]) > 50
            or any(_text(name, 200) is None for name in context["participants"]) or not callable(invoke)):
        raise RecipientSuggestionError("The saved meeting context is incomplete")
    try:
        start, end = _time(context.get("started")), _time(context.get("ended"))
        if end < start:
            raise ValueError("Recording end precedes its start")
        lower, upper = start - timedelta(minutes=90), start + timedelta(minutes=90)
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        raise RecipientSuggestionError("The saved recording times need review") from error
    result = {"schema_version": 1, "job": context["job"], "source_hash": context["source_hash"],
              "status": "no_match", "events": [], "calendars": [],
              "search_window": {"start": _iso(lower), "end": _iso(upper)},
              "incomplete": False, "warnings": [], "attendance_note": ATTENDANCE_NOTE}

    def warn(message):
        result["incomplete"] = True
        if message not in result["warnings"]:
            result["warnings"].append(message)

    try:
        calendars, more = _page(invoke("list_calendars", {"max_results": MAX_CALENDARS}), "calendars")
    except Exception:
        result["status"] = "unavailable"
        warn("Calendar access is unavailable. Enter recipient emails manually or try again.")
        return result
    if more or len(calendars) > MAX_CALENDARS:
        warn("Only the first 20 visible calendars were checked; more calendars may contain this meeting.")
    usable, seen_calendars = [], set()
    for calendar in calendars[:MAX_CALENDARS]:
        if not isinstance(calendar, dict):
            warn("Some calendar entries could not be read.")
            continue
        identity = _text(calendar.get("id"), 1000)
        role = calendar.get("access_role", calendar.get("accessRole"))
        if identity is None or role not in _ROLES or identity in seen_calendars:
            continue
        seen_calendars.add(identity)
        usable.append({"calendar_id": identity, "name": _text(calendar.get("summary"), 500) or identity,
                       "access_role": role, "primary": calendar.get("primary") is True, "searched": False})
    usable.sort(key=lambda item: (not item["primary"], {"owner": 0, "writer": 1, "reader": 2}[item["access_role"]],
                                  item["calendar_id"]))
    result["calendars"] = usable
    self_addresses = _self_addresses(context["owner"], usable)
    candidates = []
    for calendar in usable:
        try:
            rows, more = _page(invoke("search_events", {"calendar_id": calendar["calendar_id"],
                                "time_min": _iso(lower), "time_max": _iso(upper), "max_results": MAX_SEARCH_EVENTS}), "events")
            calendar["searched"] = True
        except Exception:
            warn("Some calendars could not be searched. Results cover only the calendars marked checked.")
            continue
        if more or len(rows) >= MAX_SEARCH_EVENTS:
            warn("A calendar search reached its event limit; this is a partial list.")
        for event in rows[:MAX_SEARCH_EVENTS]:
            if not isinstance(event, dict) or _text(event.get("id"), 1000) is None:
                continue
            match = _match(context, event, start, end)
            if match:
                candidates.append((calendar, event["id"], match))
    candidates.sort(key=lambda item: (-item[2]["rank"], -item[2]["overlap"], item[2]["distance"],
                                      not item[0]["primary"], item[0]["calendar_id"], item[1]))
    # De-duplicate exact IDs within a calendar, but preserve separate invitations
    # on different calendars so the user can inspect the authoritative source.
    selected, seen_events = [], set()
    for candidate in candidates:
        key = (candidate[0]["calendar_id"], candidate[1])
        if key not in seen_events:
            seen_events.add(key)
            selected.append(candidate)
    if len(selected) > MAX_READ_EVENTS:
        warn("Only the 10 closest matching events were read. Choose an event manually if the right one is missing.")
    verified_reads = 0
    for calendar, event_id, _ in selected[:MAX_READ_EVENTS]:
        try:
            event = _unwrap(invoke("read_event", {"calendar_id": calendar["calendar_id"], "event_id": event_id}))
            if "event" in event and isinstance(event["event"], dict):
                event = event["event"]
            if event.get("id") != event_id:
                raise ValueError("The read event returned a different identity")
            verified_reads += 1
            match = _match(context, event, start, end)
            if match is None:
                continue
            recipients, incomplete, limited = _recipients(event, self_addresses)
            if incomplete:
                warn("Some event invitations have incomplete attendee lists; review addresses manually.")
            if limited:
                warn("Some events have more recipients than this review can hold. At most 20 addresses are shown per event; review the remaining invitations manually.")
            option = {"calendar_id": calendar["calendar_id"], "calendar_name": calendar["name"],
                      "event_id": event_id, "title": match["title"], "start": _iso(match["start"]),
                      "end": _iso(match["end"]), "event_url": _event_url(event.get("url", event.get("htmlLink"))),
                      "match_reason": match["reason"], "match_strength": match["strength"],
                      "recipients": recipients, "selected": False, "source": "Calendar invite",
                      "attendees_complete": not incomplete, "recipient_limit_reached": limited}
            option["suggestion_id"] = _digest({"job": context["job"], "source_hash": context["source_hash"], "event": option})
            result["events"].append((option, match))
        except Exception:
            warn("Some candidate events could not be verified. No addresses were suggested from those events.")
    result["events"].sort(key=lambda value: (-value[1]["rank"], -value[1]["overlap"], value[1]["distance"],
                                             value[0]["calendar_id"], value[0]["event_id"]))
    result["events"] = [option for option, _ in result["events"]]
    if result["events"]:
        result["status"] = "ready"
    elif not usable:
        result["status"] = "unavailable"
        warn("No calendars with readable event details were available. Enter recipient emails manually.")
    elif not any(calendar["searched"] for calendar in usable) or (selected and verified_reads == 0):
        result["status"] = "unavailable"
    return result
