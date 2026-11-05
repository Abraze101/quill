"""Read invitation identity evidence; never infer attendance or share notes."""
import time


def build_context(job, metadata, transcript, *, config=None, learned=(), invoke=None, guard=None):
    from .meeting_context import meeting_context, participant_name
    from .recipient_suggestions import suggest_recipients, _recipients
    from .transcript_sources import transcript_digest
    from .speaker_review import validate_context, _name
    visible = meeting_context(metadata["meeting_context"]) if metadata.get("meeting_context") else None
    people = visible["participants"] if visible else []
    candidates = []
    for person in people:
        try:
            name = _name(person["name"])
        except ValueError:
            continue
        candidates.append({"name": name, "email": None, "source": "visible", "aliases": []})
    status = "not_configured"
    config = config or {}
    transport = None
    reads = {}
    try:
        if invoke is None:
            from .drive_accounts import require_verified_account
            from .codex_calendar_transport import CodexCalendarTransport
            account = require_verified_account(config)
            settings = config.get("publish", {})
            # Calendar and Drive use separate connector links. Resolve Calendar
            # by the explicitly selected account's email, never the first login.
            transport = CodexCalendarTransport(settings.get("codex", "/Applications/Codex.app/Contents/Resources/codex"),
                                               account_email=account["email"], timeout=45)
            invoke = transport.invoke
            owner = account["email"]
        else:
            owner = config.get("drive_account", {}).get("email", "fixture@example.test")
        deadline = time.monotonic() + 60
        def read(tool, arguments):
            if guard:
                guard()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Calendar evidence lookup timed out")
            if transport is not None:
                transport.timeout = min(45, remaining)
            value = invoke(tool, arguments)
            if time.monotonic() > deadline:
                raise ValueError("Calendar evidence lookup timed out")
            if tool == "read_event":
                from .recipient_suggestions import _unwrap
                event = _unwrap(value)
                event = event.get("event", event)
                reads[(arguments["calendar_id"], arguments["event_id"])] = event
            return value
        lookup = {"job": job, "source_hash": transcript_digest(transcript), "owner": owner,
                  "title": "Recorded meeting", "participants": [p["name"] for p in people],
                  "started": metadata["started"], "ended": metadata["ended"],
                  "meeting": {"provider": visible["provider"], "key": visible["key"]} if visible else None,
                  "observed_participants": people, "participant_coverage": "visible_only"}
        found = suggest_recipients(lookup, read)
        strong = [e for e in found["events"] if e["match_strength"] == "strong"]
        # Repeated copies of one invitation are fine. Conflicting invitations
        # sharing a link remain ambiguous and contribute no automatic identities.
        options = []
        for event in strong:
            raw = reads.get((event["calendar_id"], event["event_id"]))
            if raw:
                recipients, _, _ = _recipients(raw, set())
                rows = []
                for person in recipients:
                    try:
                        name = _name(person["name"])
                    except ValueError:
                        name = None  # Keep the address; never infer a name from its handle.
                    rows.append({"name": name, "email": person["email"], "source": "calendar", "aliases": []})
                options.append(rows)
        fingerprints = {tuple(sorted((p["name"] or "", p["email"].casefold()) for p in rows)) for rows in options}
        if options and len(fingerprints) == 1:
            candidates.extend(options[0])
            status = "matched_invitation"
        else:
            status = "ambiguous_invitation" if options else found["status"]
    except Exception:
        # A disconnected calendar must not block transcription or useful notes.
        # Recheck cancellation outside this handler so cancellation still wins.
        status = "unavailable" if invoke else "not_configured"
    if guard:
        guard()
    emails = {p["email"].casefold() for p in candidates if p["email"]}
    names = {p["name"].casefold() for p in candidates if p["name"]}
    for person in learned:
        if (person["email"].casefold() in emails
                or any(n.casefold() in names for n in [person["name"], *person.get("aliases", [])])):
            candidates.append({"name": person["name"], "email": person["email"],
                               "source": "confirmed_correction", "aliases": person.get("aliases", [])})
    unique = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return validate_context({"schema_version": 1, "candidates": unique[:100]}), status
