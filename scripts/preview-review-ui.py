#!/usr/bin/env python3
"""Preview meeting review with disposable, fictional data.

From the checkout:
    python3 scripts/preview-review-ui.py
    python3 scripts/preview-review-ui.py --open

The default prints a local URL without opening a browser. The operating system
chooses a free loopback port. Stop with Ctrl+C; all demo files are then removed.
The preview stops after 30 minutes unless --timeout is changed.

Folder choices, retries, calendar suggestions, and sharing are simulated. This
script does not load user settings, start a worker, invoke Codex, contact Google,
or send invitations. Restart it to reset the samples. Deleting a running sample
simulates stopping processing. The upload-failure sample also has saved resumable
work, so the preview can check that only its retry action appears. Retrying it
simulates a completed upload; the other failed sample creates a folder review.
"""

import argparse
import copy
import json
from pathlib import Path
import re
import signal
import time
import sys
import tempfile
import threading
import uuid
import webbrowser


# Import only the loopback UI and its local routing store. Prevent Python from
# leaving bytecode behind in the checkout during this disposable preview.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/meeting-workflow"))
from quill_workflow.review_server import create_server
from quill_workflow.routing import RoutingReviews


COMPANIES = [
    {
        "id": "northstar",
        "name": "Northstar Studio",
        "drive_folder_id": "demo_northstar_folder",
        "description": "Fictional design studio used in the local UI preview.",
    },
    {
        "id": "harbor",
        "name": "Harbor Supply",
        "drive_folder_id": "demo_harbor_folder",
        "description": "Fictional supply company used in the local UI preview.",
    },
]


def revision():
    return uuid.uuid4().hex


def sample_notes(title, summary, actions):
    return (
        f"# {title}\n\n"
        "October 20, 2026 · Alex Morgan and Taylor\n\n"
        f"## Summary\n{summary}\n\n"
        "## Decisions and next steps\n"
        + "\n".join(f"- {action}" for action in actions)
        + "\n\nThese are fictional notes for the local Quill demo.\n"
    )


