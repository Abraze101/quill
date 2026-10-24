"""Keep an owned writer's private lease alive through its child processes."""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import stat

_lease = ContextVar("quill_meeting_lease", default=None)
ENV = "QUILL_MEETING_LEASE_FD"


@contextmanager
def lease_context(fd):
    token = _lease.set(fd)
    try:
        yield
    finally:
        _lease.reset(token)


def child_lease_fds():
    fd = _lease.get()
    if fd is None:
        value = os.environ.get(ENV)
        if value is None:
            return ()
        if not value.isdecimal():
            raise ValueError("Invalid inherited meeting lease")
        fd = int(value)
    info = os.fstat(fd)
    if (fd < 3 or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("Invalid inherited meeting lease")
    return (fd,)


def adapter_environment():
    environment = dict(os.environ)
    environment.pop(ENV, None)
    leases = child_lease_fds()
    if leases:
        environment[ENV] = str(leases[0])
    return environment
