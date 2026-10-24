#!/usr/bin/env python3
"""Quill on_stop entry point and durable worker. No global installation."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from quill_workflow.pipeline import (ConnectorRunner, Queue, WorkflowError, configured,
                                     automatic_calendar_suggestions_since, load_config, notes_markdown_matches,
                                     read_json, route_settings, safe_member)
from quill_workflow.routing import RoutingReviews, preserves_company_map
from quill_workflow.recording_safety import RecordingSafetyHold
from quill_workflow.worker_supervision import (health, lock_held, reset_recovery, supervise, worker_event)

WORKFLOW_STAGES = ("archive", "notes", "route", "publish")


@contextmanager
def worker_termination():
    """Unwind an interrupted worker's subprocess cleanup and file locks."""
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, _frame):
        # A second request must not interrupt transport cleanup. SystemExit
        # bypasses the pipeline's Exception handlers, leaving an active task
        # recoverable instead of recording a retry and continuing this worker.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def start_worker(config, state, supervised=False):
    if not supervised and (lock_held(state, "supervisor.lock") or health(state)["state"] == "needs_attention"):
        return None
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                      "--config", str(Path(config).expanduser().resolve()),
                      "--state", str(state), "work"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def processing_meetings(config_path, state):
    from quill_workflow.queue_status import meetings
    queue = Queue(state)
    try:
        # Listing does not release work or read/transcribe recordings. Every
        # submitted Retry is revalidated against live settings and sources.
        current = load_config(config_path)
        result = meetings(queue)
        for item in result:
            tasks = queue.tasks(item["job"])
            selected = [task for task in tasks if task["status"] in ("failed", "retry")]
            item["retryable"] = (item["retryable"] and all(configured(current.get(task["stage"], {}),
                                                                        task["stage"]) for task in selected))
        result = [item for item in result if item["state"] != "idle"]
        worker = health(state)
        if worker["state"] == "needs_attention":
            result.insert(0, {"title": "Meeting processing stopped", "detail": worker["error"] or
                             "Automatic recovery needs attention", "retryable": False})
        return result
    finally:
        queue.close()


def retry_meeting(job, revision, config_path, state):
    queue = Queue(state)
    try:
        stages = queue.retry_failed(job, revision, load_config(config_path))
        reset_recovery(queue.state)
        queue.write_status()
        start_worker(config_path, queue.state)
        return stages
    finally:
        queue.close()


def deletion_options(config_path, state):
    from quill_workflow.local_deletion import LocalMeetingDeletion
    deletion = LocalMeetingDeletion(state, config_path)
    try:
        return deletion.options()
    finally:
        deletion.close()


def deletion_decision(action, job, revision, config_path, state, confirmation=None):
    from quill_workflow.local_deletion import LocalMeetingDeletion
    deletion = LocalMeetingDeletion(state, config_path)
    try:
        if action == "delete_prepare":
            return deletion.prepare(job, revision)
        if action == "delete_confirm":
            result = deletion.confirm(job, revision, confirmation)
            if result["status"] == "cancelling":
                start_deletion_worker(config_path, state)
            return result
        raise WorkflowError("Unknown local deletion action")
    finally:
        deletion.close()


def start_deletion_worker(config_path, state):
    # Independent of the pipeline's global lock: an unrelated meeting must not
    # delay cancelling and discarding this one.
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--config",
        str(Path(config_path).expanduser().resolve()), "--state", str(state), "delete-work"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


def reconcile_deletions(config_path, state):
    from quill_workflow.local_deletion import LocalMeetingDeletion, pending_deletions
    deletion = LocalMeetingDeletion(state, config_path)
    try:
        deletion.reconcile()
        return pending_deletions(state)
    finally:
        deletion.close()


def sharing_options(config_path, state):
    from quill_workflow.sharing import SharingReviews
    reviews = SharingReviews(state, config_path)
    try:
        return reviews.options()
    finally:
        reviews.close()


def sharing_decision(action, job, revision, emails, config_path, state, suggestion=None):
    from quill_workflow.sharing import SharingReviews
    reviews = SharingReviews(state, config_path)
    try:
        record = reviews.decide(action, job, revision, emails, suggestion)
        if action == "sharing_suggest" or record["status"] in ("preparing", "sharing"):
            command = "suggestion-work" if action == "sharing_suggest" else "sharing-work"
            try:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                    "--config", str(Path(config_path).expanduser().resolve()), "--state", str(state),
                    command, job, "--revision", record["revision"]],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                reviews.launch_failed(job, record["revision"])
                raise WorkflowError("The review worker could not start; reload and retry")
    finally:
        reviews.close()


