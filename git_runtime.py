"""Bound Git transactions and isolate their children from worker stop signals."""

import os
import signal
import subprocess
import time


class GitDeadlineExceeded(TimeoutError):
    pass


class GitCommandRunner:
    """One wall-clock budget shared by every command in a transaction."""

    def __init__(self, seconds=30.0, deadline_getter=None):
        if not 1 <= seconds <= 60:
            raise ValueError('Git transaction budget must be between 1 and 60 seconds')
        self.deadline = time.monotonic() + seconds
        self.deadline_getter = deadline_getter

    def run(self, command, *, timeout=30, capture_output=False, text=False, check=False):
        deadline = self.deadline
        if self.deadline_getter is not None:
            shared = self.deadline_getter()
            if shared is not None:
                deadline = min(deadline, shared)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitDeadlineExceeded('Git transaction time budget exhausted')
        limit = min(float(timeout), remaining)
        options = {'text': text, 'stdin': subprocess.DEVNULL}
        if text:
            options['errors'] = 'replace'
        if capture_output:
            options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.name == 'posix':
            # The runner signals the worker's process group during shutdown.
            # A final Git checkpoint needs its own bounded process group.
            options['start_new_session'] = True
        elif os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(command, **options)
        try:
            stdout, stderr = process.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            if os.name == 'posix':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            # Kill the entire POSIX group, including git-remote-https; otherwise
            # inherited pipes can leave communicate() waiting indefinitely.
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            raise GitDeadlineExceeded('Git command exceeded the transaction time budget') from None
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command, stdout, stderr)
        return result
