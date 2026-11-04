#!/usr/bin/env python3
"""Read Quill's local meeting library and open its existing review workflow."""
import sys

sys.dont_write_bytecode = True

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import stat
import tempfile
import time


class PluginError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PluginError("Invalid command or options. Use --help for available commands.")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _json(data):
    def nonfinite(value):
        raise ValueError("Nonfinite JSON")
    return json.loads(data, object_pairs_hook=_unique, parse_constant=nonfinite)


def _private_json(path):
    """Read a small owner-only regular file without following symlinks."""
    path = Path(path).absolute()
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) & 0o077 or not 0 < before.st_size <= 16384):
            raise ValueError("Untrusted settings file")
        data = handle.read(16385)
        after = os.fstat(handle.fileno())
    if (len(data) != before.st_size or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns):
        raise ValueError("File changed during read")
    return _json(data)


def _settings_path():
    return Path.home() / ".config/quill/chatgpt-plugin.json"


def _settings():
    try:
        value = _private_json(_settings_path())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        raise PluginError("Quill plugin settings could not be read safely. Check chatgpt-plugin.json.") from None
    if not isinstance(value, dict):
        raise PluginError("Quill plugin settings must be a JSON object.")
    return value


def _repo(path):
    if not isinstance(path, str) or not path.strip():
        raise PluginError("Choose the Quill checkout with --repo PATH.")
    root = Path(path).expanduser().resolve()
    required = ("workflow.py", "native-host.py", "quill_workflow/review_library.py")
    if not all((root / "integrations/meeting-workflow" / item).is_file() for item in required):
        raise PluginError("Quill checkout not found. Use --repo PATH with your Quill repository.")
    return root


def resolve_repo(explicit=None):
    if explicit is not None:
        return _repo(explicit)
    if os.environ.get("QUILL_REPO"):
        return _repo(os.environ["QUILL_REPO"])
    saved = _settings().get("repo_root")
    if saved is not None:
        return _repo(saved)
    for parent in Path(__file__).resolve().parents:
        if (parent / "integrations/meeting-workflow/workflow.py").is_file():
            return _repo(str(parent))
    return _repo(str(Path.home() / "Documents/GitHub/quill"))


