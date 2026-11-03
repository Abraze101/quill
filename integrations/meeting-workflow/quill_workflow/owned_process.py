"""Cleanup for a Popen child launched with start_new_session=True."""
import os
import signal
import subprocess
import sys


def _darwin_group_finished(pgid):
    # XNU can return EPERM for a group containing only zombies. Check only
    # its group ID and process states; never inspect command lines or signal
    # the individual PIDs reported by ps.
    try:
        result = subprocess.run(
            ["/bin/ps", "-g", str(pgid), "-o", "pgid=,state="],
            capture_output=True, text=True, timeout=1,
            env={"LC_ALL": "C", "COMMAND_MODE": "unix2003"})
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.stderr.strip():
        return False
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode == 1 and not rows:
        # Confirm absence independently. An empty process listing alone is
        # not sufficient if this environment restricts process visibility.
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        return False
    return result.returncode == 0 and bool(rows) and all(
        len(row) == 2 and row[0] == str(pgid) and row[1].startswith("Z") for row in rows)


def stop_process_group(process, grace_seconds=2):
    """Stop the owned session's group, including descendants left by an exited leader."""
    def send(signum):
        if process.returncode is not None:
            # wait()/communicate() reaped our leader. If its PID now exists,
            # that process is new. Never signal a recycled group identifier.
            try:
                os.getpgid(process.pid)
            except ProcessLookupError:
                pass  # Its original descendants may still own the group ID.
            else:
                return False
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            return False
        except PermissionError:
            if sys.platform == "darwin" and process.poll() is not None and _darwin_group_finished(process.pid):
                return False
            raise
        return True

    if send(signal.SIGTERM):
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
        send(signal.SIGKILL)
    process.wait()
