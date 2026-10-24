#!/usr/bin/python3
"""Chrome/Edge native messaging bridge. Reports signals; never records."""
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import sys
import tempfile
import time


STATUS_MAX_BYTES = 16384
STATUS_FRESHNESS = 8
STATUS_FUTURE_TOLERANCE = 2
SIGNAL_IDENTITY = re.compile(r"[A-Za-z0-9-]{1,128}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate status key")
        result[key] = value
    return result


def private_status_bytes(path):
    """Read a bounded private regular file, without following any symlink."""
    path = Path(path).absolute()
    directory_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) & 0o077
                or not 0 < before.st_size <= STATUS_MAX_BYTES):
            raise ValueError("Recording status is not a private bounded regular file")
        payload = handle.read(STATUS_MAX_BYTES + 1)
        after = os.fstat(handle.fileno())
        if (len(payload) != before.st_size or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns):
            raise ValueError("Recording status changed while reading")
        return payload


def recording_status(directory, meeting, now=None):
    """Expose only fresh recording state; never infer success from an old file."""
    unavailable = {"state": "unavailable"}
    try:
        snapshot = json.loads(private_status_bytes(Path(directory).parent / "recording-status.json"),
                              object_pairs_hook=unique_object)
        if (not isinstance(snapshot, dict)
                or set(snapshot) != {"version", "timestamp", "phase", "deliberatelySkipped"}
                or type(snapshot["version"]) is not int or snapshot["version"] != 1):
            return unavailable
        timestamp = snapshot["timestamp"]
        current = time.time() if now is None else now
        if (type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                or not current - STATUS_FRESHNESS <= timestamp <= current + STATUS_FUTURE_TOLERANCE):
            return unavailable
        phase = snapshot["phase"]
        if not isinstance(phase, str) or phase not in ("idle", "starting", "recording", "paused", "busy"):
            return unavailable
        skipped = snapshot["deliberatelySkipped"]
        if (not isinstance(skipped, list) or len(skipped) > 64
                or any(not isinstance(value, str) or not SIGNAL_IDENTITY.fullmatch(value) for value in skipped)
                or len(set(skipped)) != len(skipped)):
            return unavailable
        if meeting in skipped:
            return {"state": "skipped"}
        return {"state": {"idle": "not-recording", "busy": "not-recording"}.get(phase, phase)}
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return unavailable


def atomic_json(path, data):
    """Publish one private signal without importing anything from the checkout."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".quill-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def browser_window(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid browser window")
    limits = {"id": (1, 2147483647), "left": (-131072, 131072),
              "top": (-131072, 131072), "width": (1, 65536), "height": (1, 65536)}
    result = {}
    for key, (minimum, maximum) in limits.items():
        number = value.get(key)
        # bool is a Python int subclass; accepting it would disagree with the
        # browser's integer validation and Swift's Codable representation.
        if type(number) is not int or not minimum <= number <= maximum:
            raise ValueError("Invalid browser window geometry")
        result[key] = number
    return result


def accept(message, directory, now=None):
    if not isinstance(message, dict):
        raise ValueError("Expected an object")
    if message.get("type") == "recording-status":
        if (set(message) != {"type", "requestID", "meeting"}
                or not isinstance(message.get("meeting"), str)
                or not SIGNAL_IDENTITY.fullmatch(message["meeting"])
                or not isinstance(message.get("requestID"), str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", message["requestID"])):
            raise ValueError("Invalid recording status request")
        # A probe does not constitute evidence that a meeting was joined and
        # must never create, refresh, or clear a durable meeting signal.
        return {"accepted": True, "requestID": message["requestID"],
                "recordingStatus": recording_status(directory, message["meeting"], now)}
    for key in ("source", "meeting"):
        if not isinstance(message.get(key), str) or not SIGNAL_IDENTITY.fullmatch(message[key]):
            raise ValueError("Invalid signal identity")
    if message.get("provider") not in ("google-meet", "zoom-web") or message.get("state") not in ("joined", "left", "unknown"):
        raise ValueError("Invalid meeting signal")
    request_id = message.get("requestID")
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", request_id)):
        raise ValueError("Invalid delivery identity")
    event = {key: message[key] for key in ("source", "meeting", "provider", "state")}
    if "browserWindow" in message:
        geometry = browser_window(message["browserWindow"])
        if event["state"] != "left":
            event["browserWindow"] = geometry
    event["timestamp"] = time.time() if now is None else now
    atomic_json(Path(directory) / (event["source"] + "-" + event["meeting"] + ".json"), event)
    # Acknowledgment follows the atomic signal write. The browser retains each
    # terminal event until this exact delivery identity is acknowledged.
    answer = {"accepted": True, "recordingStatus": recording_status(directory, message["meeting"], now)}
    if request_id is not None:
        answer["requestID"] = request_id
    return answer


def read_exact(stream, length):
    result = bytearray()
    while len(result) < length:
        chunk = stream.read(length - len(result))
        if not chunk:
            break
        result.extend(chunk)
    return bytes(result)


def serve(source, target, directory):
    while True:
        header = read_exact(source, 4)
        if not header:
            return
        if len(header) != 4:
            raise ValueError("Truncated native message header")
        length = struct.unpack("=I", header)[0]
        if not 0 < length <= 4096:
            raise ValueError("Native message too large")
        body = read_exact(source, length)
        if len(body) != length:
            raise ValueError("Truncated native message")
        try:
            answer = accept(json.loads(body), directory)
        except (ValueError, OSError):
            answer = {"accepted": False}
        payload = json.dumps(answer).encode()
        target.write(struct.pack("=I", len(payload)) + payload)
        target.flush()


if __name__ == "__main__":
    os.umask(0o077)
    # Chrome/Edge supplies its extension origin. Registration restricts that
    # origin to the specific extension ID chosen by the user.
    if len(sys.argv) != 2 or not re.fullmatch(r"chrome-extension://[a-p]{32}/", sys.argv[1]):
        raise SystemExit("Use this host through its registered browser extension")
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer, Path.home() / ".local/state/quill/meeting-signals")
    except (ValueError, BrokenPipeError):
        raise SystemExit(1)
