"""Durable, explicit notes-sharing reviews, independent of private filing."""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from .codex_sharing_transport import recipient_email
from .codex_drive_transport import DriveProtocolError
from .pipeline import Queue, WorkflowError, automatic_calendar_suggestions_since, load_config, safe_member
from .sharing_source import sharing_snapshot


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def recipients(value):
    if not isinstance(value, str) or len(value) > 6000:
        raise WorkflowError("Enter the recipients' complete email addresses")
    try:
        result = sorted({recipient_email(item) for item in re.split(r"[,;\s]+", value.strip()) if item})
    except DriveProtocolError as error:
        raise WorkflowError(str(error)) from error
    if not 1 <= len(result) <= 20:
        raise WorkflowError("Choose between one and twenty individual recipients")
    return result


class SharingReviews:
    def __init__(self, state, config_path, snapshot=None):
        self.state = Path(state).expanduser().resolve()
        self.config_path = Path(config_path).expanduser().resolve()
        self.snapshot = snapshot or (lambda job: sharing_snapshot(self.state, self.config_path, job))
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.state / "sharing.sqlite3"
        if path.is_symlink():
            raise WorkflowError("The sharing review database must be a regular file")
        self.db = sqlite3.connect(path, timeout=5)
        path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS sharing_reviews (job TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, job):
        row = self.db.execute("SELECT record FROM sharing_reviews WHERE job=?", (job,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, record):
        record["updated"] = time.time()
        with self.db:
            self.db.execute("INSERT INTO sharing_reviews VALUES (?,?) ON CONFLICT(job) DO UPDATE SET record=excluded.record",
                            (record["job"], json.dumps(record, ensure_ascii=False, allow_nan=False)))

    @contextmanager
    def locked(self, job):
        # A software rollback takes this lock exclusively; separate sharing
        # jobs may run concurrently but cannot overlap installation changes.
        fd = os.open(self.state / "sharing-work.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as global_lock:
            try:
                fcntl.flock(global_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise WorkflowError("Software recovery is in progress; reopen this review when it finishes") from error
            with self._job_lock(job):
                from .local_deletion import deleted_jobs
                if job in deleted_jobs(self.state):
                    raise WorkflowError("This local meeting was deleted; sharing is stopped")
                yield

    @contextmanager
    def _job_lock(self, job):
        # Hash the opaque queue ID; it never becomes a user-controlled path.
        name = "sharing-" + hashlib.sha256(job.encode()).hexdigest() + ".lock"
        path = self.state / name
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise WorkflowError("This sharing review is already being updated; reload shortly") from error
            from .processing_lease import lease_context
            with lease_context(handle.fileno()):
                yield

    def _current(self, record):
        snapshot = self.snapshot(record["job"])
        if digest(snapshot) != record["source_hash"]:
            raise WorkflowError("The saved meeting notes changed. This sharing approval is no longer valid")
        return snapshot

    def _automatic_policy(self, record):
        if record.get("suggestion_automatic") is not True:
            return
        automatic = record.get("automatic_suggestion", {})
        if (automatic.get("state") not in ("pending", "started")
                or automatic_calendar_suggestions_since(load_config(self.config_path)) != automatic.get("since")):
            automatic["state"] = "cancelled"
            raise WorkflowError("Automatic calendar lookup was turned off or its settings changed. Find attendees manually to retry.")

    def _eligible_automatic_jobs(self, since):
        queue = Queue(self.state)
        try:
            return {row[0] for row in queue.db.execute(
                "SELECT j.id FROM jobs j JOIN tasks t ON j.id=t.job "
                "WHERE j.created>=? AND t.stage='publish' AND t.status='succeeded'", (since,))}
        finally:
            queue.close()

    def request_automatic(self, job, since):
        """Durably request a first lookup for newly completed notes only.

        Dispatch may be retried before a child starts. Once Calendar lookup
        starts, failures require a manual retry and never replay silently.
        """
        if isinstance(since, bool) or not isinstance(since, (int, float)) or not math.isfinite(since) or since <= 0:
            raise WorkflowError("Automatic calendar lookup needs its activation time")
        if automatic_calendar_suggestions_since(load_config(self.config_path)) != since:
            return None
        if job not in self._eligible_automatic_jobs(since):
            return None
        try:
            with self.locked(job):
                record = self.get(job)
                now = time.time()
                if record:
                    automatic = record.get("automatic_suggestion", {})
                    if (automatic.get("state") != "pending" or automatic.get("since") != since
                            or record["status"] != "available" or record.get("approval")
                            or record.get("recipients") or record.get("recipient_draft") is not None):
                        return None
                    if now < automatic.get("next_dispatch_at", 0):
                        return None
                    if automatic.get("dispatch_attempts", 0) >= 3:
                        automatic["state"] = "failed"
                        record.update(suggestion_status="needs_attention", revision=uuid.uuid4().hex,
                                      suggestion_error="The automatic calendar lookup could not start. Retry it from this review.")
                        self.put(record)
                        return None
                    try:
                        self._current(record)
                    except (ValueError, WorkflowError, OSError) as error:
                        automatic["state"] = "failed"
                        record.update(suggestion_status="needs_attention", suggestion_error=str(error)[:1000], revision=uuid.uuid4().hex)
                        self.put(record)
                        return None
                else:
                    snapshot = self.snapshot(job)
                    record = self._new(job, snapshot)
                    record.update(status="available", suggestion_automatic=True,
                                  automatic_suggestion={"state": "pending", "since": since, "dispatch_attempts": 0})
                    automatic = record["automatic_suggestion"]
                automatic["dispatch_attempts"] += 1
                automatic["next_dispatch_at"] = now + (15 if automatic["dispatch_attempts"] == 1 else 60)
                record.update(suggestion_status="searching", suggestion_revision=uuid.uuid4().hex,
                              suggested_events=[], suggestion_error=None, suggestion_warnings=[], revision=uuid.uuid4().hex)
                self.put(record)
                return copy.deepcopy(record)
        except WorkflowError as error:
            if "already being updated" in str(error) or "recovery is in progress" in str(error):
                return None
            raise

    def pending_automatic(self, since):
        if automatic_calendar_suggestions_since(load_config(self.config_path)) != since:
            return False
        eligible = self._eligible_automatic_jobs(since)
        return any(record.get("automatic_suggestion", {}).get("state") == "pending"
                   and record["automatic_suggestion"].get("since") == since
                   and record["job"] in eligible
                   and record.get("status") == "available" and not record.get("approval")
                   and not record.get("recipients") and record.get("recipient_draft") is None
                   for (saved,) in self.db.execute("SELECT record FROM sharing_reviews")
                   for record in [json.loads(saved)])

    def automatic_launch_failed(self, job, revision):
        with self.locked(job):
            record = self.get(job)
            if record and record["revision"] == revision and record.get("automatic_suggestion", {}).get("state") == "pending":
                record["suggestion_error"] = "The calendar lookup could not start. It will retry shortly."
                self.put(record)

    def card(self, job):
        automatic_enabled = load_config(self.config_path).get("sharing", {}).get("automatic_calendar_suggestions") is True
        record = self.get(job)
        if record is None:
            snapshot = self.snapshot(job)
            return {"job": job, "title": snapshot["title"], "preview": snapshot["preview"],
                    "revision": digest(snapshot), "status": "available", "suggestion_status": "idle", "auto_suggestions": automatic_enabled}
        result = {key: record.get(key) for key in ("job", "title", "preview", "revision", "status", "recipients", "error")}
        result["draft_url"] = record.get("draft", {}).get("url")
        result["receipts"] = record.get("receipt", {}).get("recipients", {})
        result["cancellable"] = not record.get("approval")
        result["sharing_started"] = bool(record.get("approval"))
        result["verified_recipients"] = sorted(email for email, value in
            record.get("journal", {}).get("share", {}).get("recipients", {}).items()
            if value.get("state") == "verified")
        automatic_pending = record.get("automatic_suggestion", {}).get("state") == "pending"
        # A lost completion process must not strand the review in Searching.
        # Leave dispatch backoff ample time, then offer a manual retry. An
        # active lookup holds the job lock and cannot be recovered here.
        recovery_at = (record["automatic_suggestion"].get("next_dispatch_at", record["updated"]) + 90
                       if automatic_pending else record["updated"] + 15)
        if record.get("suggestion_status") == "searching" and time.time() > recovery_at:
            try:
                with self.locked(job):
                    record = self.get(job)
                    if record.get("suggestion_status") == "searching" and record["revision"] == result["revision"]:
                        if record.get("automatic_suggestion", {}).get("state") == "pending":
                            record["automatic_suggestion"]["state"] = "failed"
                        record.update(suggestion_status="needs_attention", suggestion_error="The calendar lookup stopped. Find attendees again to retry.",
                                      revision=uuid.uuid4().hex)
                        self.put(record)
                        result["revision"] = record["revision"]
            except WorkflowError:
                pass
        if record["status"] in ("available", "ready") and not record.get("approval"):
            result["auto_suggestions"] = automatic_enabled
            result["suggestion_automatic"] = record.get("suggestion_automatic") is True
            result.update({key: record.get(key) for key in ("suggestion_error", "suggestion_revision", "recipient_draft")})
            result["suggestion_status"] = record.get("suggestion_status", "idle")
            result["suggested_events"] = record.get("suggested_events", [])
            result["suggestion_warnings"] = record.get("suggestion_warnings", [])
            result["suggestion_scope"] = record.get("suggestion_scope", [])
        if record["status"] in ("preparing", "sharing") and time.time() - record["updated"] > 15:
            try:
                with self.locked(job):
                    record = self.get(job)
                    if record["status"] in ("preparing", "sharing"):
                        record.update(status="needs_attention", error="The sharing operation stopped before it finished. Review and retry to check its saved result.",
                                      revision=uuid.uuid4().hex)
                        self.put(record)
                        result.update(status=record["status"], revision=record["revision"], error=record["error"])
            except WorkflowError:
                pass
        if record["status"] not in ("private", "shared"):
            try:
                self._current(record)
            except (ValueError, WorkflowError, OSError) as error:
                result.update(status="needs_attention", error=str(error), retryable=False)
                return result
        result["retryable"] = result["status"] == "needs_attention" and record.get("attempts", 0) < 3
        return result

    def options(self):
        queue = Queue(self.state)
        try:
            jobs = [row[0] for row in queue.db.execute("SELECT job FROM tasks WHERE stage='publish' AND status='succeeded' ORDER BY rowid DESC")]
        finally:
            queue.close()
        jobs.extend(row[0] for row in self.db.execute("SELECT job FROM sharing_reviews") if row[0] not in jobs)
        from .local_deletion import deleted_jobs
        removed = deleted_jobs(self.state)
        cards = []
        for job in jobs:
            if job in removed:
                continue
            try:
                card = self.card(job)
                if card["status"] != "private":
                    cards.append(card)
            except (ValueError, WorkflowError, OSError):
                # Ineligible historical/private-only jobs never become offers.
                continue
        return cards

    def decide(self, action, job, revision, emails=None, suggestion=None):
        if action not in ("sharing_prepare", "sharing_confirm", "sharing_private", "sharing_retry", "sharing_suggest", "sharing_use_suggestions"):
            raise WorkflowError("Unknown sharing action")
        if action != "sharing_prepare" and emails is not None:
            raise WorkflowError("Recipients can only be set when preparing the private copy")
        if action != "sharing_use_suggestions" and suggestion is not None:
            raise WorkflowError("Choose a listed calendar suggestion")
        with self.locked(job):
            record = self.get(job)
            # Keeping an unsent draft private remains safe when its source has
            # changed or disappeared. All externally acting choices revalidate.
            snapshot = None if record and action == "sharing_private" else self.snapshot(job)
            expected = record["revision"] if record else digest(snapshot)
            if revision != expected:
                raise WorkflowError("This review changed; reload before choosing")
            if record and action != "sharing_private":
                self._current(record)
            status = record["status"] if record else "available"
            if action == "sharing_suggest":
                if status not in ("available", "ready") or (record and record.get("approval")):
                    raise WorkflowError("Calendar lookup is available before sharing is approved")
                if record and record.get("suggestion_status") == "searching":
                    raise WorkflowError("The calendar lookup is already running; refresh shortly")
                record = record or self._new(job, snapshot)
                record.update(status=status, suggestion_status="searching", suggestion_revision=uuid.uuid4().hex,
                              suggested_events=[], suggestion_error=None, suggestion_warnings=[], revision=uuid.uuid4().hex)
            elif action == "sharing_use_suggestions":
                if (status not in ("available", "ready") or not record or record.get("approval")
                        or record.get("suggestion_status") != "ready"):
                    raise WorkflowError("Find calendar suggestions before choosing recipients")
                if (not isinstance(suggestion, str) or not re.fullmatch(r"[a-f0-9]{64}", suggestion)):
                    raise WorkflowError("Choose a listed calendar suggestion")
                matches = [event for event in record.get("suggested_events", []) if event.get("selection_token") == suggestion]
                if len(matches) != 1:
                    raise WorkflowError("This calendar suggestion changed; find attendees again")
                event = matches[0]
                chosen = recipients("\n".join(person["email"] for person in event["recipients"]))
                if snapshot["owner"].casefold() in chosen:
                    raise WorkflowError("The document owner already has access")
                record.update(recipient_draft="\n".join(chosen), recipient_source=copy.deepcopy(event), revision=uuid.uuid4().hex)
            elif action == "sharing_private":
                if status not in ("available", "ready", "needs_attention") or (record and record.get("approval")):
                    raise WorkflowError("Sharing may already have started; existing access cannot be revoked here")
                record = record or self._new(job, snapshot)
                record.update(status="private", revision=uuid.uuid4().hex)
            elif action == "sharing_prepare":
                if status not in ("available", "ready") or (record and record.get("approval")):
                    raise WorkflowError("A private sharing copy was already requested")
                chosen = recipients(emails)
                if snapshot["owner"].casefold() in chosen:
                    raise WorkflowError("The document owner already has access; enter the other recipients")
                if status == "ready":
                    record.update(recipients=chosen, revision=uuid.uuid4().hex, recipient_draft=None)
                else:
                    record = record or self._new(job, snapshot)
                    record.update(status="preparing", operation="prepare", recipients=chosen, attempts=1,
                                  recipient_draft=None, revision=uuid.uuid4().hex)
            elif action == "sharing_confirm":
                if status != "ready" or not record.get("draft"):
                    raise WorkflowError("Prepare and review the private notes copy first")
                if record.get("recipient_draft") is not None:
                    raise WorkflowError("Save and review the suggested recipients before sharing")
                record.update(status="sharing", operation="share", attempts=1,
                              approval={"source_hash": record["source_hash"], "draft_hash": digest(record["draft"]),
                                        "recipients": record["recipients"], "approved_at": time.time()})
                record["revision"] = uuid.uuid4().hex
            else:
                if status != "needs_attention" or record.get("attempts", 0) >= 3:
                    raise WorkflowError("This operation needs a new manual review before another attempt")
                record.update(status="preparing" if record["operation"] == "prepare" else "sharing",
                              attempts=record["attempts"] + 1, revision=uuid.uuid4().hex, error=None)
            if action != "sharing_suggest" and record.get("suggestion_status") == "searching":
                record["suggestion_status"] = "idle"
            # Any manual choice owns this review from now on. Completion
            # reconciliation must never replace a user's chosen recipients.
            if record.get("automatic_suggestion"):
                record["automatic_suggestion"]["state"] = "cancelled"
            if action == "sharing_suggest":
                record["suggestion_automatic"] = False
            self.put(record)
            return copy.deepcopy(record)

    @staticmethod
    def _new(job, snapshot):
        return {"job": job, "title": snapshot["title"], "preview": snapshot["preview"],
                "source_hash": digest(snapshot), "revision": uuid.uuid4().hex, "journal": {}, "recipients": []}

    def launch_failed(self, job, revision):
        with self.locked(job):
            record = self.get(job)
            if record and record["revision"] == revision and record.get("suggestion_status") == "searching":
                record.update(suggestion_status="needs_attention", revision=uuid.uuid4().hex,
                              suggestion_error="The calendar lookup could not start. Find attendees again to retry.")
                self.put(record)
            elif record and record["revision"] == revision and record["status"] in ("preparing", "sharing"):
                record.update(status="needs_attention", revision=uuid.uuid4().hex,
                              error="The sharing worker could not start. Nothing new was sent; retry when ready.")
                self.put(record)

    def _suggestion_context(self, job, snapshot):
        # Read only the bounded metadata and validated names already bound to
        # this notes review. Calendar receives time bounds, never the transcript.
        recording = self.state / "recordings" / job
        metadata_path = safe_member(recording, "meta.json")
        notes_path = safe_member(self.state / "results" / job / "notes", "notes.json")
        def read_bound(path, key, limit):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                    raise WorkflowError("Calendar context must use bounded regular meeting files")
                data = source.read(limit + 1)
            if len(data) > limit or hashlib.sha256(data).hexdigest() != snapshot["binding"][key]:
                raise WorkflowError("The saved meeting changed before calendar lookup")
            return json.loads(data)
        metadata = read_bound(metadata_path, "metadata_sha256", 100_000)
        notes = read_bound(notes_path, "notes_sha256", 20_000_000)
        return {"job": job, "source_hash": digest(snapshot), "title": notes["meeting_title"]["text"],
                "started": metadata["started"], "ended": metadata["ended"], "owner": snapshot["owner"],
                "timezone": metadata.get("recording_timezone", "UTC"),
                "participants": [person["name"] for person in notes["meeting_title"]["participants"]]}

    def run_suggestions(self, job, revision, invoke=None, suggest=None):
        from .local_deletion import deleted_jobs
        with self.locked(job):
            record = self.get(job)
            if (not record or record["revision"] != revision or record.get("suggestion_status") != "searching"
                    or record["status"] not in ("available", "ready") or record.get("approval")):
                raise WorkflowError("This calendar lookup is no longer current")
            try:
                self._automatic_policy(record)
                snapshot = self._current(record)
                context = self._suggestion_context(job, snapshot)
                if record.get("automatic_suggestion", {}).get("state") == "pending":
                    record["automatic_suggestion"].update(state="started", started_at=time.time())
                    self.put(record)
                deadline = time.monotonic() + 90
                transport = None
                if invoke is None:
                    from .codex_calendar_transport import CodexCalendarTransport
                    settings = load_config(self.config_path).get("publish", {})
                    transport = CodexCalendarTransport(settings.get("codex", "/Applications/Codex.app/Contents/Resources/codex"),
                        timeout=min(90, max(1, int(settings.get("timeout_seconds", 180)))))
                    transport.cancelled = lambda: job in deleted_jobs(self.state)
                    invoke = transport.invoke
                if suggest is None:
                    from .recipient_suggestions import suggest_recipients
                    suggest = suggest_recipients
                def guarded(tool, arguments):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WorkflowError("The calendar lookup reached its time limit; review partial results or retry")
                    if transport is not None:
                        transport.timeout = min(transport.timeout, remaining)
                    live = self.get(job)
                    if (not live or live["revision"] != revision or live.get("suggestion_status") != "searching"
                            or live.get("approval")):
                        raise WorkflowError("This calendar lookup is no longer current")
                    self._current(record)
                    self._automatic_policy(record)
                    if tool not in {"list_calendars", "search_events", "read_event"}:
                        raise WorkflowError("Recipient lookup can only read calendar events")
                    return invoke(tool, arguments)
                found = suggest(context, guarded)
                self._automatic_policy(record)
                self._current(record)
                if found.get("source_hash") != record["source_hash"] or found.get("job") != job:
                    raise WorkflowError("Calendar suggestions belong to a different meeting review")
                events = copy.deepcopy(found["events"])
                for event in events:
                    event["selection_token"] = event["suggestion_id"]
                    event["display_timezone"] = context.get("timezone", "UTC")
                unavailable = found.get("status") == "unavailable"
                record.update(suggestion_status="needs_attention" if unavailable else "ready", suggested_events=events,
                              suggestion_warnings=found.get("warnings", []), suggestion_error=(
                                  "Calendar access or event reads were unavailable. Try again or enter addresses manually." if unavailable else None),
                              suggestion_scope=found.get("calendars", []), suggestion_checked_at=time.time())
            except Exception as error:
                record.update(suggestion_status="needs_attention", suggestion_error=str(error)[:1000], suggested_events=[])
            record["revision"] = uuid.uuid4().hex
            self.put(record)
            return copy.deepcopy(record)

    def run(self, job, revision, adapter=None):
        from .local_deletion import deleted_jobs
        with self.locked(job):
            record = self.get(job)
            if not record or record["revision"] != revision or record["status"] not in ("preparing", "sharing"):
                raise WorkflowError("This sharing operation is no longer current")
            try:
                snapshot = self._current(record)
                def guard():
                    live = self.get(job)
                    if (not live or live["revision"] != revision or live["status"] != record["status"]
                            or live.get("recipients") != record["recipients"] or live.get("approval") != record.get("approval")):
                        raise WorkflowError("This exact sharing approval is no longer current")
                    self._current(record)
                    if record["operation"] == "share":
                        approval = record.get("approval", {})
                        if (approval.get("source_hash") != record["source_hash"]
                                or approval.get("draft_hash") != digest(record.get("draft"))
                                or approval.get("recipients") != record["recipients"]):
                            raise WorkflowError("Review the exact notes copy and recipients before sharing")
                guard()
                if adapter is None:
                    from .codex_sharing_transport import CodexSharingTransport
                    from .sharing_drive import SharingDrive
                    settings = load_config(self.config_path).get("publish", {})
                    transport = CodexSharingTransport(settings.get("codex", "/Applications/Codex.app/Contents/Resources/codex"),
                        model=settings.get("model"), timeout=min(7200, max(1, int(settings.get("timeout_seconds", 180)))))
                    transport.cancelled = lambda: job in deleted_jobs(self.state)
                    adapter = SharingDrive(transport.invoke)
                save = lambda: self.put(record)
                if record["operation"] == "prepare":
                    record["draft"] = adapter.prepare(snapshot, record["journal"], save, guard)
                    record.update(status="ready", error=None)
                else:
                    record["receipt"] = adapter.share(snapshot, record["draft"], record["recipients"], record["journal"], save, guard)
                    record.update(status="shared", error=None)
            except Exception as error:
                record.update(status="needs_attention", error=str(error)[:1000])
            record["revision"] = uuid.uuid4().hex
            self.put(record)
            return copy.deepcopy(record)