class PreviewData:
    """Keep simulated callbacks in memory and routing reviews in a temp folder."""

    def __init__(self, state):
        self.state = Path(state)
        self.notes = {}
        self.deletion_confirmations = {}
        self.removed = set()
        self.removal_deadlines = {}
        self.resumable = {}
        self.sharing = {}
        self.processing = {
            "demo-running": {
                "job": "demo-running",
                "revision": revision(),
                "title": "Northstar research review",
                "state": "running",
                "stage": "notes",
                "detail": "Drafting the summary and next steps from the recording.",
                "retryable": False,
            },
            "demo-retry": {
                "job": "demo-retry",
                "revision": revision(),
                "title": "Harbor supplier check-in",
                "state": "needs_attention",
                "stage": "notes",
                "detail": "Notes processing stopped. The recording is saved and ready to retry.",
                "retryable": True,
            },
        }
        self._add_folder_review(
            "demo-northstar-launch",
            "Northstar launch review",
            "northstar",
            "The meeting covers Northstar's launch page and the next design review.",
            sample_notes(
                "Northstar launch review",
                "Alex Morgan and Taylor reviewed the launch page and agreed on the remaining edits.",
                [
                    "Alex Morgan will update the product images before the next review.",
                    "Taylor will check the mobile page and collect the final copy edits.",
                    "Keep the launch date open until both reviews are complete.",
                ],
            ),
        )
        self._add_folder_review(
            "demo-harbor-operations",
            "Harbor operations check-in",
            "harbor",
            "The discussion mentions Harbor's orders and a shared project with Northstar.",
            sample_notes(
                "Harbor operations check-in",
                "The team checked open orders and discussed which updates belong in the shared project.",
                [
                    "Taylor will confirm the delivery window with the supplier.",
                    "Alex Morgan will separate the order updates from the design work.",
                    "Review the company folder before filing these notes.",
                ],
            ),
            ambiguous=True,
        )
        conflict = "demo-upload-conflict"
        record = self._add_folder_review(
            conflict,
            "Northstar launch follow-up",
            "northstar",
            "The team reviewed Northstar's launch edits and final checks.",
            sample_notes(
                "Northstar launch follow-up",
                "Alex Morgan and Taylor checked the updated launch page and agreed on the last review tasks.",
                [
                    "Taylor will check the final layout on a phone.",
                    "Alex Morgan will confirm the launch date after the review.",
                ],
            ),
        )
        reviews = RoutingReviews(self.state)
        try:
            reviews.approve(conflict, "northstar", record["revision"])
        finally:
            reviews.close()
        self.resumable[conflict] = ["archive", "publish"]
        self.processing[conflict] = {
            "job": conflict,
            "revision": revision(),
            "title": "Northstar launch follow-up",
            "state": "needs_attention",
            "stage": "archive",
            "detail": "The upload stopped. Retry checks the saved attempt before continuing.",
            "retryable": True,
        }
        self._add_sharing(
            "demo-sharing-available",
            "Northstar design handoff",
            sample_notes(
                "Northstar design handoff",
                "Alex Morgan walked Taylor through the approved layouts and the remaining review tasks.",
                [
                    "Taylor will review the layouts on a phone and a laptop.",
                    "Alex Morgan will collect feedback in one document.",
                ],
            ),
            "available",
        )
        self._add_sharing(
            "demo-sharing-ready",
            "Harbor account planning",
            sample_notes(
                "Harbor account planning",
                "Alex Morgan and Taylor agreed on the topics for the next account review.",
                [
                    "Alex Morgan will prepare the list of open orders.",
                    "Taylor will add the questions that need a supplier response.",
                ],
            ),
            "ready",
            ["alex.morgan@example.com", "taylor@example.com"],
        )
        self._add_sharing(
            "demo-sharing-complete",
            "Northstar weekly check-in",
            sample_notes(
                "Northstar weekly check-in",
                "The team reviewed the week's completed work and agreed on the next two tasks.",
                [
                    "Taylor will send the remaining feedback to Alex Morgan.",
                    "Alex Morgan will prepare the next review agenda.",
                ],
            ),
            "shared",
            ["taylor@example.com"],
        )
        self._set_suggestions(self.sharing["demo-sharing-available"])

    def _add_folder_review(self, job, title, company, reason, notes, ambiguous=False):
        self.notes[job] = (title, notes)
        destination = self.state / "results" / job / "notes" / "notes.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(notes, encoding="utf-8")
        transcript = json.dumps({"segments": [{"text": reason}]}).encode("utf-8")
        reviews = RoutingReviews(self.state)
        try:
            return reviews.propose(
                job,
                transcript,
                COMPANIES,
                {
                    "schema_version": 1,
                    "company_id": company,
                    "confidence": "medium" if ambiguous else "high",
                    "reason": reason,
                    "evidence": [0],
                    "ambiguous": ambiguous,
                },
            )
        finally:
            reviews.close()

    def _add_sharing(self, job, title, notes, status, recipients=None):
        self.sharing[job] = {
            "job": job,
            "revision": revision(),
            "title": title,
            "preview": notes,
            "status": status,
            "recipients": list(recipients or []),
            # The UI retains its normal link validation, but preview mode
            # renders a demo-copy label without an outbound anchor.
            "draft_url": "https://docs.google.com/document/d/quill-preview-example/edit",
            "recipient_draft": None,
            "cancellable": status != "shared",
            "sharing_started": status == "shared",
            "retryable": False,
            "suggestion_status": "idle",
        }

    def _set_suggestions(self, item):
        item.update(
            suggestion_status="ready",
            suggestion_automatic=True,
            suggested_events=[
                {
                    "title": item["title"],
                    "calendar_name": "Demo calendar",
                    "start": "2026-10-20T10:00:00+08:00",
                    "display_timezone": "Asia/Singapore",
                    "match_reason": "The invitation title and recording time match this sample meeting.",
                    "selection_token": revision(),
                    "recipients": [
                        {
                            "name": "Alex Morgan",
                            "email": "alex.morgan@example.com",
                            "response_status": "accepted",
                        },
                        {
                            "name": "Taylor",
                            "email": "taylor@example.com",
                            "response_status": "accepted",
                        },
                    ],
                }
            ],
        )

    def deletion_options(self):
        for job, (deadline, item) in list(self.removal_deadlines.items()):
            if time.monotonic() >= deadline:
                self._remove_demo(job)
                del self.removal_deadlines[job]
        jobs = set(self.notes) | set(self.sharing) | set(self.processing)
        result = [{"job": job, "title": (self.notes[job][0] if job in self.notes else
                  (self.sharing.get(job) or self.processing[job])["title"]),
                 "revision": self.sharing.get(job, self.processing.get(job, {})).get("revision", job),
                 "available": True}
                for job in sorted(jobs - self.removed)]
        for job, (_deadline, item) in self.removal_deadlines.items():
            result = [entry for entry in result if entry["job"] != job]
            result.append(dict(item, cancelling=True, detail="Stopping processing… This preview will finish automatically."))
        return result

    def on_delete(self, action, job, expected_revision, confirmation=None):
        item = next((item for item in self.deletion_options() if item["job"] == job), None)
        if not item or not item["available"] or item["revision"] != expected_revision:
            raise ValueError("This demo meeting changed or is processing. Reload its review.")
        if action == "delete_prepare":
            token = revision()
            self.deletion_confirmations[job] = token
            return dict(item, confirmation=token, folder="Fictional preview folder / " + item["title"])
        if action != "delete_confirm" or self.deletion_confirmations.get(job) != confirmation:
            raise ValueError("Review the simulated removal before confirming.")
        if self.processing.get(job, {}).get("state") == "running":
            self.removal_deadlines[job] = (time.monotonic() + 2, item)
            self.processing.pop(job, None)
            return {"status": "cancelling"}
        self._remove_demo(job)
        return {"status": "trashed"}

    def _remove_demo(self, job):
        self.removed.add(job)
        self.processing.pop(job, None)
        self.sharing.pop(job, None)
        self.resumable.pop(job, None)
        reviews = RoutingReviews(self.state)
        try:
            # This store is disposable fixture data; no user state is loaded.
            with reviews.db:
                reviews.db.execute("DELETE FROM routing_reviews WHERE job=?", (job,))
        finally:
            reviews.close()

    def processing_options(self):
        return copy.deepcopy(list(self.processing.values()))

    def sharing_options(self):
        return copy.deepcopy(list(self.sharing.values()))

    def resume_options(self, record):
        return list(self.resumable.get(record["job"], []))

    def on_resume(self, record, stages):
        job = record["job"]
        if job in self.processing or stages != self.resumable.get(job):
            raise ValueError("This demo meeting needs its processing issue resolved first.")
        title, notes = self.notes[job]
        self._add_sharing(job, title, notes, "available")
        self.resumable.pop(job, None)

    def on_decision(self, record):
        if record["status"] != "approved" or record["job"] in self.sharing:
            return
        title, notes = self.notes[record["job"]]
        self._add_sharing(record["job"], title, notes, "available")

    def on_retry(self, job, expected_revision):
        item = self.processing.get(job)
        if not item or item["revision"] != expected_revision or not item["retryable"]:
            raise ValueError("This demo meeting has changed. Reload the review.")
        if job in self.resumable:
            # This callback changes only fictional in-memory state. No upload
            # is attempted and no held production work is released.
            title, notes = self.notes[job]
            self._add_sharing(job, title, notes, "available")
            self.resumable.pop(job, None)
            del self.processing[job]
            return
        self._add_folder_review(
            job,
            item["title"],
            "harbor",
            "The supplier discussion concerns Harbor's delivery schedule.",
            sample_notes(
                item["title"],
                "Alex Morgan and Taylor checked the delivery schedule and the remaining supplier questions.",
                [
                    "Taylor will confirm the delivery date.",
                    "Alex Morgan will add the confirmed date to the order notes.",
                ],
            ),
        )
        del self.processing[job]

    @staticmethod
    def _recipients(value):
        recipients = [email.strip() for email in re.split(r"[,\n]", value or "") if email.strip()]
        if not recipients or any(
            not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@example\.com", email)
            for email in recipients
        ):
            raise ValueError("Use example.com addresses in this demo, such as taylor@example.com.")
        return list(dict.fromkeys(recipients))

    def on_sharing(self, action, job, expected_revision, emails=None, suggestion=None):
        item = self.sharing.get(job)
        if not item or item["revision"] != expected_revision:
            raise ValueError("This demo review has changed. Reload the review.")
        if item["status"] in ("private", "shared"):
            raise ValueError("This demo review is complete. Restart the preview to reset it.")
        if action == "sharing_prepare":
            item.update(status="ready", recipients=self._recipients(emails), recipient_draft=None)
        elif action == "sharing_confirm":
            if item["status"] != "ready" or not item["recipients"] or item["recipient_draft"] is not None:
                raise ValueError("Review and save the demo recipients before confirming.")
            item.update(status="shared", sharing_started=True, cancellable=False)
        elif action == "sharing_private":
            item.update(status="private", cancellable=False)
        elif action == "sharing_suggest":
            self._set_suggestions(item)
        elif action == "sharing_use_suggestions":
            event = next(
                (event for event in item.get("suggested_events", [])
                 if event["selection_token"] == suggestion),
                None,
            )
            if event is None:
                raise ValueError("These demo suggestions have changed. Reload the review.")
            item["recipient_draft"] = "\n".join(person["email"] for person in event["recipients"])
        elif action == "sharing_retry" and item["status"] == "needs_attention":
            item.update(status="ready", retryable=False)
        else:
            raise ValueError("This action is unavailable for the current demo review.")
        item["revision"] = revision()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run the Quill review UI with temporary, fictional meeting data.",
        epilog=(
            "Open the printed local URL. All actions are simulated; use only example.com "
            "addresses. Ctrl+C removes the demo files. Restart to reset the samples."
        ),
    )
    browser = parser.add_mutually_exclusive_group()
    browser.add_argument("--open", action="store_true", help="open the preview in your default browser")
    browser.add_argument("--no-open", action="store_true", help="only print the local URL (the default)")
    parser.add_argument(
        "--timeout", type=int, default=1800, metavar="SECONDS",
        help="stop and remove the demo after this many seconds (default: 1800)",
    )
    args = parser.parse_args()
    if not 1 <= args.timeout <= 86400:
        parser.error("--timeout must be between 1 and 86400 seconds")
    return args


def main():
    args = parse_args()
    stopped = threading.Event()
    previous_handlers = {}

    def stop_preview(signum, frame):
        stopped.set()

    with tempfile.TemporaryDirectory(prefix="quill-review-demo-") as temporary:
        demo = PreviewData(Path(temporary) / "state")
        server = create_server(
            demo.state,
            on_decision=demo.on_decision,
            resume_options=demo.resume_options,
            on_resume=demo.on_resume,
            processing_options=demo.processing_options,
            on_retry=demo.on_retry,
            sharing_options=demo.sharing_options,
            on_sharing=demo.on_sharing,
            deletion_options=demo.deletion_options,
            on_delete=demo.on_delete,
        )
        server.preview_mode = True
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.1}, name="quill-review-demo"
        )
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, stop_preview)
            thread.start()
            print("Quill UI demo · fictional meetings · all actions are simulated", flush=True)
            print(server.review_url, flush=True)
            print("Use example.com addresses. Ctrl+C closes the demo and removes its files.", flush=True)
            if args.open:
                webbrowser.open(server.review_url)
            stopped.wait(args.timeout)
        finally:
            if thread.is_alive():
                server.shutdown()
                thread.join(timeout=6)
            server.server_close()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    print("Demo closed. Temporary files removed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
