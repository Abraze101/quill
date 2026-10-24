"""Conservative identity for completed recordings across macOS device renumbering.

A volume UUID supplements, never replaces, the saved directory inode and app
session UUID. Changed device numbers are accepted only after checking every
completed source file against the immutable queue manifest. Legacy mismatches
cannot bootstrap a new identity from the folder they are supposed to verify.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from .recording_safety import RecordingSafetyHold, read_safety


class _AttrList(ctypes.Structure):
    # Darwin sys/attr.h: five 32-bit attribute groups after the 16-bit header.
    _fields_ = [("bitmapcount", ctypes.c_uint16), ("reserved", ctypes.c_uint16),
                ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                ("forkattr", ctypes.c_uint32)]


def _volume_uuid_bytes(path):
    function = ctypes.CDLL(None, use_errno=True).getattrlist
    function.argtypes = [ctypes.c_char_p, ctypes.POINTER(_AttrList), ctypes.c_void_p,
                         ctypes.c_size_t, ctypes.c_ulong]
    function.restype = ctypes.c_int
    # ATTR_VOL_INFO is required with ATTR_VOL_UUID. The result is a 32-bit
    # byte length followed by uuid_t. Query the recording's actual volume,
    # including APFS firmlinks; never infer it from a disk number or label.
    attributes = _AttrList(5, 0, 0, 0x80040000, 0, 0, 0)
    buffer = ctypes.create_string_buffer(20)
    if function(os.fsencode(path), ctypes.byref(attributes), buffer, len(buffer), 1) != 0:  # FSOPT_NOFOLLOW
        raise OSError(ctypes.get_errno(), "Cannot read recording volume UUID")
    if int.from_bytes(buffer.raw[:4], sys.byteorder) != len(buffer):
        raise ValueError("Unexpected volume UUID result")
    return buffer.raw[4:20]


def volume_identity(path):
    """Return a persistent filesystem volume UUID, or no recovery capability."""
    if sys.platform != "darwin":
        return None
    try:
        value = uuid.UUID(bytes=_volume_uuid_bytes(path))
        return "macos-volume:" + str(value) if value.int else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _directory(path):
    info = Path(path).lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RecordingSafetyHold("Original recording folder is missing or linked")
    return info


def _manifest(queue, job):
    from .pipeline import read_json
    manifest = read_json(queue.state / "recordings" / job / "manifest.json")
    if not isinstance(manifest, dict) or not {"meta.json", "mic.caf", "system.caf"} <= manifest.keys():
        raise RecordingSafetyHold("Saved recording manifest needs review")
    for name, value in manifest.items():
        if (not isinstance(name, str) or name in ("", ".", "..") or Path(name).name != name
                or not isinstance(value, dict) or set(value) != {"sha256", "size"}
                or type(value["size"]) is not int or value["size"] <= 0
                or not isinstance(value["sha256"], str) or len(value["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in value["sha256"])):
            raise RecordingSafetyHold("Saved recording manifest needs review")
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return manifest, digest


def _verify_files(path, manifest, expected_directory):
    """Pin the directory and each regular file while checking content hashes."""
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory_info = os.fstat(directory_fd)
        if (directory_info.st_dev, directory_info.st_ino) != expected_directory:
            raise RecordingSafetyHold("Original recording folder changed during verification")
        for name, expected in manifest.items():
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != expected["size"]:
                    raise RecordingSafetyHold("Original recording changed since completion")
                digest = hashlib.sha256()
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                after = os.fstat(handle.fileno())
                at_path = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                fingerprint = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                if (fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(at_path)
                        or digest.hexdigest() != expected["sha256"]):
                    raise RecordingSafetyHold("Original recording changed since completion")
        final = _directory(path)
        if (final.st_dev, final.st_ino) != expected_directory:
            raise RecordingSafetyHold("Original recording folder changed during verification")
    finally:
        os.close(directory_fd)


def remember_identity(queue, job, path, safety):
    """Migrate only after the caller verifies the original device and inode."""
    if queue.db.execute("SELECT 1 FROM recording_identities WHERE job=?", (job,)).fetchone():
        return
    binding = queue.db.execute("SELECT * FROM recording_safety_bindings WHERE job=?", (job,)).fetchone()
    info = _directory(path)
    if (not binding or (info.st_dev, info.st_ino) != (binding["device"], binding["inode"])
            or not binding["session_id"] or binding["session_id"].lower() != safety["session_id"].lower()):
        raise RecordingSafetyHold("Original recording identity cannot be migrated without review")
    volume = volume_identity(path)
    if volume is None:
        # Normal processing still uses the original device/inode binding.
        # No volume identity means a later device change must fail closed.
        return
    manifest, digest = _manifest(queue, job)
    _verify_files(path, manifest, (info.st_dev, info.st_ino))
    read_safety(path, required=True, expected_session_id=safety["session_id"])
    identity = {"version": 1, "volume": volume, "inode": info.st_ino,
                "session_id": safety["session_id"].lower(), "manifest_sha256": digest,
                "birthtime": getattr(info, "st_birthtime", None)}
    active_transaction = queue.db.in_transaction
    queue.db.execute("INSERT INTO recording_identities(job,identity) VALUES (?,?)",
                     (job, json.dumps(identity, sort_keys=True)))
    if not active_transaction:
        queue.db.commit()


def verify_directory(queue, job, path, device, inode):
    """Accept only the original directory, including a verified device change."""
    from .pipeline import WorkflowError
    try:
        info = _directory(path)
        if info.st_ino != inode:
            raise RecordingSafetyHold("Original recording folder was replaced; review is required")
        row = queue.db.execute("SELECT identity FROM recording_identities WHERE job=?", (job,)).fetchone()
        if not row:
            if info.st_dev == device:
                return info
            raise RecordingSafetyHold("Original recording device changed without a saved volume identity; review is required")
        saved = json.loads(row["identity"])
        if (not isinstance(saved, dict) or type(saved.get("version")) is not int or saved["version"] != 1
                or type(saved.get("inode")) is not int or saved["inode"] != inode
                or not isinstance(saved.get("session_id"), str)
                or not saved.get("volume") or volume_identity(path) != saved["volume"]
                or saved.get("birthtime") != getattr(info, "st_birthtime", None)):
            raise RecordingSafetyHold("Original recording volume or folder changed; review is required")
        # Device and inode numbers can both be reused on another mounted
        # volume. An enrolled persistent identity applies on the fast path too.
        read_safety(path, required=True, expected_session_id=saved["session_id"], require_approval=False)
        if info.st_dev == device:
            return info
        read_safety(path, required=True, expected_session_id=saved["session_id"])
        manifest, digest = _manifest(queue, job)
        if digest != saved.get("manifest_sha256"):
            raise RecordingSafetyHold("Saved recording manifest changed; review is required")
        _verify_files(path, manifest, (info.st_dev, info.st_ino))
        read_safety(path, required=True, expected_session_id=saved["session_id"])
        active_transaction = queue.db.in_transaction
        # Update only references that matched the old binding. In particular,
        # never repair a changed inode or a different stored naming identity.
        for table in ("recording_safety_bindings", "local_names"):
            queue.db.execute("UPDATE " + table + " SET device=? WHERE job=? AND device=? AND inode=?",
                             (info.st_dev, job, device, inode))
        if not active_transaction:
            queue.db.commit()
        return info
    except RecordingSafetyHold:
        raise
    except (WorkflowError, OSError, ValueError, TypeError, KeyError) as error:
        raise RecordingSafetyHold("Original recording identity needs review: " + str(error)) from error