def auto_suggest(job, config_path, state):
    """Request one read-only calendar lookup for an eligible new meeting.

    The review store owns the idempotent claim. Neither this dispatch nor a
    startup retry can approve recipients, prepare a shared copy, or share it.
    """
    from quill_workflow.sharing import SharingReviews
    since = automatic_calendar_suggestions_since(load_config(config_path))
    if since is None:
        return False
    reviews = SharingReviews(state, config_path)
    try:
        record = reviews.request_automatic(job, since)
        if record is None:
            return False
        if (not isinstance(record, dict) or record.get("job") != job
                or record.get("suggestion_status") != "searching" or record.get("approval")):
            raise WorkflowError("Automatic calendar lookup did not return an unapproved review")
        try:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                "--config", str(Path(config_path).expanduser().resolve()), "--state", str(state),
                "suggestion-work", job, "--revision", record["revision"]],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            # Preserve the durable, bounded pending dispatch. The review store
            # applies its backoff and attempt limit; a started calendar lookup
            # can never be silently repeated by this recovery path.
            reviews.automatic_launch_failed(job, record["revision"])
            return False
        return True
    finally:
        reviews.close()


def completion_autosuggest(stage, job, receipt, config_path, state):
    if stage != "publish":
        return False
    return auto_suggest(job, config_path, state)


def reconcile_automatic_suggestions(queue, config_path):
    """Recover a commit-before-dispatch interruption without historical work."""
    since = automatic_calendar_suggestions_since(load_config(config_path))
    if since is None:
        return 0
    jobs = [row[0] for row in queue.db.execute(
        "SELECT j.id FROM jobs j JOIN tasks t ON t.job=j.id "
        "WHERE j.created>=? AND t.stage='publish' AND t.status='succeeded' ORDER BY j.created,j.id", (since,))]
    dispatched = 0
    for job in jobs:
        try:
            dispatched += int(auto_suggest(job, config_path, queue.state))
        except Exception:
            # One changed or unavailable source cannot prevent another newly
            # filed meeting from receiving its independent review. The store
            # preserves existing requests and decisions on subsequent scans.
            continue
    return dispatched


def pending_automatic_suggestions(config_path, state):
    """Keep the worker awake only for the store's bounded dispatch recovery."""
    from quill_workflow.sharing import SharingReviews
    since = automatic_calendar_suggestions_since(load_config(config_path))
    if since is None:
        return False
    reviews = SharingReviews(state, config_path)
    try:
        return reviews.pending_automatic(since) is True
    finally:
        reviews.close()


class _SupervisedCompletionQueue:
    """Let the existing supervisor own recovery for pending lookup dispatches."""
    def __init__(self, queue, config_path):
        self.queue, self.config_path = queue, config_path

    def __getattr__(self, name):
        return getattr(self.queue, name)

    def has_work(self):
        from quill_workflow.local_deletion import pending_deletions
        return (self.queue.has_work() or pending_automatic_suggestions(self.config_path, self.queue.state)
                or pending_deletions(self.queue.state))


