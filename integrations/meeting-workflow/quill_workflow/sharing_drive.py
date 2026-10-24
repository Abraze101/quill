"""Reviewed, notes-only copies and exact individual reader grants.

No background worker calls this adapter. The review controller supplies an
immutable snapshot, a durable operation journal, its save callback, and a guard
which rechecks the user's current approval before every connector operation.
"""
import copy
from datetime import datetime
import hashlib
import json
import re
import time

from . import native_template as native
from .codex_sharing_transport import recipient_email
from .drive_upload import (DOCUMENT_MIME, FOLDER_MIME, METADATA_FIELDS,
                           native_template_sha256)
from .pipeline import NeedsConfiguration


class SharingNeedsReview(NeedsConfiguration):
    """A changed or uncertain item needs a fresh review, never a blind write."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", value):
        raise SharingNeedsReview("A fixed Drive document or folder ID is required")
    return value


def _revision(document):
    revision = document.get("revisionId")
    if not isinstance(revision, str) or not revision or len(revision) > 1000:
        raise SharingNeedsReview("The notes document has no usable revision")
    return revision


def _snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise SharingNeedsReview("A reviewed notes snapshot is required")
    snapshot = copy.deepcopy(snapshot)
    for key in ("template_document_id", "destination_folder_id", "private_document_id"):
        _id(snapshot.get(key))
    if snapshot["template_document_id"] == snapshot["private_document_id"]:
        raise SharingNeedsReview("The private combined document cannot be used as a sharing template")
    if not isinstance(snapshot.get("template_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot["template_sha256"]):
        raise SharingNeedsReview("The approved native template needs its saved content hash")
    title = snapshot.get("title")
    if (not isinstance(title, str) or not title.strip() or len(title.encode()) > 500
            or any(ord(char) < 32 for char in title)):
        raise SharingNeedsReview("The notes title is invalid")
    snapshot["owner"] = recipient_email(snapshot.get("owner"))
    payload = snapshot.get("notes_payload")
    if not isinstance(payload, dict) or payload.get("format") != native.FORMAT:
        raise SharingNeedsReview("A validated native notes payload is required")
    replacements = payload.get("replacements")
    if not isinstance(replacements, dict):
        raise SharingNeedsReview("Notes sections are missing")
    # Never place actual transcript text or an original-document link into a
    # shared copy, including its revision history. The blank template is the
    # only document ever copied. Notes content was reviewed before this call.
    replacements["{{QUILL_TRANSCRIPT}}"] = "Not included."
    replacements["{{QUILL_ATTACHMENTS}}"] = "Meeting notes only."
    native._payload(payload)
    return snapshot


def notes_plan(template, payload):
    """Fill the approved native layout and remove its entire transcript tail."""
    tab_id, expanded, _spans, paragraphs, _ = native._expanded(template, payload)
    if paragraphs[-1]["text"] != "{{QUILL_TRANSCRIPT}}\n":
        raise SharingNeedsReview("The notes template must end with its transcript placeholder")
    cut = len(paragraphs) - 1
    while cut > 0 and not paragraphs[cut - 1]["text"].strip():
        cut -= 1
    if cut > 0 and paragraphs[cut - 1]["text"].strip().casefold() in {"transcript", "full transcript"}:
        cut -= 1
    if cut < 1:
        raise SharingNeedsReview("The native notes template has no notes section")
    kept = []
    for paragraph in paragraphs[:cut]:
        text = paragraph["text"]
        for token, replacement in payload["replacements"].items():
            text = text.replace(token, native.DATE_CHARACTER if token == "{{QUILL_DATE}}" else replacement)
        kept.append(text)
    expected = "".join(kept)
    requests = native.plan_requests(template, payload)
    # Retain the required final newline and merge with the preceding notes
    # paragraph. Deleting a Docs document's terminal newline is invalid.
    requests.append({"deleteContentRange": {"range": {"tabId": tab_id,
        "startIndex": native._utf16(expected), "endIndex": native._utf16(expanded)}}})
    return {"requests": requests, "text": expected, "paragraph_count": cut}


def verify_notes(document, template, payload):
    plan = notes_plan(template, payload)
    tab_id, actual = native._body(document)
    original_tab, original = native._body(template)
    if tab_id != original_tab or native.document_text(document) != plan["text"]:
        raise SharingNeedsReview("The shared copy differs from the reviewed notes-only content")
    before, after = native.style_signature(template), native.style_signature(document)
    if any(before[key] != after[key] for key in ("documentStyle", "namedStyles")):
        raise SharingNeedsReview("The shared copy lost its native template formatting")
    source_outer, source_tab = native._tab_data(template)
    result_outer, result_tab = native._tab_data(document)
    source_lists = source_tab.get("lists") or source_outer.get("lists") or {}
    result_lists = result_tab.get("lists") or result_outer.get("lists") or {}
    expected_paragraphs = []
    for paragraph in original[:plan["paragraph_count"]]:
        text = paragraph["text"]
        for token, replacement in payload["replacements"].items():
            text = text.replace(token, native.DATE_CHARACTER if token == "{{QUILL_DATE}}" else replacement)
        expected_paragraphs.extend((part + "\n", paragraph["paragraph"]) for part in text[:-1].split("\n"))
    if len(actual) != len(expected_paragraphs):
        raise SharingNeedsReview("The shared copy has unexpected paragraphs")
    for result, (text, source) in zip(actual, expected_paragraphs):
        paragraph = result["paragraph"]
        if (result["text"] != text
                or native._without_generated_ids(paragraph.get("paragraphStyle", {})) != native._without_generated_ids(source.get("paragraphStyle", {}))
                or paragraph.get("bullet") != source.get("bullet")):
            raise SharingNeedsReview("The shared copy's paragraph or list formatting changed")
        bullet = source.get("bullet")
        if bullet and source_lists.get(bullet["listId"]) != result_lists.get(bullet["listId"]):
            raise SharingNeedsReview("The shared copy's native list formatting changed")
        source_style = source["elements"][0].get("textRun", {}).get("textStyle", {})
        for element in paragraph.get("elements", []):
            run = element.get("textRun")
            if not run or run.get("content") == "\n":
                continue
            if any(key in source_style and run.get("textStyle", {}).get(key) != source_style[key]
                   for key in ("weightedFontFamily", "fontSize", "foregroundColor")):
                raise SharingNeedsReview("The shared copy's font formatting changed")
    dates = [date for paragraph in actual for date in paragraph["dates"]]
    if len(dates) != 1:
        raise SharingNeedsReview("The shared notes must retain their meeting date")
    props = dates[0].get("dateElementProperties", {})
    try:
        actual_date = datetime.fromisoformat(props["timestamp"].replace("Z", "+00:00"))
        expected_date = datetime.fromisoformat(payload["date"]["timestamp"].replace("Z", "+00:00"))
    except (TypeError, ValueError, KeyError) as error:
        raise SharingNeedsReview("The shared notes date is unreadable") from error
    if (actual_date != expected_date
            or props.get("timeFormat", "TIME_FORMAT_DISABLED") != "TIME_FORMAT_DISABLED"
            or props.get("timeZoneId", "etc/UTC") not in ("UTC", "etc/UTC", "Etc/UTC", None, "")):
        raise SharingNeedsReview("The shared notes date changed")
    return True


class SharingDrive:
    def __init__(self, invoke):
        self.invoke = invoke

    def _call(self, tool, arguments, guard):
        if not callable(guard):
            raise SharingNeedsReview("A current review approval guard is required")
        guard()
        result = self.invoke(tool, arguments)
        if not isinstance(result, dict):
            raise SharingNeedsReview("Drive returned an unreadable result")
        return result

    def _metadata(self, identity, guard):
        item = self._call("get_file_metadata", {"fileId": _id(identity), "fields": METADATA_FIELDS}, guard)
        if item.get("id") != identity:
            raise SharingNeedsReview("Drive returned a different item")
        return item

    def _permissions(self, item, owner, allowed=()):
        permissions = item.get("permissions")
        if (item.get("drive_id") or item.get("current_user_can_share") is not True
                or not isinstance(permissions, list) or not permissions):
            raise SharingNeedsReview("Drive ownership and sharing permissions could not be verified")
        seen, owners = {}, 0
        for permission in permissions:
            if not isinstance(permission, dict) or permission.get("type") != "user" or permission.get("deleted"):
                raise SharingNeedsReview("The notes location has public, domain, group, or unreadable access")
            email = recipient_email(permission.get("emailAddress"))
            role = permission.get("role")
            if email in seen:
                raise SharingNeedsReview("Duplicate Drive permissions need review")
            if role == "owner" and email == owner:
                owners += 1
            elif role != "reader" or email not in allowed:
                raise SharingNeedsReview("The notes location has access outside this approval")
            seen[email] = copy.deepcopy(permission)
        if owners != 1 or item.get("shared") is not (len(seen) > 1):
            raise SharingNeedsReview("Drive privacy or owner information is inconsistent")
        return {email: permission for email, permission in seen.items() if email != owner}

    def _drive_root(self, owner, guard):
        root = self._call("get_file_metadata", {"fileId": "root", "fields": METADATA_FIELDS}, guard)
        _id(root.get("id"))
        self._permissions(root, owner)
        if (root.get("mime_type") != FOLDER_MIME or "parent_ids" not in root
                or root["parent_ids"] not in (None, [])):
            raise SharingNeedsReview("The authenticated Drive root could not be verified")
        return root

    def _ancestors(self, folder, owner, guard):
        seen = set()
        for _ in range(12):
            if folder == "root":
                self._drive_root(owner, guard)
                return
            if folder in seen:
                raise SharingNeedsReview("The destination folder ancestry is invalid")
            seen.add(folder)
            item = self._metadata(folder, guard)
            self._permissions(item, owner)
            if item.get("mime_type") != FOLDER_MIME:
                raise SharingNeedsReview("The notes destination is not a private folder")
            if "parent_ids" not in item:
                raise SharingNeedsReview("The notes destination ancestry is unreadable")
            parents = item["parent_ids"]
            if parents in (None, []):
                # Connectors represent My Drive's missing API parent as null.
                # Verify the authenticated root ID, never infer from its name
                # or accept an arbitrary folder with absent parent metadata.
                if self._drive_root(owner, guard)["id"] != folder:
                    raise SharingNeedsReview("The notes ancestry does not reach the authenticated Drive root")
                return
            if not isinstance(parents, list) or len(parents) != 1:
                raise SharingNeedsReview("The notes destination ancestry is unreadable")
            folder = parents[0]
        raise SharingNeedsReview("The notes destination ancestry exceeds the review limit")

    def _boundaries(self, snapshot, guard):
        self._ancestors(snapshot["destination_folder_id"], snapshot["owner"], guard)
        original = self._metadata(snapshot["private_document_id"], guard)
        self._permissions(original, snapshot["owner"])
        if original.get("mime_type") != DOCUMENT_MIME or original.get("parent_ids") != [snapshot["destination_folder_id"]]:
            raise SharingNeedsReview("The original private meeting document moved or changed type")

    def _document(self, identity, guard):
        document = self._call("get_document", {"document_id": _id(identity)}, guard)
        if document.get("documentId") != identity:
            raise SharingNeedsReview("Drive returned a different native document")
        _revision(document)
        return document

    def prepare(self, snapshot, journal, save, guard):
        snapshot = _snapshot(snapshot)
        binding = _hash(snapshot)
        if journal and journal.get("snapshot_hash") != binding:
            raise SharingNeedsReview("The saved sharing draft belongs to different reviewed notes")
        if not journal:
            journal.update(schema_version=1, snapshot_hash=binding, copy={"state": "planned"}, fill={"state": "planned"})
            save()
        self._boundaries(snapshot, guard)
        if journal.get("draft"):
            self.verify(snapshot, journal["draft"], guard=guard)
            return copy.deepcopy(journal["draft"])
        template = self._document(snapshot["template_document_id"], guard)
        self._permissions(self._metadata(snapshot["template_document_id"], guard), snapshot["owner"])
        if native_template_sha256(template) != snapshot["template_sha256"]:
            raise SharingNeedsReview("The approved blank notes template changed")
        notes_plan(template, snapshot["notes_payload"])
        op = journal["copy"]
        identity = op.get("id")
        if not identity:
            if op["state"] != "planned":
                raise SharingNeedsReview("A notes copy may exist without a saved ID; no duplicate was created")
            self._boundaries(snapshot, guard)
            op.update(state="dispatched", dispatched_at=time.time())
            save()
            try:
                result = self._call("copy_file", {"url": "https://docs.google.com/document/d/" + snapshot["template_document_id"] + "/edit",
                    "new_title": snapshot["title"], "parent_folder": snapshot["destination_folder_id"]}, guard)
            except Exception as error:
                if getattr(error, "outcome_unknown", True) is False:
                    op["state"] = "planned"
                    save()
                raise
            if result.get("success") is not True:
                raise SharingNeedsReview("The notes copy was not confirmed; no duplicate will be created")
            identity = _id(result.get("id"))
            if identity in {snapshot["private_document_id"], snapshot["template_document_id"]}:
                raise SharingNeedsReview("Drive did not create a separate notes document")
            op.update(id=identity, state="acknowledged")
            save()
        item = self._metadata(identity, guard)
        if identity in {snapshot["private_document_id"], snapshot["template_document_id"]}:
            raise SharingNeedsReview("Only a separate notes copy may be edited")
        self._permissions(item, snapshot["owner"])
        self._location(snapshot, item)
        actual = self._document(identity, guard)
        fill = journal["fill"]
        if fill["state"] == "planned":
            if (native.document_text(actual) != native.document_text(template)
                    or native.style_signature(actual) != native.style_signature(template)):
                raise SharingNeedsReview("The copied blank template changed before notes were inserted")
            fill.update(template=copy.deepcopy(actual), revision=_revision(actual),
                        requests=notes_plan(actual, snapshot["notes_payload"])["requests"])
            save()
        else:
            try:
                verify_notes(actual, fill["template"], snapshot["notes_payload"])
            except (ValueError, SharingNeedsReview):
                if _revision(actual) != fill["revision"] or fill["state"] != "planned":
                    # Even at the same revision, uncertain edits are held:
                    # eventual read consistency does not authorize a repeat.
                    raise SharingNeedsReview("The notes edit is uncertain or changed; inspect the saved copy before retrying")
            else:
                fill["state"] = "verified"
                save()
        if fill["state"] != "verified":
            self._boundaries(snapshot, guard)
            self._permissions(self._metadata(identity, guard), snapshot["owner"])
            current = self._document(identity, guard)
            if _revision(current) != fill["revision"]:
                raise SharingNeedsReview("The blank notes copy changed before editing")
            fill.update(state="dispatched", dispatched_at=time.time())
            save()
            try:
                self._call("batch_update_document", {"document_id": identity, "requests": fill["requests"],
                    "write_control": {"requiredRevisionId": fill["revision"]}}, guard)
            except Exception as error:
                if getattr(error, "outcome_unknown", True) is False:
                    fill["state"] = "planned"
                    save()
                raise
            actual = self._document(identity, guard)
            verify_notes(actual, fill["template"], snapshot["notes_payload"])
            fill["state"] = "verified"
            save()
        draft = {"document_id": identity, "url": "https://docs.google.com/document/d/" + identity + "/edit",
                 "revision": _revision(actual), "content_hash": native_template_sha256(actual),
                 "snapshot_hash": binding, "title": snapshot["title"], "prepared_at": time.time()}
        self.verify(snapshot, draft, guard=guard)
        journal["draft"] = copy.deepcopy(draft)
        save()
        return draft

    def _location(self, snapshot, item):
        if (item.get("mime_type") != DOCUMENT_MIME or item.get("parent_ids") != [snapshot["destination_folder_id"]]
                or item.get("title") != snapshot["title"]):
            raise SharingNeedsReview("The notes draft moved, changed title, or changed type")

    def verify(self, snapshot, draft, recipients=(), guard=None):
        snapshot = _snapshot(snapshot)
        if not isinstance(draft, dict) or draft.get("snapshot_hash") != _hash(snapshot):
            raise SharingNeedsReview("The shared notes snapshot changed")
        identity = _id(draft.get("document_id"))
        if draft.get("url") != "https://docs.google.com/document/d/" + identity + "/edit":
            raise SharingNeedsReview("The shared notes URL does not match the saved draft")
        if identity in {snapshot["private_document_id"], snapshot["template_document_id"]}:
            raise SharingNeedsReview("Only the separate notes copy can be shared")
        self._boundaries(snapshot, guard)
        item = self._metadata(identity, guard)
        self._location(snapshot, item)
        permissions = self._permissions(item, snapshot["owner"], recipients)
        document = self._document(identity, guard)
        if (_revision(document) != draft.get("revision")
                or native_template_sha256(document) != draft.get("content_hash")):
            raise SharingNeedsReview("The notes draft changed after preview; review it again before sharing")
        guard()
        return {"document_id": identity, "permissions": permissions, "revision": _revision(document)}

    def share(self, snapshot, draft, recipients, journal, save, guard):
        snapshot = _snapshot(snapshot)
        if not isinstance(recipients, (list, tuple)) or not 1 <= len(recipients) <= 20:
            raise SharingNeedsReview("Choose between one and twenty individual recipient emails")
        recipients = sorted(recipient_email(value) for value in recipients)
        if len(set(recipients)) != len(recipients) or snapshot["owner"] in recipients:
            raise SharingNeedsReview("Recipients must be unique and must not include the owner")
        if journal.get("draft") != draft or journal.get("snapshot_hash") != _hash(snapshot):
            raise SharingNeedsReview("Sharing requires this review's prepared notes-only draft")
        approval = {"draft": draft, "recipients": recipients}
        binding = _hash(approval)
        if "share" in journal and journal["share"].get("approval_hash") != binding:
            raise SharingNeedsReview("This sharing attempt has different recipients or notes")
        if "share" not in journal:
            journal["share"] = {"approval_hash": binding, "recipients": {}, "created_at": time.time()}
            save()
        operations = journal["share"]["recipients"]
        for email in recipients:
            verified = self.verify(snapshot, draft, recipients, guard)
            op = operations.setdefault(email, {"state": "planned"})
            if email in verified["permissions"]:
                op.update(state="verified", permission=verified["permissions"][email], verified_at=time.time(),
                          reconciled=op["state"] in {"dispatched", "acknowledged"})
                save()
                continue
            if op["state"] != "planned":
                raise SharingNeedsReview("An invitation has an uncertain outcome; no repeat invitation was sent")
            op.update(state="dispatched", dispatched_at=time.time())
            save()
            try:
                self._call("share_file", {"url": draft["url"], "user_email": email,
                    "permission": "reader", "anyone_at_company": False}, guard)
            except Exception as error:
                if getattr(error, "outcome_unknown", True) is False:
                    op["state"] = "planned"
                    save()
                    raise
                # A permission grant may have succeeded before the response
                # vanished. Read back once; never repeat the notification.
                verified = self.verify(snapshot, draft, recipients, guard)
                if email not in verified["permissions"]:
                    raise SharingNeedsReview("An invitation has an uncertain outcome; no repeat invitation was sent") from error
                op.update(state="verified", permission=verified["permissions"][email], reconciled=True, verified_at=time.time())
                save()
                continue
            op["state"] = "acknowledged"
            save()
            verified = self.verify(snapshot, draft, recipients, guard)
            if email not in verified["permissions"]:
                raise SharingNeedsReview("Drive has not verified the recipient's access; no repeat invitation was sent")
            op.update(state="verified", permission=verified["permissions"][email], verified_at=time.time())
            save()
        verified = self.verify(snapshot, draft, recipients, guard)
        if set(verified["permissions"]) != set(recipients):
            raise SharingNeedsReview("Drive recipient access differs from the approved list")
        return {"document_id": draft["document_id"], "url": draft["url"], "permission": "reader",
                "recipients": copy.deepcopy(operations), "verified_at": time.time()}
