import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from git_runtime import GitCommandRunner, GitDeadlineExceeded


class GitRuntimeTests(unittest.TestCase):
    def test_existing_and_new_transactions_observe_same_shutdown_deadline(self):
        child = Mock(returncode=0)
        child.communicate.return_value = ('', '')
        shared = None
        with patch('git_runtime.time.monotonic', side_effect=[100, 101, 110, 111, 113, 116]), \
                patch('git_runtime.subprocess.Popen', return_value=child) as spawn:
            first = GitCommandRunner(seconds=30, deadline_getter=lambda: shared)
            first.run(['fixture', 'push'], timeout=90)
            shared = 115
            first.run(['fixture', 'fetch'], timeout=90)
            second = GitCommandRunner(seconds=30, deadline_getter=lambda: shared)
            second.run(['fixture', 'push'], timeout=90)
            with self.assertRaises(GitDeadlineExceeded):
                second.run(['fixture', 'retry'], timeout=90)
        self.assertEqual(spawn.call_count, 3)
        self.assertEqual([c.kwargs['timeout'] for c in child.communicate.call_args_list], [29, 5, 2])

    def test_commands_share_one_deadline_instead_of_resetting_timeout(self):
        child = Mock(returncode=0)
        child.communicate.return_value = ('', '')
        with patch('git_runtime.time.monotonic', side_effect=[100, 101, 110]), \
                patch('git_runtime.subprocess.Popen', return_value=child):
            runner = GitCommandRunner(seconds=30)
            runner.run(['fixture', 'push'], timeout=90)
            runner.run(['fixture', 'fetch'], timeout=90)
        self.assertEqual([c.kwargs['timeout'] for c in child.communicate.call_args_list], [29, 20])

    def test_no_new_process_is_started_after_deadline(self):
        with patch('git_runtime.time.monotonic', side_effect=[100, 131]), \
                patch('git_runtime.subprocess.Popen') as spawn:
            runner = GitCommandRunner(seconds=30)
            with self.assertRaises(GitDeadlineExceeded):
                runner.run(['fixture', 'push'])
        spawn.assert_not_called()

    def test_hung_process_has_a_real_bounded_timeout(self):
        started = time.monotonic()
        with self.assertRaises(GitDeadlineExceeded):
            GitCommandRunner(seconds=1).run(
                [sys.executable, '-c', 'import time; time.sleep(30)'],
                timeout=90, capture_output=True, text=True,
            )
        self.assertLess(time.monotonic() - started, 4)

    @unittest.skipUnless(os.name == 'posix', 'POSIX worker signal groups are checked on Linux CI')
    def test_worker_group_signal_does_not_interrupt_final_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'complete'
            ready = Path(root) / 'ready'
            script = r'''
import signal, sys
from git_runtime import GitCommandRunner
signal.signal(signal.SIGTERM, lambda *_: None)
checkpoint = "import sys,time; from pathlib import Path; Path(sys.argv[1]).write_text('ready'); time.sleep(.7); Path(sys.argv[2]).write_text('saved')"
result = GitCommandRunner(seconds=3).run([sys.executable, '-c', checkpoint, sys.argv[1], sys.argv[2]], capture_output=True)
raise SystemExit(result.returncode)
'''
            worker = subprocess.Popen([sys.executable, '-c', script, str(ready), str(marker)],
                                      start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                os.killpg(worker.pid, signal.SIGTERM)
                _, stderr = worker.communicate(timeout=5)
                self.assertEqual(worker.returncode, 0, stderr)
                self.assertEqual(marker.read_text(), 'saved')
            finally:
                if worker.poll() is None:
                    os.killpg(worker.pid, signal.SIGKILL)
                    worker.communicate(timeout=5)

    @unittest.skipUnless(os.name == 'posix', 'POSIX helper process cleanup is checked on Linux CI')
    def test_timed_out_command_also_kills_its_pipe_holding_helper(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'helper-finished'
            child_code = "import time,sys; from pathlib import Path; time.sleep(2); Path(sys.argv[1]).write_text('leaked')"
            parent_code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(30)"
            started = time.monotonic()
            with self.assertRaises(GitDeadlineExceeded):
                GitCommandRunner(seconds=1).run(
                    [sys.executable, '-c', parent_code, child_code, str(marker)],
                    capture_output=True, timeout=90,
                )
            self.assertLess(time.monotonic() - started, 2)
            time.sleep(1.3)
            self.assertFalse(marker.exists(), 'Descendant survived the transaction timeout')