def acquire_worker_lock(lock, state):
    """Keep one successor waiting so a completion cannot lose its wake-up."""
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        pass
    # A worker may have observed an empty queue just before a new completion
    # was committed. One successor must acquire the lock after it exits and
    # inspect the queue again. Further wake-ups are covered by that successor.
    with (Path(state) / "worker-wakeup.lock").open("a") as successor:
        try:
            fcntl.flock(successor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(lock, fcntl.LOCK_EX)
    return True


def enabled_stages(config_path):
    current = load_config(config_path)
    return {stage: current.get(stage, {}).get("enabled") is True for stage in WORKFLOW_STAGES}


def _resumable_stages(queue, record, current):
    """Validate one exact approved review before offering or releasing work."""
    queue.require_processing(record["job"])
    if record.get("status") != "approved" or not configured(current.get("route", {}), "route"):
        return []
    reviews = RoutingReviews(queue.state)
    try:
        saved = reviews.get(record["job"])
    finally:
        reviews.close()
    if not saved or saved["status"] != "approved" or saved["revision"] != record["revision"]:
        raise WorkflowError("This meeting review changed; reopen the current review")
    tasks = {task["stage"]: task for task in queue.tasks(record["job"])}
    if "route" not in tasks or tasks.get("notes", {}).get("status") != "succeeded":
        return []
    frozen = route_settings(json.loads(tasks["route"]["settings"]))
    if not preserves_company_map(route_settings(current["route"])["companies"], frozen["companies"]):
        raise WorkflowError("Company folders changed; this meeting needs a new review")
    result = queue.state / "results" / record["job"]
    transcript = result / "input/transcript.json"
    if not transcript.exists():
        transcript = queue.state / "recordings" / record["job"] / "transcript.json"
    runner = ConnectorRunner(queue.state, Path(__file__).parent / "skills/call-summary/SKILL.md")
    route, source = runner.approved_route(record["job"], transcript, frozen)
    if route != saved["approved_route"]:
        raise WorkflowError("The approved company changed; reopen this meeting review")
    receipt = read_json(result / "notes-receipt.json")
    if receipt.get("input_sha256") != hashlib.sha256(source).hexdigest():
        raise WorkflowError("Notes use a different transcript; an explicit revision is required")
    notes = result / "notes"
    provenance = read_json(notes / "provenance.json")
    if any(provenance.get(key) != receipt.get(key) for key in ("input_sha256", "settings_sha256")):
        raise WorkflowError("Saved notes provenance changed; an explicit revision is required")
    if safe_member(notes, "source-transcript.json").read_bytes() != source:
        raise WorkflowError("Saved notes use a different source transcript")
    markdown = safe_member(notes, "notes.md")
    if (markdown.stat().st_size > 20_000_000
            or not notes_markdown_matches(markdown.read_text(encoding="utf-8"),
                                          read_json(notes / "notes.json"), json.loads(source))):
        raise WorkflowError("Saved notes and their source references need review before resuming")
    return [stage for stage in ("route", "archive", "publish")
            if tasks.get(stage, {}).get("status") in ("held", "failed", "pending", "retry")
            and configured(current.get(stage, {}), stage)]


def resumable_stages(record, config_path, state):
    queue = Queue(state)
    try:
        return _resumable_stages(queue, record, load_config(config_path))
    finally:
        queue.close()


def resume_waiting_work(record, config_path, state, requested_stages=None):
    """Explicitly release only enabled held stages for this approved meeting."""
    queue = Queue(state)
    try:
        current = load_config(config_path)
        stages = _resumable_stages(queue, record, current)
        if requested_stages is not None:
            if (not isinstance(requested_stages, list) or not requested_stages
                    or len(set(requested_stages)) != len(requested_stages)
                    or any(stage not in ("route", "archive", "publish") for stage in requested_stages)):
                raise WorkflowError("Choose the waiting stages displayed in this meeting review")
            # Settings may narrow a submitted action, but turning on another
            # stage after the page loaded cannot expand the user's release.
            stages = [stage for stage in stages if stage in requested_stages]
        for stage in stages:
            task = next(item for item in queue.tasks(record["job"]) if item["stage"] == stage)
            if task["status"] in ("held", "failed"):
                queue.release(record["job"], stage, current)
        queue.write_status()
        if stages:
            # Retry a failed launch even when release already committed. Pending
            # and retry tasks keep their saved settings, attempts and backoff.
            start_worker(config_path, queue.state)
        return stages
    finally:
        queue.close()


def resume_reviewed_job(record, config_path, state):
    """A folder choice resumes only its held route, never older held uploads."""
    queue = Queue(state)
    try:
        current = load_config(config_path)
        task = next((item for item in queue.tasks(record["job"])
                     if item["stage"] == "route"), None)
        if (record["status"] == "approved" and task is not None
                and current.get("route", {}).get("enabled") is True):
            if task["status"] in ("held", "failed"):
                queue.release(record["job"], "route", current)
            # Popen can fail after release commits. A repeated request must
            # retry the launch for pending work; the worker lock deduplicates.
            # An existing worker reconciles approval racing its held update.
            if task["status"] != "succeeded":
                start_worker(config_path, queue.state)
    finally:
        queue.write_status()
        queue.close()


def reconcile_approved_reviews(queue, config):
    """Repair the decision-before-hold race without releasing unrelated holds."""
    if config.get("route", {}).get("enabled") is not True or not (queue.state / "reviews.sqlite3").exists():
        return 0
    reviews = RoutingReviews(queue.state)
    try:
        approved = {record["job"] for record in reviews.list("approved")}
    finally:
        reviews.close()
    resumed = 0
    for task in queue.tasks():
        if (task["job"] in approved and task["stage"] == "route" and task["status"] == "held"
                and task["error"] == "Choose this meeting's company in the routing review"):
            try:
                queue.release(task["job"], "route", config)
                resumed += 1
            except (WorkflowError, RecordingSafetyHold):
                # A changed map or disabled connector still needs intervention.
                continue
    if resumed:
        queue.write_status()
    return resumed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path.home() / ".config/quill/workflow.json"))
    parser.add_argument("--state", default=str(Path.home() / ".local/state/quill/workflow"))
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("enqueue", "complete"):
        child = sub.add_parser(name)
        child.add_argument("session")
    worker = sub.add_parser("work")
    worker.add_argument("--once", action="store_true")
    supervisor = sub.add_parser("supervise", help="Recover processing while Quill is running")
    supervisor.add_argument("--parent-pid", type=int, default=os.getppid())
    retry = sub.add_parser("retry", help="Retry only failed steps for one unchanged meeting")
    retry.add_argument("job")
    retry.add_argument("--revision", required=True)
    sub.add_parser("status")
    sub.add_parser("delete-work", help="Finish confirmed local deletions after processing stops")
    sub.add_parser("reviews", help="List saved company-folder reviews")
    review = sub.add_parser("review", help="Open the local company-folder review page")
    review.add_argument("--no-open", action="store_true", help="Serve without opening a browser")
    review.add_argument("--timeout", type=float, default=1800)
    review.add_argument("--print-url", action="store_true", help="Print the private localhost review address")
    sharing_work = sub.add_parser("sharing-work", help="Finish one explicitly requested sharing review")
    sharing_work.add_argument("job")
    sharing_work.add_argument("--revision", required=True)
    suggestion_work = sub.add_parser("suggestion-work", help="Find calendar invitees for one requested sharing review")
    suggestion_work.add_argument("job")
    suggestion_work.add_argument("--revision", required=True)
    choose = sub.add_parser("choose-company", help="Approve a company for one exact saved review")
    choose.add_argument("job")
    choose.add_argument("company")
    choose.add_argument("--revision", required=True)
    keep = sub.add_parser("keep-local", help="Keep one reviewed meeting local")
    keep.add_argument("job")
    keep.add_argument("--revision", required=True)
    release = sub.add_parser("release")
    release.add_argument("job")
    release.add_argument("stage", choices=WORKFLOW_STAGES)
    attach = sub.add_parser("attach-transcript", help="Attach this session's transcript to a held job; does not start processing")
    attach.add_argument("job")
    attach.add_argument("transcript")
    name_local = sub.add_parser("name-local", help="Apply date and validated title to one local meeting folder")
    name_local.add_argument("job")
    name_local.add_argument("--refresh", action="store_true", help="Update an already named folder to the current date format")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    queue = Queue(args.state)
    try:
        if args.command in ("enqueue", "complete"):
            print(queue.enqueue(args.session, config))
            queue.write_status()
            if args.command == "complete":
                start_worker(args.config, queue.state)
        elif args.command == "status":
            # Deliberately omit command arguments and settings, which can
            # contain user-supplied connection information.
            print(json.dumps([{key: task[key] for key in
                               ("job", "stage", "status", "attempts", "next_attempt", "error")}
                              for task in queue.tasks()], indent=2))
        elif args.command == "release":
            queue.release(args.job, args.stage, config)
            queue.write_status()
            start_worker(args.config, queue.state)
        elif args.command == "retry":
            retry_meeting(args.job, args.revision, args.config, queue.state)
            print("Retry requested for this meeting.")
        elif args.command == "supervise":
            # Enforce the same app-parent boundary before startup recovery can
            # dispatch a lookup; supervise() keeps checking it for its lifetime.
            if args.parent_pid <= 1 or os.getppid() != args.parent_pid:
                raise ValueError("The processing supervisor must be started by the running Quill app")
            with worker_termination():
                reconcile_automatic_suggestions(queue, args.config)
                # A startup calendar-process launch may fail after its durable
                # pending claim. Treat bounded dispatch recovery as work so
                # the normal supervisor starts and tracks a worker even when
                # every recording stage has already completed.
                supervise(_SupervisedCompletionQueue(queue, args.config), args.config,
                          lambda config, state: start_worker(config, state, supervised=True), args.parent_pid)
        elif args.command == "attach-transcript":
            queue.attach_transcript(args.job, args.transcript)
            print("Transcript attached. Notes remain held until explicitly released.")
        elif args.command == "name-local":
            from quill_workflow.local_naming import request_naming, name_session
            with (queue.state / "worker.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with queue.db:
                    request_naming(queue, args.job, config)
                destination = name_session(queue, args.job, refresh=args.refresh)
                print(str(destination) if destination else "No validated title; existing dated folder kept.")
        elif args.command == "reviews":
            reviews = RoutingReviews(queue.state)
            try:
                print(json.dumps(reviews.list(), indent=2))
            finally:
                reviews.close()
        elif args.command in ("choose-company", "keep-local"):
            reviews = RoutingReviews(queue.state)
            try:
                if args.command == "choose-company":
                    record = reviews.approve(args.job, args.company, args.revision)
                else:
                    record = reviews.skip(args.job, args.revision)
            finally:
                reviews.close()
            resume_reviewed_job(record, args.config, queue.state)
            print("Folder choice saved." if record["status"] == "approved" else "Meeting will stay local.")
        elif args.command == "review":
            from quill_workflow.review_server import serve_review
            from quill_workflow.local_deletion import pending_deletions
            if pending_deletions(queue.state):
                start_deletion_worker(args.config, queue.state)
            if reconcile_approved_reviews(queue, config) or queue.has_work():
                start_worker(args.config, queue.state)
            serve_review(queue.state,
                         on_decision=lambda record: resume_reviewed_job(record, args.config, queue.state),
                         resume_options=lambda record: resumable_stages(record, args.config, queue.state),
                         on_resume=lambda record, stages: resume_waiting_work(record, args.config, queue.state, stages),
                         processing_options=lambda: processing_meetings(args.config, queue.state),
                         on_retry=lambda job, revision: retry_meeting(job, revision, args.config, queue.state),
                         sharing_options=lambda: sharing_options(args.config, queue.state),
                         on_sharing=lambda action, job, revision, emails=None, suggestion=None: sharing_decision(
                             action, job, revision, emails, args.config, queue.state, suggestion),
                         deletion_options=lambda: deletion_options(args.config, queue.state),
                         on_delete=lambda action, job, revision, confirmation=None: deletion_decision(
                             action, job, revision, args.config, queue.state, confirmation),
                         open_browser=not args.no_open, timeout_seconds=args.timeout,
                         on_ready=(lambda url: print(url, flush=True)) if args.print_url else None)
        elif args.command == "delete-work":
            from quill_workflow.local_deletion import _lock, ProcessingBusy
            try:
                with worker_termination(), _lock(queue.state / "deletion-worker.lock"):
                    while reconcile_deletions(args.config, queue.state):
                        time.sleep(0.5)
            except ProcessingBusy:
                pass  # The existing deletion worker owns this durable request.
        elif args.command in ("sharing-work", "suggestion-work"):
            from quill_workflow.sharing import SharingReviews
            reviews = SharingReviews(queue.state, args.config)
            try:
                with worker_termination():
                    result = (reviews.run_suggestions(args.job, args.revision) if args.command == "suggestion-work"
                              else reviews.run(args.job, args.revision))
                success = (result.get("suggestion_status") == "ready" if args.command == "suggestion-work"
                           else result["status"] in ("ready", "shared"))
                return 0 if success else 1
            finally:
                reviews.close()
        else:
            with worker_termination(), (queue.state / "worker.lock").open("a") as lock:
                if not acquire_worker_lock(lock, queue.state):
                    return 0
                worker_event(queue.state, "running")
                try:
                    queue.recover()
                    queue.write_status()
                    runner = ConnectorRunner(queue.state, Path(__file__).parent / "skills/call-summary/SKILL.md",
                                             lock.fileno(), config_path=args.config)
                    while True:
                        deletion_pending = reconcile_deletions(args.config, queue.state)
                        if deletion_pending:
                            start_deletion_worker(args.config, queue.state)
                        current = load_config(args.config)
                        reconcile_approved_reviews(queue, current)
                        reconcile_automatic_suggestions(queue, args.config)
                        queue.run_ready(runner, enabled=lambda: enabled_stages(args.config),
                                        on_completed=lambda stage, job, receipt: completion_autosuggest(
                                            stage, job, receipt, args.config, queue.state))
                        from quill_workflow.local_naming import name_pending
                        name_pending(queue, load_config(args.config))
                        queue.write_status()
                        reconcile_approved_reviews(queue, load_config(args.config))
                        reconcile_automatic_suggestions(queue, args.config)
                        if args.once or (not queue.has_work() and not pending_automatic_suggestions(args.config, queue.state)):
                            break
                        time.sleep(5)
                except BaseException as error:
                    worker_event(queue.state, "failed", str(error) or type(error).__name__)
                    raise
                else:
                    worker_event(queue.state, "succeeded")
        return 0
    finally:
        queue.close()


if __name__ == "__main__":
    os.umask(0o077)
    try:
        raise SystemExit(main())
    except (WorkflowError, RecordingSafetyHold, OSError, ValueError) as error:
        print("quill workflow: " + str(error), file=sys.stderr)
        raise SystemExit(1)