def setup(repo):
    path = _settings_path()
    saved = _settings()
    saved["repo_root"] = str(repo)
    data = (json.dumps(saved, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    if len(data) > 16384:
        raise PluginError("Quill plugin settings are too large to update safely.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # This file may contain future private settings. Keep it owner-only and
    # replace atomically without changing the rest of Quill's configuration.
    for parent in (path.parent, *path.parent.parents):
        if parent.is_symlink():
            raise PluginError("Quill plugin settings cannot use linked directories.")
    fd, temporary = tempfile.mkstemp(prefix=".chatgpt-plugin-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"configured": True, "repo_root": str(repo)}


def native_host(repo):
    spec = importlib.util.spec_from_file_location("quill_plugin_native_host",
        repo / "integrations/meeting-workflow/native-host.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "recording_status", None)):
        raise PluginError("This Quill checkout needs an update for recording status. Pull the latest version and try again.")
    return module


def library(repo, *, require_detail=True):
    directory = repo / "integrations/meeting-workflow"
    sys.path.insert(0, str(directory))
    from quill_workflow import review_library
    if Path(review_library.__file__).resolve() != directory / "quill_workflow/review_library.py":
        raise PluginError("A different Quill library is already loaded. Run this command in a fresh process.")
    if require_detail and not all(callable(getattr(review_library, name, None)) for name in ("meeting_library", "meeting_detail")):
        raise PluginError("This Quill checkout needs an update for the plugin. Pull the latest version and try again.")
    return review_library


def processing_status(state, now=None):
    unavailable = {"available": False, "source": "saved_snapshot"}
    try:
        saved = _private_json(state / "status.json")
        updated = saved.get("updated")
        current = time.time() if now is None else now
        if (not isinstance(saved, dict) or type(updated) not in (int, float)
                or not math.isfinite(updated) or updated < 0 or updated > current + 2):
            return unavailable
        processing = saved.get("processing", {})
        phase = processing.get("state") if isinstance(processing, dict) else None
        if phase not in {"idle", "queued", "running", "retrying", "waiting_for_company", "needs_attention"}:
            return unavailable
        tasks = saved.get("tasks", {})
        counts = {key: count for key, count in tasks.items()
                  if key in {"pending", "running", "succeeded", "failed", "retry", "held", "safety_held", "cancelled"}
                  and type(count) is int and 0 <= count <= 1_000_000} if isinstance(tasks, dict) else {}
        pending = saved.get("pending_reviews")
        return {"available": True, "source": "saved_snapshot", "updated": updated,
                "age_seconds": max(0, round(current - updated, 1)), "state": phase, "tasks": counts,
                "pending_reviews": pending if type(pending) is int and 0 <= pending <= 1_000_000 else None}
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
        return unavailable


def summary(job, entry):
    keys = ("title", "display_date", "recorded_at", "participants", "company_id", "folder_name", "storage_status")
    result = {"job": job, **{key: entry.get(key) for key in keys}}
    result["storage"] = {kind: {"status": entry.get("storage", {}).get(kind, {}).get("status", "unknown")}
                         for kind in ("document", "media")}
    return result


def meetings(repo, state, query, limit, offset):
    query = query.casefold()
    entries = [summary(job, entry) for job, entry in library(repo).meeting_library(state).items()]
    entries = [entry for entry in entries if query in " ".join(str(entry.get(key) or "")
               for key in ("title", "participants", "folder_name", "display_date", "job")).casefold()]
    entries.sort(key=lambda item: (-(item["recorded_at"] or 0), item["job"]))
    return {"meetings": entries[offset:offset + limit], "total": len(entries), "offset": offset,
            "next_offset": offset + limit if offset + limit < len(entries) else None,
            "storage_evidence": "saved_receipts_not_live_drive_check"}


def get_meeting(repo, state, job, content, limit, offset):
    entry = library(repo).meeting_detail(state, job)
    if entry is None:
        raise PluginError("Meeting not found or unavailable. List meetings and use an exact job ID.")
    text = entry.get(content + "_text")
    total = len(text) if isinstance(text, str) else 0
    return {"meeting": summary(job, entry), "content": content, "available": text is not None,
            "text": text[offset:offset + limit] if isinstance(text, str) else None,
            "offset": offset, "total_characters": total,
            "next_offset": offset + limit if offset + limit < total else None,
            "preview_shortened": isinstance(text, str) and text.endswith(
                "\n\n[" + ("Notes" if content == "notes" else "Transcript") + " preview shortened.]"),
            "storage_evidence": "saved_receipts_not_live_drive_check"}


def parser():
    result = Parser(description=__doc__)
    result.add_argument("--repo", help="Path to the Quill checkout")
    result.add_argument("--state", default=str(Path.home() / ".local/state/quill/workflow"))
    result.add_argument("--recording-state", default=str(Path.home() / ".local/state/quill"),
                        help="Directory containing the recording heartbeat, independent of workflow --state")
    result.add_argument("--config", default=str(Path.home() / ".config/quill/workflow.json"))
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status", "setup", "review"):
        commands.add_parser(name)
    listing = commands.add_parser("meetings")
    listing.add_argument("--query", default="")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--offset", type=int, default=0)
    detail = commands.add_parser("get")
    detail.add_argument("job")
    detail.add_argument("--content", choices=("notes", "transcript"), default="notes")
    detail.add_argument("--limit", type=int, default=12000)
    detail.add_argument("--offset", type=int, default=0)
    return result


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        if hasattr(args, "limit") and not 1 <= args.limit <= (100 if args.command == "meetings" else 20000):
            raise PluginError("Choose a limit from 1 to 100 meetings or 1 to 20000 text characters.")
        if hasattr(args, "offset") and not 0 <= args.offset <= 1_000_000:
            raise PluginError("Offset must be between 0 and 1000000.")
        if hasattr(args, "query") and (len(args.query) > 160 or any(ord(c) < 32 for c in args.query)):
            raise PluginError("Use a search phrase of up to 160 characters.")
        repo = resolve_repo(args.repo)
        state = Path(args.state).expanduser().absolute()
        if args.command == "setup":
            result = setup(repo)
        elif args.command == "doctor":
            reader = library(repo, require_detail=False)
            result = {"macos": platform.system() == "Darwin", "repo_root": str(repo),
                      "reader_compatible": all(callable(getattr(reader, name, None))
                                               for name in ("meeting_library", "meeting_detail")),
                      "app_installed": any(path.is_dir() for path in (Path("/Applications/Quill.app"), Path.home() / "Applications/Quill.app")),
                      "workflow_state_exists": state.is_dir(), "meeting_database_exists": (state / "jobs.sqlite3").is_file()}
        elif args.command == "status":
            recording_state = Path(args.recording_state).expanduser().absolute()
            result = {"recording": native_host(repo).recording_status(recording_state / "browser-signals", None),
                      "processing": processing_status(state)}
        elif args.command == "meetings":
            result = meetings(repo, state, args.query, args.limit, args.offset)
        elif args.command == "get":
            result = get_meeting(repo, state, args.job, args.content, args.limit, args.offset)
        else:
            # Explicit UI action. The existing process emits its private URL
            # and retains its normal timeout, approvals, and recovery behavior.
            os.execv(sys.executable, [sys.executable, str(repo / "integrations/meeting-workflow/workflow.py"),
                "--config", str(Path(args.config).expanduser().absolute()), "--state", str(state),
                "review", "--no-open", "--print-url", "--timeout", "1800"])
            return 0
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except PluginError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    except (OSError, ValueError, TypeError, ImportError):
        print(json.dumps({"error": "Quill could not complete this local request. Check setup and try again."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
