"""Process-group cleanup uses only Popen-owned, private process groups."""
from pathlib import Path
import select
import signal
import subprocess
import sys
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quill_workflow.owned_process import stop_process_group


class OwnedProcessTests(unittest.TestCase):
    def test_final_denial_is_harmless_only_for_confirmed_gone_or_zombie_group(self):
        pid = 12345
        for code, output, error, probe, finished in [
                (1, "", "", ProcessLookupError, True),
                (1, "", "", PermissionError, False),
                (0, f"{pid} Z\n{pid} Z+\n", "", None, True),
                (0, f"{pid} Z\n{pid} S\n", "", None, False),
                (0, "999 Z\n", "", None, False),
                (1, "", "permission denied", None, False),
                (0, "", "", None, False)]:
            with self.subTest(code=code, output=output, error=error, probe=probe):
                process = Mock(pid=pid, returncode=None)
                def wait(timeout=None):
                    process.returncode = -signal.SIGTERM
                    return process.returncode
                process.wait.side_effect = wait
                process.poll.side_effect = lambda: process.returncode
                with patch("quill_workflow.owned_process.sys.platform", "darwin"), \
                     patch("quill_workflow.owned_process.os.getpgid", side_effect=ProcessLookupError), \
                     patch("quill_workflow.owned_process.os.killpg", side_effect=[None, PermissionError, probe]) as send, \
                     patch("quill_workflow.owned_process.subprocess.run",
                           return_value=Mock(returncode=code, stdout=output, stderr=error)) as inspect_group:
                    if finished:
                        stop_process_group(process)
                    else:
                        with self.assertRaises(PermissionError):
                            stop_process_group(process)
                    expected = [call(pid, signal.SIGTERM), call(pid, signal.SIGKILL)]
                    if probe is not None:
                        expected.append(call(pid, 0))
                    self.assertEqual(send.call_args_list, expected)
                    self.assertEqual(inspect_group.call_args.args[0], ["/bin/ps", "-g", str(pid), "-o", "pgid=,state="])

    def test_live_leader_denial_propagates_without_additional_signals(self):
        process = Mock(pid=12345, returncode=None)
        process.poll.return_value = None
        with patch("quill_workflow.owned_process.os.killpg", side_effect=PermissionError) as send, \
             patch("quill_workflow.owned_process.subprocess.run") as inspect_group:
            with self.assertRaises(PermissionError):
                stop_process_group(process)
            send.assert_called_once_with(process.pid, signal.SIGTERM)
            inspect_group.assert_not_called()

    def test_recycled_pid_cannot_receive_a_group_signal(self):
        process = Mock(pid=12345, returncode=0)
        with patch("quill_workflow.owned_process.os.getpgid", return_value=12345), \
             patch("quill_workflow.owned_process.os.killpg") as send:
            stop_process_group(process)
            send.assert_not_called()

    def test_descendant_ignoring_term_is_stopped_after_its_leader_exited(self):
        # Grandchild inherits the output pipe and announces readiness only after
        # installing SIG_IGN. EOF proves it stopped even though the leader was
        # already reaped before cleanup began.
        child = ("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                 "print('ready', flush=True); time.sleep(5); print('survived', flush=True)")
        launch = ("import subprocess,sys; subprocess.Popen([sys.executable, '-c', sys.argv[1]])")
        leader = subprocess.Popen([sys.executable, "-c", launch, child], start_new_session=True,
                                  stdout=subprocess.PIPE, text=True)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
        try:
            self.assertEqual(leader.stdout.readline(), "ready\n")
            self.assertEqual(leader.wait(timeout=2), 0)
            stop_process_group(leader)
            self.assertTrue(select.select([leader.stdout], [], [], 2)[0], "Owned descendant kept its pipe open")
            self.assertEqual(leader.stdout.read(), "")
            self.assertIsNone(other.poll())
        finally:
            leader.stdout.close()
            if leader.poll() is None:
                leader.kill()
            leader.wait()
            other.terminate()
            other.wait()
