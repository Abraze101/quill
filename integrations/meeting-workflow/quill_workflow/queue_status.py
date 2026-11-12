"""Small user-facing queue summaries; no transcript content or settings exposed."""
import hashlib
import json
from pathlib import Path

from .worker_supervision import health

STAGE_LABELS = {"notes": "Writing notes", "route": "Checking the filing destination",
                "archive": "Filing recording", "publish": "Uploading notes",
                "prepare_media": "Uploading recording privately"}
INTENTIONAL_HOLDS = {"Stage disabled in current configuration", "Required routing is disabled",
                     "Choose this meeting's company in the routing review"}


def retry_revision(tasks):
    fields = ("job", "stage", "status", "attempts", "next_attempt", "settings", "receipt")
    return hashlib.sha256(json.dumps([{key: task[key] for key in fields} for task in tasks],
                                    sort_keys=True).encode()).hexdigest()


def meetings(queue, job=None, *, summary_tasks=None, stopped=None, skipped=None):
    from .routing import RoutingReviews
    from .local_deletion import stopped_jobs
    arguments = (job,) if job is not None else ()
    removed = {row[0] for row in queue.db.execute(
        "SELECT job FROM local_deletions" + (" WHERE job=?" if arguments else ""), arguments)}
    tasks = queue.tasks(job) if summary_tasks is None else summary_tasks
    cancelled = stopped_jobs(queue.state, job) if stopped is None else stopped
    if skipped is None and (queue.state / "reviews.sqlite3").exists():
        reviews = RoutingReviews(queue.state)
        try:
            if job is None:
                skipped = {record["job"] for record in reviews.list("skipped")}
            else:
                record = reviews.get(job)
                skipped = {job} if record and record["status"] == "skipped" else set()
        finally:
            reviews.close()
    skipped = skipped or set()
    titles = {row["id"]: Path(row["destination"] or row["session"]).name[:180]
              for row in queue.db.execute("SELECT j.id,j.session,n.destination FROM jobs j "
                                           "LEFT JOIN local_names n ON j.id=n.job"
                                           + (" WHERE j.id=?" if arguments else ""), arguments)}
    grouped = {}
    for task in tasks:
        if task["job"] not in removed:
            grouped.setdefault(task["job"], []).append(task)
    summaries = []
    for job, all_tasks in grouped.items():
        saved = [task for task in all_tasks if job not in skipped or task["stage"] == "notes"]
        running_tasks = [task for task in saved if task["status"] == "running"]
        running = next(iter(running_tasks), None)
        failed = next((task for task in saved if task["stage"] != "prepare_media" and
                       (task["status"] in ("failed", "safety_held") or task["status"] == "held" and task["error"]
                        and task["error"] not in INTENTIONAL_HOLDS and task["error"] != "Transcribing on this Mac")), None)
        retry = next((task for task in saved if task["stage"] != "prepare_media" and task["status"] == "retry"), None)
        transcribing = any(task["stage"] == "notes" and task["status"] == "held"
                           and task["error"] == "Transcribing on this Mac" for task in saved)
        company = any(task["stage"] == "route" and task["status"] == "held"
                      and task["error"] == "Choose this meeting's company in the routing review" for task in saved)
        pending = any(task["status"] == "pending" for task in saved)
        state, detail = "idle", "Finished or kept local"
        chosen = running or failed or retry
        if job in cancelled:
            state, detail = "idle", "Processing cancelled"
        elif running:
            state, detail = "running", " · ".join(STAGE_LABELS[task["stage"]] for task in running_tasks)
        elif failed:
            state, detail = "needs_attention", failed["error"] or "Processing needs attention"
        elif retry:
            state, detail = "retrying", retry["error"] or "Retrying processing"
        elif transcribing:
            state, detail = "queued", "Waiting for local transcription"
        elif company:
            state, detail = "waiting_for_company", "Choose a Drive folder to finish filing"
        elif pending:
            state, detail = "queued", "Waiting for an earlier step"
        elif job in skipped:
            state, detail = "idle", "Kept local"
        retryable = summary_tasks is None and job not in cancelled and not running and not any(task["status"] == "safety_held" for task in saved) and any(task["stage"] != "prepare_media" and task["status"] in ("failed", "retry")
                                       and task["receipt"] is None for task in saved)
        summaries.append({"job": job, "title": titles.get(job, "Meeting"), "state": state,
                          "detail": str(detail)[:500], "retryable": retryable,
                          "revision": retry_revision(all_tasks) if summary_tasks is None else None,
                          "stage": chosen["stage"] if chosen else None})
    return summaries


def compact_status(queue):
    jobs = meetings(queue)
    worker = health(queue.state)
    priority = {"running": 0, "needs_attention": 1, "retrying": 2,
                "queued": 3, "waiting_for_company": 4, "idle": 5}
    chosen = min(jobs, key=lambda item: priority[item["state"]]) if jobs else None
    state = chosen["state"] if chosen else "idle"
    labels = {"idle": "Meeting processing is up to date", "queued": "Waiting for processing",
              "running": chosen["detail"] if chosen else "Processing meeting",
              "retrying": "Retrying meeting processing", "waiting_for_company": "Waiting for company choice",
              "needs_attention": "Processing needs attention"}
    title = labels[state]
    detail = (chosen["title"] + " — " + chosen["detail"] if chosen and state != "idle"
              else "No meetings waiting for processing")
    if worker["state"] == "needs_attention":
        state, title = "needs_attention", "Meeting processing stopped"
        detail = worker["error"] or "Automatic recovery needs attention"
    elif worker["state"] == "recovering" and state != "running":
        state, title = "retrying", "Restarting meeting processing"
        detail = worker["error"] or "The worker will restart automatically"
    failures = [(item["job"], item["detail"]) for item in jobs if item["state"] == "needs_attention"]
    if worker["state"] == "needs_attention":
        failures.append(("worker", worker["error"]))
    alert = hashlib.sha256(json.dumps(failures, sort_keys=True).encode()).hexdigest() if failures else None
    result = {"processing": {"state": state, "title": title, "detail": str(detail)[:500],
                              "retryable_jobs": sum(item["retryable"] for item in jobs),
                              "alert_revision": alert,
                              "attention_detail": str(failures[0][1])[:500] if failures else None}, "worker": worker}
    if chosen and chosen["state"] == "running":
        result["active"] = {key: chosen[key] for key in ("job", "stage", "title")}
    return result
