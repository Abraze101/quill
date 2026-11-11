"""Refresh one held folder review from the current local configuration."""
from contextlib import closing
import json
import os
import sqlite3
import time
import uuid

from .local_deletion import job_lock
from .pipeline import WorkflowError, load_config, read_json, route_settings, safe_member
from .routing import RoutingReviews, _digest, freeze_companies, preserves_company_map, transcript_identity, validate_companies


def _held_settings(queue, job, record):
    queue.require_processing(job)
    tasks = {task["stage"]: task for task in queue.tasks(job)}
    route, notes = tasks.get("route", {}), tasks.get("notes", {})
    if (route.get("status") != "held" or route.get("receipt") is not None
            or notes.get("status") != "succeeded"):
        raise WorkflowError("Only a held folder review with completed notes can be refreshed")
    for stage in ("archive", "publish"):
        task = tasks.get(stage)
        if task and (task["status"] not in ("held", "pending") or task["attempts"] or task["receipt"] is not None):
            raise WorkflowError("This meeting has filing activity; its folder choices cannot be refreshed")
    result = queue.state / "results" / job
    if result.is_symlink() or any(member.name == "route.json" or member.name.startswith(
            ("archive-", "publish-", "drive-", "route-receipt")) for member in result.iterdir()):
        raise WorkflowError("This meeting already has filing evidence")
    sharing = queue.state / "sharing.sqlite3"
    if sharing.is_symlink():
        raise WorkflowError("Cannot verify the meeting's sharing state")
    if sharing.exists():
        with closing(sqlite3.connect(sharing.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT 1 FROM sharing_reviews WHERE job=?", (job,)).fetchone():
                raise WorkflowError("This meeting already has a sharing review")
    attached = result / "input"
    source = attached if os.path.lexists(attached / "transcript.json") else queue.state / "recordings" / job
    if source.is_symlink():
        raise WorkflowError("The saved transcript folder is linked")
    path = safe_member(source, "transcript.json")
    if path.stat().st_size > 20_000_000:
        raise WorkflowError("The saved transcript is too large")
    digest, _ = transcript_identity(path.read_bytes())
    for receipt in (json.loads(notes["receipt"] or "{}"), read_json(result / "notes-receipt.json")):
        if (record["transcript_sha256"] != digest or receipt.get("input_sha256") != digest
                or receipt.get("job") != job or receipt.get("stage") != "notes"):
            raise WorkflowError("The review and completed notes must match the saved transcript")
    return json.loads(route["settings"])


def _recovery_choices(record, frozen, original, live):
    target_hash = record.get("refresh_target_sha256")
    target = record.get("refresh_target_companies")
    if target is None:
        # Earlier markers saved only the hash. Either the task or the current
        # configuration can identify that exact map. Otherwise undo the refresh
        # only if the task still contains the verified original.
        target = next((choices for choices in (frozen, freeze_companies(live))
                       if _digest(choices) == target_hash), None)
        if target is None and frozen == original:
            return original
    if target is None or _digest(target) != target_hash or frozen not in (original, target):
        raise WorkflowError("The interrupted refresh no longer matches the saved routing task")
    if preserves_company_map(live, original) and preserves_company_map(live, target):
        return target
    return original


def refresh_folder_choices(queue, config_path, job, revision):
    """The original revision resumes a crash between the two database commits."""
    with job_lock(queue.state, job), closing(RoutingReviews(queue.state)) as reviews, queue.db, reviews.db:
        queue.db.execute("BEGIN IMMEDIATE")
        reviews.db.execute("BEGIN IMMEDIATE")
        record = reviews.get(job)
        if (not record or record.get("job") != job or record.get("revision") != revision
                or record.get("status") not in ("pending", "refreshing")
                or any(record.get(key) is not None for key in (
                    "approved_route", "decision_from_revision", "decision_source", "automatic_basis"))):
            raise WorkflowError("This folder review changed or already has a decision; reload it")
        settings = _held_settings(queue, job, record)
        frozen = route_settings(settings)["companies"]
        original = validate_companies(record["companies"])
        live = route_settings(load_config(config_path).get("route", {}))["companies"]
        if _digest(original) != record["company_map_sha256"]:
            raise WorkflowError("The saved folder review no longer matches its original choices")
        if record["status"] == "pending":
            if original != frozen:
                raise WorkflowError("The folder review and queued routing settings disagree")
            if not preserves_company_map(live, original):
                raise WorkflowError("The current folder configuration does not preserve the reviewed destinations")
            current = freeze_companies(live)
            if current == original:
                return record
            # WAL cannot atomically commit an attached database. Close the old
            # form durably before touching the task. Until the final commit,
            # both approval and skip reject this status, even after a crash.
            record.update(status="refreshing", refresh_target_companies=current,
                          refresh_target_sha256=_digest(current))
            reviews._save(record)
            reviews.db.commit()
            reviews.db.execute("BEGIN IMMEDIATE")
        else:
            current = _recovery_choices(record, frozen, original, live)
        if frozen != current:
            settings["companies"] = current
            queue.db.execute("UPDATE tasks SET settings=? WHERE job=? AND stage='route'",
                             (json.dumps(settings, sort_keys=True), job))
        queue.db.commit()
        record.pop("refresh_target_sha256")
        record.pop("refresh_target_companies", None)
        record.update(status="pending", companies=current, company_map_sha256=_digest(current),
                      revision=uuid.uuid4().hex, updated_at=time.time())
        reviews._save(record)
        return record
