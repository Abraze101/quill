"""Temporary localhost UI for company-folder and exact notes-sharing decisions."""
from datetime import datetime
import hashlib
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import math
import os
import re
import secrets
import stat
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser
from zoneinfo import ZoneInfo

from .routing import RoutingError, RoutingReviews, StaleReviewError
from .review_ui import document, notes_html, workspace, meeting_url
from .review_library import meeting_library

DELETION_ACTIONS = {"delete_prepare", "delete_confirm"}
MAX_FORM_BYTES = 8192
MAX_NOTES_BYTES = 64 * 1024
SHARING_ACTIONS = {"sharing_prepare", "sharing_confirm", "sharing_private", "sharing_retry",
                   "sharing_suggest", "sharing_use_suggestions"}


def _escape(value):
    return html.escape(str(value), quote=True)


def _selection(job):
    # Stable UI identity, without putting meeting names or job paths in the URL.
    return hashlib.sha256(str(job).encode("utf-8")).hexdigest()[:24]


def _selected_path(server, job, tab="notes"):
    return meeting_url(server.review_path, _selection(job), tab=tab)


def _notes_preview(state, job):
    """Read only the fixed notes member, without following linked directories."""
    if not isinstance(job, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job):
        return None
    directory = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open(state, flags)
        for part in ("results", job, "notes"):
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open("notes.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            data = handle.read(MAX_NOTES_BYTES + 1)
        text = data[:MAX_NOTES_BYTES].decode("utf-8", errors="replace")
        return text + ("\n\n[Preview truncated]" if len(data) > MAX_NOTES_BYTES else "")
    except OSError:
        return None
    finally:
        if directory is not None:
            os.close(directory)


def _page(body):
    return document('<div class="error-page">' + body + '</div>')


def _sharing_link(value):
    """Only a specific native Google Doc can be an outbound review link."""
    if (not isinstance(value, str) or not re.fullmatch(
            r"https://docs\.google\.com/document/d/[A-Za-z0-9_-]+(?:/(?:edit|view))?(?:[?#][^\s]*)?", value)):
        return None
    return '<a href="' + _escape(value) + '" target="_blank" rel="noopener noreferrer">Open notes-only copy</a>'


def _sharing_form(server, item, controls):
    if not server.on_sharing:
        return ""
    result = '<form method="post" action="' + _escape(server.review_path + 'decision') + '">'
    for name in ("job", "revision"):
        result += '<input type="hidden" name="' + name + '" value="' + _escape(item[name]) + '">'
    return result + controls + '</form>'


def _calendar_start(value, display_timezone=None):
    """Use the recording's explicit timezone, or retain the event's offset."""
    if not isinstance(value, str):
        return "Time unavailable"
    literal = value[:160] + ('…' if len(value) > 160 else '')
    if len(value) > 100 or 'T' not in value:
        return literal
    try:
        timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        offset = timestamp.utcoffset()
        if offset is None or offset.total_seconds() % 60:
            return literal
        if isinstance(display_timezone, str):
            try:
                timestamp = timestamp.astimezone(ZoneInfo(display_timezone))
                offset = timestamp.utcoffset()
            except (KeyError, ValueError):
                pass
        if offset.total_seconds() % 60:
            return literal
        minutes = int(offset.total_seconds() // 60)
        hours, remaining = divmod(abs(minutes), 60)
        zone = 'UTC' + ('+' if minutes >= 0 else '-') + f'{hours:02d}:{remaining:02d}'
        return timestamp.strftime('%-m/%-d/%y · %-I:%M %p') + ' (' + zone + ')'
    except (ValueError, OverflowError):
        return literal


def _calendar_suggestions(server, item):
    status = item.get("suggestion_status")
    if status is None or item.get("sharing_started") is True:
        return ""
    body = '<section aria-label="Calendar attendee suggestions"><h4>Calendar attendees</h4>'
    if status == "searching":
        return body + '<p role="status">Looking for matching calendar invitations.</p><p><a href="' + _escape(_selected_path(server, item["job"], tab="sharing")) + '">Refresh suggestions</a></p></section>'
    if status == "idle" and item.get("auto_suggestions") is True:
        body += '<p>For new meetings, calendar attendees are found automatically once notes are filed. You can also look them up here.</p>'
    body += '<p>Use a calendar invitation to fill the email addresses, then review them before sharing.</p>'
    for warning in item.get("suggestion_warnings") or []:
        body += '<p class="muted">' + _escape(warning) + '</p>'
    if item.get("suggestion_scope"):
        body += '<details><summary>Calendars checked</summary><ul>'
        for calendar in item["suggestion_scope"]:
            body += '<li>' + _escape(calendar["name"]) + ' · '
            body += ('Checked' if calendar.get("searched") is True else 'Not checked') + '</li>'
        body += '</ul></details>'
    if status == "needs_attention":
        body += '<p role="alert">' + _escape(item.get("suggestion_error") or "Calendar suggestions are unavailable.") + '</p>'
    if status == "ready":
        events = item.get("suggested_events") or []
        if item.get("suggestion_automatic") is True:
            body += ('<p class="muted">Found automatically from your calendar.</p>' if events
                     else '<p class="muted">Your calendar was checked automatically.</p>')
        if not events:
            body += '<p>No matching calendar invitations were found. You can enter the email addresses below.</p>'
        for event in events:
            body += '<section class="calendar-event"><h5>' + _escape(event["title"]) + '</h5>'
            if event.get("calendar_name"):
                body += '<p class="muted">Calendar: ' + _escape(event["calendar_name"]) + '</p>'
            body += '<p>' + _escape(_calendar_start(event["start"], event.get("display_timezone"))) + '</p>'
            body += '<p>' + _escape(event["match_reason"]) + '</p>'
            body += '<p class="muted">Invited people; attendance unverified.</p><ul>'
            for recipient in event["recipients"]:
                email, name = recipient["email"], recipient.get("name")
                label = (str(name) + ' (' + email + ')') if name else email
                body += '<li>' + _escape(label) + ' · Calendar invite'
                if recipient.get("response_status"):
                    body += ' · ' + _escape(recipient["response_status"])
                body += '</li>'
            body += '</ul>'
            link = event.get("event_url")
            if isinstance(link, str) and re.fullmatch(
                    r"https://(?:calendar|www)\.google\.com/calendar/event\?[^\s]+", link):
                body += '<p><a href="' + _escape(link) + '" target="_blank" rel="noopener noreferrer">Open calendar invitation</a></p>'
            if event["recipients"]:
                controls = '<input type="hidden" name="suggestion" value="' + _escape(event["selection_token"]) + '">'
                controls += '<button name="action" value="sharing_use_suggestions">Use these suggestions</button>'
                body += _sharing_form(server, item, controls)
            body += '</section>'
    label = {"ready": "Refresh calendar suggestions", "needs_attention": "Retry calendar lookup"}.get(
        status, "Find attendees from calendar")
    body += _sharing_form(server, item, '<button name="action" value="sharing_suggest">' + label + '</button>')
    return body + '</section>'


def _sharing_cards(server, items=None, start_index=0):
    if not server.sharing_options:
        return ""
    body = ""
    items = server.sharing_options() if items is None else items
    if not items:
        return '<p class="muted">No filed meetings are waiting for a sharing review.</p>'
    for index, item in enumerate(items, start=start_index):
        status = item["status"]
        heading = {"available": "Share meeting notes", "ready": "Approve notes sharing",
                   "preparing": "Preparing notes copy", "sharing": "Sharing notes",
                   "shared": "Sharing details", "private": "Notes kept private",
                   "needs_attention": "Sharing needs attention"}.get(status, "Notes sharing")
        body += '<div class="sharing-review"><h3>' + heading + '</h3>'
        body += '<p class="muted">Sharing uses a separate notes-only copy. Your recording and full transcript stay private.</p>'
        if status in ("available", "ready"):
            body += '<details open><summary>Read notes to share</summary>'
            body += notes_html(item["preview"]) + '</details>'
            if status == "available":
                body += _calendar_suggestions(server, item)
        if status == "available":
            body += '<p>Enter the exact email addresses to review. Preparing a copy does not share it.</p>'
            controls = '<label for="sharing-emails-' + str(index) + '">Recipient email addresses</label>'
            controls += '<textarea required name="emails" id="sharing-emails-' + str(index) + '" maxlength="4000" autocomplete="off" spellcheck="false">'
            controls += _escape(item.get("recipient_draft") or '') + '</textarea>'
            controls += '<p class="muted">Separate addresses with commas or new lines.</p>'
            controls += '<button name="action" value="sharing_prepare">Prepare private sharing copy</button>'
            # Keep private has its own form, so the recipient field is never
            # submitted with an action that does not accept recipients.
            body += _sharing_form(server, item, controls)
            body += _sharing_form(server, item, '<button name="action" value="sharing_private">Keep private</button>')
        elif status in ("preparing", "sharing"):
            message = ('Preparing your private notes-only copy. Nothing has been shared.' if status == "preparing"
                       else 'Sharing is in progress. Wait for the result before making another choice.')
            body += '<p role="status">' + message + '</p><p><a href="' + _escape(_selected_path(server, item["job"], tab="sharing")) + '">Refresh status</a></p>'
        elif status in ("ready", "shared"):
            link = _sharing_link(item.get("draft_url"))
            body += '<p><strong>' + ('Share view-only with:' if status == "ready" else 'Shared view-only with:') + '</strong></p><ul>'
            for recipient in item["recipients"]:
                body += '<li>' + _escape(recipient) + '</li>'
            body += '</ul>'
            if link:
                body += ('<p class="muted">Preview copy — no document created.</p>'
                         if getattr(server, "preview_mode", False) else '<p>' + link + '</p>')
            else:
                body += '<p role="alert">The notes copy link is unavailable. Reload this review before sharing.</p>'
            if status == "ready":
                body += '<p>Google may email these recipients an invitation. This shares only the notes shown above.</p>'
                controls = ('<button name="action" value="sharing_private">Keep private</button>'
                            if item.get("cancellable") is True else '')
                if link and item["recipients"] and item.get("recipient_draft") is None:
                    controls = '<button name="action" value="sharing_confirm">Share view-only</button>' + controls
                if item.get("recipient_draft") is not None:
                    body += '<p>Save the suggested email addresses below, then review the recipient list before sharing.</p>'
                if controls:
                    body += _sharing_form(server, item, controls)
                if item.get("cancellable") is True:
                    body += '<details' + (' open' if item.get("recipient_draft") is not None else '') + '><summary>Change recipients</summary>'
                    controls = '<label for="sharing-change-' + str(index) + '">Recipient email addresses</label>'
                    controls += '<textarea required name="emails" id="sharing-change-' + str(index) + '" maxlength="4000" autocomplete="off" spellcheck="false">'
                    draft = item.get("recipient_draft")
                    controls += _escape(draft if draft is not None else '\n'.join(item["recipients"])) + '</textarea>'
                    controls += '<p class="muted">Separate addresses with commas or new lines. You will review the new list before sharing.</p>'
                    controls += '<button name="action" value="sharing_prepare">Save recipients for review</button>'
                    body += _sharing_form(server, item, controls) + '</details>'
                suggestions = _calendar_suggestions(server, item)
                if suggestions:
                    body += '<details><summary>Calendar suggestions</summary>' + suggestions + '</details>'
            else:
                body += '<p>Sharing is complete. No new invitation will be sent from this review.</p>'
        elif status == "private":
            body += '<p>Kept private. No sharing is scheduled.</p>'
        elif status == "needs_attention":
            body += '<p role="alert">' + _escape(item.get("error") or "Sharing needs review.") + '</p>'
            if item.get("sharing_started") is True:
                verified = item.get("verified_recipients") or []
                if verified:
                    body += '<p><strong>Access already verified for:</strong></p><ul>'
                    body += ''.join('<li>' + _escape(email) + '</li>' for email in verified) + '</ul>'
                body += '<p>Some invitations may already have been sent.'
                if item.get("retryable") is True:
                    body += ' Retry checks existing access before continuing.'
                body += '</p>'
            if item.get("retryable") is True:
                body += _sharing_form(server, item, '<button name="action" value="sharing_retry">Retry this sharing step</button>')
            if item.get("cancellable") is True:
                body += _sharing_form(server, item, '<button name="action" value="sharing_private">Keep private</button>')
        else:
            body += '<p role="alert">This sharing review is unavailable. Reload the page before making a choice.</p>'
        body += '</div>'
    return body


def _notes_heading(notes, fallback="Meeting awaiting a folder"):
    return next((line.lstrip("# ")[:180] for line in (notes or "").splitlines()
                 if line.strip()), fallback)


def _identity_form(server, record):
    body = '<form method="post" action="' + _escape(server.review_path + 'decision') + '">'
    for name in ("job", "revision"):
        body += '<input type="hidden" name="' + name + '" value="' + _escape(record[name]) + '">'
    return body


def _folder_body(server, record, index):
    suggestion = record["suggestion"]
    suggested = next((company["name"] for company in record["companies"]
                      if company["id"] == suggestion["company_id"]), "Needs your choice")
    body = '<div class="suggestion"><p class="suggestion-title"><strong>Suggested folder:</strong> ' + _escape(suggested) + '</p>'
    body += '<p>' + _escape(suggestion["reason"]) + '</p></div>'
    if suggestion["ambiguous"]:
        body += '<p class="muted">The destination is unclear. Review the notes and choose a folder.</p>'
    body += _identity_form(server, record)
    body += '<label for="company-' + str(index) + '">Meeting folder</label>'
    body += '<select required name="company_id" id="company-' + str(index) + '"><option value="">Choose a folder…</option>'
    for company in record["companies"]:
        body += '<option value="' + _escape(company["id"]) + '">' + _escape(company["name"]) + '</option>'
    body += '</select><div><button name="action" value="approve">Use this folder</button>'
    body += '<button name="action" value="skip" formnovalidate>Keep local</button></div></form>'
    body += '<p class="muted">Keep local saves this meeting on your Mac without filing it in Drive.</p>'
    return '<details><summary>Choose folder</summary>' + body + '</details>'


def _deletion_controls(server, item):
    body = '<div class="local-removal"><h3>Local meeting files</h3>'
    if item.get("cancelling"):
        body += '<p class="muted">Stopping processing… This meeting will move to Trash automatically once its files are closed.</p>'
    elif item.get("available"):
        label = "Retry local removal…" if item.get("interrupted") else "Delete local meeting…"
        body += '<p class="muted">Move this meeting’s recording, transcript and notes to Trash. Existing Drive copies stay as they are.</p>'
        body += _identity_form(server, item)
        body += '<button class="danger" name="action" value="delete_prepare">' + label + '</button></form>'
    else:
        body += '<p class="muted">' + _escape(item.get("detail") or "Local deletion is unavailable while recording or processing.") + '</p>'
    return body + '</div>'


def _deletion_confirmation(server, item):
    body = '<h1>Move this meeting to Trash?</h1><h2>' + _escape(item["title"]) + '</h2>'
    body += '<p>This moves the entire local meeting folder, including its recording, transcript and notes, plus Quill’s queued copies, to macOS Trash.</p>'
    body += '<p>Processing and retries for this meeting will stop. Existing Drive files and shared notes stay as they are.</p>'
    body += '<p class="muted">' + _escape(item["folder"]) + '</p>'
    body += _identity_form(server, item)
    body += '<input type="hidden" name="confirmation" value="' + _escape(item["confirmation"]) + '">'
    body += '<button class="danger" name="action" value="delete_confirm">Move entire meeting to Trash</button></form>'
    body += '<p><a href="' + _escape(_selected_path(server, item["job"])) + '">Cancel and keep this meeting</a></p>'
    return _page(body)


def _resume_body(server, record, stages):
    route = record["approved_route"]
    labels = {"route": "company routing", "archive": "recording upload", "publish": "notes upload"}
    body = '<p><strong>Approved folder:</strong> ' + _escape(route["company_name"]) + '</p>'
    body += '<p class="muted">Resume ' + _escape(", ".join(labels[stage] for stage in stages)) + ' for this meeting only.</p>'
    body += _identity_form(server, record)
    for name, value in (("company_id", route["company_id"]), ("stages", ",".join(stages))):
        body += '<input type="hidden" name="' + name + '" value="' + _escape(value) + '">'
    return body + '<button name="action" value="resume">Resume waiting work</button></form>'


def _review_entries(server):
    """Resolve one presentation state without changing any workflow decision."""
    reviews = RoutingReviews(server.review_state)
    try:
        routing = {record["job"]: reviews.get(record["job"]) for record in reviews.list()}
    finally:
        reviews.close()
    sources = {}

    def source_for(job):
        return sources.setdefault(job, {"processing": []})

    for job, record in routing.items():
        if record["status"] == "pending":
            source_for(job)["folder"] = record
        elif record["status"] == "approved" and server.resume_options and server.on_resume:
            try:
                stages = server.resume_options(record)
            except Exception:
                # A changed source or configuration must never expose a release.
                stages = []
            if stages:
                source_for(job)["resume"] = (record, stages)

    for index, item in enumerate(server.processing_options() if server.processing_options else []):
        source_for(item.get("job", ("worker", index)))["processing"].append(item)
    for index, item in enumerate(server.sharing_options() if server.sharing_options else []):
        source_for(item["job"])["sharing"] = (index, item)
    if server.deletion_options and server.on_delete:
        for item in server.deletion_options():
            if item["job"] in sources or item.get("available"):
                source_for(item["job"])["deletion"] = item

    entries = []
    for index, (job, source) in enumerate(sources.items()):
        record = routing.get(job)
        pending = source.get("folder")
        processing = source["processing"]
        sharing_index, sharing = source.get("sharing", (0, None))
        deletion = source.get("deletion")
        notes = _notes_preview(server.review_state, job)
        if notes is None and sharing:
            notes = sharing.get("preview")
        fallback = (sharing.get("title") if sharing else None) or next(
            (item.get("title") for item in processing if item.get("title")), None)
        fallback = fallback or (deletion.get("title") if deletion else None) or "Saved meeting"
        entry = {"job": job, "title": _notes_heading(notes, fallback), "status": "On this Mac",
                 "detail": "Local files are available. Drive and sharing status are not established by this view.",
                 "tone": "neutral", "priority": 6, "attention": False, "selection": _selection(job),
                 "body": "", "notes": notes, "sharing_body": "", "deletion_body": "", "metadata": [],
                 "sharing_status": "unknown", "sharing_label": "No sharing confirmation available"}
        if record and record.get("companies"):
            entry["companies"] = record["companies"]
        if record and record.get("approved_route"):
            route = record["approved_route"]
            entry.update(folder_id=route["company_id"], folder_name=route["company_name"])
        if sharing:
            entry["sharing_body"] = _sharing_cards(server, [sharing], start_index=sharing_index)
            entry["sharing_status"] = sharing["status"]
            entry["sharing_label"] = {
                "available": "Not shared through Quill",
                "preparing": "Preparing a private notes copy",
                "ready": "Private copy awaiting approval",
                "sharing": "Sharing started; confirmation pending",
                "shared": "View-only sharing confirmed",
                "private": "Kept private; no sharing scheduled",
                "needs_attention": "Sharing result needs review",
            }.get(sharing["status"], "No sharing confirmation available")
        if deletion:
            entry["deletion_body"] = _deletion_controls(server, deletion)
            local_folder = deletion.get("local_folder") or deletion.get("folder")
            if isinstance(local_folder, str) and local_folder:
                entry["local_folder"] = local_folder

        active = next((item for item in processing if item.get("state") in ("running", "retrying", "queued")), None)
        errors = [item for item in processing if item.get("state") not in
                  ("running", "retrying", "queued", "waiting_for_company", "idle")]
        process = active or (errors[0] if errors else None)
        deletion_pending = deletion and (deletion.get("cancelling") or deletion.get("interrupted"))
        if deletion_pending:
            cancelling = bool(deletion.get("cancelling"))
            entry.update(status="Stopping processing…" if cancelling else "Removal needs attention",
                         detail=deletion.get("detail") or (
                             "This meeting will move to Trash after its files are closed." if cancelling
                             else "Processing is stopped. Retry local removal to finish moving the files to Trash."),
                         tone="amber" if cancelling else "red", priority=-1, attention=not cancelling,
                         sharing_body="")
            # Cancellation takes precedence over every old retry, folder and
            # sharing form. Its own removal action remains in deletion_body.
        elif process:
            state = process.get("state", "needs_attention")
            status, tone, priority, attention = {
                "running": ("Processing", "neutral", 3, False),
                "retrying": ("Waiting to retry", "amber", 3, False),
                "queued": ("Waiting", "neutral", 3, False),
                "needs_attention": ("Needs attention", "red", 0, True),
            }.get(state, ("Check status", "red", 0, True))
            details = list(dict.fromkeys(item.get("detail", "") for item in [process] + processing
                                        if item.get("detail")))
            entry.update(status=status, detail=" ".join(details), tone=tone,
                         priority=priority, attention=attention or bool(errors))
            # Unknown and failed processing never becomes a generic Resume.
            # A retry button is shown only when the processing adapter permits it.
            if not active and process.get("retryable") and server.on_retry:
                entry["body"] = (_identity_form(server, process) +
                                 '<button name="action" value="retry">Retry this meeting</button></form>')
        elif pending:
            entry.update(status="Choose a folder", detail="Your notes are ready. Choose where to file this meeting.",
                         tone="amber", priority=1, attention=True,
                         body=_folder_body(server, pending, index))
        elif source.get("resume"):
            approved, stages = source["resume"]
            entry.update(status="Ready to resume", detail="Your folder choice is saved. Some work is still on hold.",
                         tone="amber", priority=1, attention=True,
                         body=_resume_body(server, approved, stages))
        elif any(item.get("state") == "waiting_for_company" for item in processing):
            entry.update(status="Check status", detail="The saved folder review is unavailable. Reload before continuing.",
                         tone="red", priority=0, attention=True)
        elif sharing:
            status, detail, tone, priority, attention = {
                "available": ("Notes ready", "Sharing is optional. Your recording and transcript stay private.", "neutral", 5, False),
                "ready": ("Approve sharing", "Review the prepared notes-only copy and its exact recipients.", "amber", 2, True),
                "preparing": ("Preparing notes copy", "The separate sharing copy is being prepared. Nothing has been shared.", "neutral", 3, False),
                "sharing": ("Sharing in progress", "Wait for verified access before making another sharing choice.", "neutral", 3, False),
                "shared": ("Notes shared", "View-only access was confirmed for the recipients in Sharing details.", "neutral", 5, False),
                "private": ("Notes kept private", "No notes sharing is scheduled.", "neutral", 5, False),
                "needs_attention": ("Sharing needs attention", sharing.get("error") or "Review the saved sharing result before continuing.", "red", 0, True),
            }.get(sharing["status"], ("Check sharing", "The sharing result needs review.", "red", 0, True))
            entry.update(status=status, detail=detail, tone=tone, priority=priority, attention=attention)
        elif record and record["status"] == "skipped":
            entry.update(status="Kept on this Mac", detail="You chose to keep this meeting local without filing it in Drive.")

        # Sharing remains an independent action, but a prepared approval or a
        # sharing failure must still be discoverable in the attention filter.
        if sharing and sharing["status"] in ("ready", "needs_attention") and not deletion_pending:
            entry["attention"] = True
            if process or pending or source.get("resume"):
                entry["metadata"].append("Sharing also needs review.")
        entries.append(entry)
    return sorted(entries, key=lambda entry: entry["priority"])


def _reviews_page(server, notice=None, selected=None, **navigation):
    entries = _review_entries(server)
    known = {entry["selection"]: entry for entry in entries}
    for job, saved in meeting_library(server.review_state).items():
        selection = _selection(job)
        entry = known.get(selection)
        if entry is None:
            has_content = any(saved.get(key) for key in (
                "title", "display_date", "transcript_text", "recording_files"))
            has_saved_copy = any(item.get("status") == "confirmed"
                                 for item in saved.get("storage", {}).values())
            # Old queue rows and their remembered paths can outlive cleanup.
            # They alone are not evidence that a meeting is still available.
            if not has_content and not has_saved_copy:
                continue
            entry = {"job": job, "selection": selection, "title": saved.get("title") or "Saved meeting",
                     "status": "On this Mac", "detail": "", "tone": "neutral", "attention": False,
                     "body": "", "notes": _notes_preview(server.review_state, job), "metadata": []}
            entries.append(entry)
            known[selection] = entry
        if saved.get("title"):
            entry["title"] = saved["title"]
        for key in ("display_date", "recorded_at", "participants", "storage_status", "storage",
                    "transcript_text", "local_folder", "recording_files"):
            if saved.get(key) is not None:
                entry[key] = saved[key]
        if saved.get("company_id") and saved.get("folder_name"):
            entry.update(folder_id=saved["company_id"], folder_name=saved["folder_name"])
        metadata = ([", ".join(saved["participants"])] if saved.get("participants") else [])
        if saved.get("display_date"):
            metadata.append(saved["display_date"])
        entry["metadata"] = metadata + entry.get("metadata", [])
        if entry["status"] in ("On this Mac", "Notes ready"):
            if saved["storage_status"] == "saved_in_drive":
                entry.update(status="Saved in Drive", detail="Private filing is confirmed in the saved receipts.")
            elif saved["storage_status"] == "partly_saved":
                entry.update(status="Partly saved in Drive", detail="Some files have confirmed Drive saves. View activity for details.")
            elif saved["storage_status"] == "not_requested":
                entry.update(status="Kept on this Mac", detail="Drive filing was not requested for this meeting.")
    companies = {}
    for entry in entries:
        for company in entry.get("companies", []):
            companies[company["id"]] = {"id": company["id"], "name": company["name"]}
    return workspace(entries, server.review_path, notice, bool(server.sharing_options), getattr(server, "preview_mode", False), selected,
                     companies=list(companies.values()), **navigation)


class _ReviewHandler(BaseHTTPRequestHandler):
    # Do not write the token URL or meeting contents into server access logs.
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def _send(self, status, document, location=None):
        data = document.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # Native browser POSTs use Origin: null under no-referrer. Keep the
        # token path out of referrers while retaining a verifiable local Origin.
        self.send_header("Referrer-Policy", "strict-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, message):
        self._send(status, _page("<h1>Meeting review</h1><p>" + _escape(message) + "</p>"))

    def _trusted_request(self, post=False):
        if self.headers.get_all("Host", []) != [self.server.review_host]:
            self._error(403, "This request did not come through the local review address.")
            return False
        if post and self.headers.get_all("Origin", []) != [self.server.review_origin]:
            self._error(403, "Open the local review page before saving a choice.")
            return False
        return True

    def do_GET(self):
        if not self._trusted_request():
            return
        request = urlsplit(self.path)
        try:
            query = (parse_qs(request.query, keep_blank_values=True, strict_parsing=True,
                              encoding="utf-8", errors="strict", max_num_fields=5) if request.query else {})
            valid_query = (len(request.query) <= 2400
                           and set(query) <= {"meeting", "view", "folder", "q", "tab"}
                           and all(len(values) == 1 for values in query.values()))
            values = {key: value[0] for key, value in query.items()}
            if "meeting" in values:
                valid_query = valid_query and bool(re.fullmatch(r"[a-f0-9]{24}", values["meeting"]))
            if "view" in values:
                valid_query = valid_query and values["view"] in ("all", "attention")
            if "folder" in values:
                valid_query = valid_query and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", values["folder"]))
            if "q" in values:
                valid_query = valid_query and len(values["q"]) <= 160 and not any(ord(c) < 32 or ord(c) == 127 for c in values["q"])
            if "tab" in values:
                valid_query = valid_query and "meeting" in values and values["tab"] in ("notes", "transcript", "recording", "sharing", "activity")
        except (ValueError, UnicodeError):
            valid_query = False
        if (request.scheme or request.netloc or request.fragment
                or request.path != self.server.review_path or not valid_query):
            self._error(404, "Review page not found.")
            return
        try:
            self._send(200, _reviews_page(self.server, selected=values.get("meeting"),
                                        view=values.get("view", "all"), folder=values.get("folder"),
                                        query=values.get("q", ""), tab=values.get("tab", "notes")))
        except (RoutingError, OSError, ValueError):
            self._error(500, "The saved reviews could not be loaded.")

    def do_POST(self):
        if not self._trusted_request(post=True):
            return
        if self.path != self.server.review_path + "decision":
            self._error(404, "Review action not found.")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not lengths[0].isdigit() or int(lengths[0]) > MAX_FORM_BYTES
                or self.headers.get("Transfer-Encoding") is not None):
            self._error(413, "The review form is too large or incomplete.")
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            self._error(415, "Use the form on the review page.")
            return
        try:
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise ValueError("Incomplete form")
            fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True,
                              encoding="utf-8", errors="strict", max_num_fields=5)
            if any(len(values) != 1 for values in fields.values()):
                raise ValueError("Unexpected form fields")
            form = {key: values[0] for key, values in fields.items()}
            if form.get("action") not in {"approve", "skip", "resume", "retry"} | SHARING_ACTIONS | DELETION_ACTIONS:
                raise ValueError("Unknown action")
            expected = {"job", "revision", "company_id", "action"}
            if form["action"] == "retry" or form["action"] in DELETION_ACTIONS:
                expected.remove("company_id")
            if form["action"] == "delete_confirm":
                expected.add("confirmation")
            if form["action"] == "resume":
                expected.add("stages")
            if form["action"] in SHARING_ACTIONS:
                expected.remove("company_id")
                if form["action"] == "sharing_prepare":
                    expected.add("emails")
                if form["action"] == "sharing_use_suggestions":
                    expected.add("suggestion")
            if set(form) != expected:
                raise ValueError("Unexpected form fields")
            if form["action"] in SHARING_ACTIONS and (not form["job"] or not form["revision"]):
                raise ValueError("Missing sharing review identity")
            if form["action"] == "sharing_use_suggestions" and not form["suggestion"]:
                raise ValueError("Missing calendar suggestion")
            if form["action"] == "resume":
                stages = form["stages"].split(",")
                if (not stages or len(stages) > 3 or len(set(stages)) != len(stages)
                        or any(stage not in ("route", "archive", "publish") for stage in stages)):
                    raise ValueError("Unexpected resume stages")
        except (ValueError, UnicodeError, OSError):
            self._error(400, "The review form could not be read. Reload the page and try again.")
            return
        if form["action"] in DELETION_ACTIONS:
            try:
                if not self.server.on_delete or not form["job"] or not form["revision"]:
                    raise ValueError("Local deletion is unavailable")
                item = self.server.on_delete(form["action"], form["job"], form["revision"],
                                             confirmation=form.get("confirmation"))
            except Exception as error:
                self._send(409, _page('<h1>Local removal needs review</h1><p>' + _escape(str(error)[:500])
                                     + '</p><p><a href="' + _escape(_selected_path(self.server, form["job"]))
                                     + '">Back to this meeting</a></p>'))
                return
            if form["action"] == "delete_prepare":
                self._send(200, _deletion_confirmation(self.server, item))
            else:
                destination = (_selected_path(self.server, form["job"]) if item.get("status") == "cancelling"
                               else self.server.review_path)
                self._send(303, "", location=destination)
            return
        if form["action"] in SHARING_ACTIONS:
            try:
                if not self.server.on_sharing:
                    raise ValueError("Sharing is unavailable")
                # The callback validates the exact saved revision and persists
                # a decision before starting any background Drive operation.
                keywords = {"emails": form.get("emails")}
                if form["action"] == "sharing_use_suggestions":
                    keywords["suggestion"] = form["suggestion"]
                self.server.on_sharing(form["action"], form["job"], form["revision"], **keywords)
            except Exception as error:
                self._send(409, _page('<h1>Sharing needs review</h1><p>' + _escape(str(error)[:500])
                                     + '</p><p><a href="' + _escape(self.server.review_path)
                                     + '">Back to meeting reviews</a></p>'))
                return
            self._send(303, "", location=_selected_path(self.server, form["job"], tab="sharing"))
            return
        if form["action"] == "retry":
            try:
                if not self.server.on_retry:
                    raise ValueError("Retry unavailable")
                self.server.on_retry(form["job"], form["revision"])
            except Exception as error:
                self._send(409, _page('<h1>Retry needs review</h1><p>' + _escape(str(error)[:500])
                                     + '</p><p><a href="' + _escape(self.server.review_path)
                                     + '">Back to meeting reviews</a></p>'))
                return
            self._send(303, "", location=_selected_path(self.server, form["job"]))
            return
        reviews = RoutingReviews(self.server.review_state)
        try:
            if form["action"] == "resume":
                record = reviews.get(form["job"])
                if (not self.server.on_resume or not record or record["status"] != "approved"
                        or record["revision"] != form["revision"]
                        or record["approved_route"]["company_id"] != form["company_id"]):
                    raise StaleReviewError("Resume requires this exact approved review")
            elif form["action"] == "approve":
                record = reviews.approve(form["job"], form["company_id"], form["revision"])
            else:
                record = reviews.skip(form["job"], form["revision"])
        except StaleReviewError:
            self._send(409, _page('<h1>Meeting review has changed</h1><p>Open the current review before choosing a folder.</p><p><a href="'
                                 + _escape(self.server.review_path) + '">Back to reviews</a></p>'))
            return
        except RoutingError:
            self._error(400, "That choice is unavailable. Reload the review page and choose a listed folder.")
            return
        finally:
            reviews.close()
        callback = self.server.on_resume if form["action"] == "resume" else self.server.on_decision
        if callback:
            try:
                # The decision is committed first. The callback must tolerate a
                # repeated request; it may be invoked again after a browser retry.
                if form["action"] == "resume":
                    callback(record, stages)
                else:
                    callback(record)
            except Exception:
                self._send(200, _reviews_page(self.server, "Your choice was saved. Follow-up processing could not start and can be retried.",
                                             selected=_selection(form["job"])))
                return
        self._send(303, "", location=_selected_path(self.server, form["job"]))


def create_server(state, on_decision=None, resume_options=None, on_resume=None,
                  processing_options=None, on_retry=None, sharing_options=None, on_sharing=None,
                  deletion_options=None, on_delete=None):
    """Create an unstarted loopback server. Caller owns shutdown/server_close."""
    for callback in (on_decision, resume_options, on_resume, processing_options, on_retry,
                     sharing_options, on_sharing, deletion_options, on_delete):
        if callback is not None and not callable(callback):
            raise TypeError("Review callbacks must be callable")
    reviews = RoutingReviews(state)
    try:
        review_state = reviews.state.absolute()
    finally:
        reviews.close()
    server = HTTPServer(("127.0.0.1", 0), _ReviewHandler)
    server.review_state = review_state
    server.review_host = "127.0.0.1:" + str(server.server_address[1])
    server.review_origin = "http://" + server.review_host
    server.review_path = "/" + secrets.token_urlsafe(32) + "/"
    server.review_url = server.review_origin + server.review_path
    server.on_decision = on_decision
    server.resume_options = resume_options
    server.on_resume = on_resume
    server.processing_options = processing_options
    server.on_retry = on_retry
    server.sharing_options = sharing_options
    server.on_sharing = on_sharing
    server.deletion_options = deletion_options
    server.on_delete = on_delete
    return server


def serve_review(state, on_decision=None, open_browser=True, timeout_seconds=1800, on_ready=None,
                 resume_options=None, on_resume=None, processing_options=None, on_retry=None,
                 sharing_options=None, on_sharing=None, deletion_options=None, on_delete=None):
    """Serve until timeout. Closing the UI never discards saved decisions."""
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 86400):
        raise ValueError("Review timeout must be between zero and 86400 seconds")
    server = create_server(state, on_decision, resume_options, on_resume, processing_options, on_retry,
                           sharing_options, on_sharing, deletion_options=deletion_options, on_delete=on_delete)
    timer = threading.Timer(timeout_seconds, server.shutdown)
    timer.daemon = True
    try:
        if on_ready:
            on_ready(server.review_url)
        if open_browser:
            webbrowser.open(server.review_url)
        timer.start()
        server.serve_forever(poll_interval=0.1)
    finally:
        timer.cancel()
        server.server_close()
