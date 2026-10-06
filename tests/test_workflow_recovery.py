"""Recovery limits are exercised without credentials, Git or live dispatches."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import workflow_recovery as recovery


REPOSITORY = "owner/repository"


def source(**changes):
    run = dict(id=123, name=recovery.WORKFLOW_NAME, repository={"full_name": REPOSITORY},
               head_branch="main", event="schedule", run_attempt=1, status="completed",
               conclusion="failure", workflow_id=42, run_number=10,
               created_at="2026-10-05T18:00:00Z")
    run.update(changes)
    return run


def api_for(run, history=(), active=()):
    api = Mock()
    api.workflow.return_value = {"state": "active", "id": 42}
    api.run.return_value = deepcopy(run)
    api.history.return_value = list(history)
    api.active_runs.return_value = list(active)
    return api


class WorkflowRecoveryTests(unittest.TestCase):
    def recover(self, run, api):
        return recovery.recover({"action": "completed", "workflow_run": run}, REPOSITORY, api)

    def test_scheduled_runner_failure_requests_exactly_one_normal_worker(self):
        for conclusion in ("failure", "timed_out"):
            with self.subTest(conclusion=conclusion):
                run = source(conclusion=conclusion)
                api = api_for(run)
                result = self.recover(run, api)
                self.assertIn("richiesto", result)
                api.dispatch.assert_called_once_with(123)
                self.assertEqual(run["conclusion"], conclusion)  # Original failure stays visible.

    def test_manual_recovery_cancelled_success_and_rerun_are_never_retried(self):
        changes = ({"event": "workflow_dispatch"}, {"event": "push"},
                   {"event": "pull_request"}, {"run_attempt": 2},
                   {"conclusion": "cancelled"}, {"conclusion": "success"},
                   {"status": "in_progress"}, {"head_branch": "feature"},
                   {"repository": {"full_name": "attacker/fork"}}, {"name": "Other"})
        for change in changes:
            with self.subTest(change=change):
                run = source(**change)
                api = api_for(run)
                self.recover(run, api)
                api.workflow.assert_not_called()
                api.dispatch.assert_not_called()

    def test_recovery_dispatch_failure_does_not_create_a_chain(self):
        run = source(event="workflow_dispatch", display_title=recovery.recovery_title(99))
        api = api_for(run)
        self.recover(run, api)
        api.dispatch.assert_not_called()

    def test_disabled_workflow_and_changed_identity_preserve_owner_stop(self):
        for workflow in ({"state": "disabled_manually", "id": 42},
                         {"state": "active", "id": 999}):
            with self.subTest(workflow=workflow):
                run = source()
                api = api_for(run)
                api.workflow.return_value = workflow
                self.recover(run, api)
                api.dispatch.assert_not_called()
                api.run.assert_not_called()

    def test_owner_rerun_started_after_event_prevents_another_worker(self):
        run = source()
        api = api_for(run)
        api.run.return_value = source(status="in_progress", run_attempt=2)
        self.recover(run, api)
        api.history.assert_not_called()
        api.dispatch.assert_not_called()

    def test_already_queued_recovery_deduplicates_repeated_completion(self):
        run = source()
        prior = source(id=124, event="workflow_dispatch", status="completed", conclusion="failure",
                       display_title=recovery.recovery_title(123), run_number=11)
        api = api_for(run, [run, prior])
        result = self.recover(run, api)
        self.assertIn("già richiesto", result)
        api.dispatch.assert_not_called()

    def test_active_or_pending_worker_even_created_earlier_prevents_dispatch(self):
        for status in recovery.ACTIVE_STATUSES:
            with self.subTest(status=status):
                run = source()
                older = source(id=99, status=status, conclusion=None, run_number=9,
                               created_at="2026-10-05T17:00:00Z")
                api = api_for(run, [run], [older])
                self.assertIn("attivo o in coda", self.recover(run, api))
                api.dispatch.assert_not_called()

    def test_late_failure_event_after_healthy_successor_does_not_restart_service(self):
        run = source()
        later = source(id=124, run_number=11, conclusion="success")
        api = api_for(run, [run, later])
        self.assertIn("successiva", self.recover(run, api))
        api.dispatch.assert_not_called()

    def test_manual_worker_on_another_ref_still_shares_bot_lock_and_blocks_recovery(self):
        run = source()
        other = source(id=124, head_branch="feature", status="in_progress", conclusion=None)
        api = api_for(run, [run, other])
        self.recover(run, api)
        api.dispatch.assert_not_called()

    def test_api_error_keeps_recovery_failed_without_blind_retry(self):
        run = source()
        api = api_for(run)
        api.dispatch.side_effect = RuntimeError("ambiguous API timeout")
        with self.assertRaises(RuntimeError):
            self.recover(run, api)
        api.dispatch.assert_called_once()

    def test_invalid_source_id_is_never_interpolated_into_api_path(self):
        for value in ("123/../../other", 0, True):
            with self.subTest(value=value):
                api = api_for(source(id=value))
                with self.assertRaises(ValueError):
                    self.recover(source(id=value), api)
                api.workflow.assert_not_called()


class GitHubRecoveryAPITests(unittest.TestCase):
    def test_dispatch_uses_fixed_main_workflow_and_explicit_normal_duration(self):
        api = recovery.GitHubAPI(REPOSITORY, "fixture-token")
        with patch.object(api, "request", return_value=None) as request:
            api.dispatch(123)
        request.assert_called_once_with("/workflows/albo_check.yml/dispatches", {
            "ref": "main", "inputs": {"run_seconds": "18000", "recovery_from_run_id": "123"}})

    def test_paginated_history_does_not_miss_recovery_marker(self):
        api = recovery.GitHubAPI(REPOSITORY, "fixture-token")
        marker = source(id=999, event="workflow_dispatch", display_title=recovery.recovery_title(123))
        with patch.object(api, "request", side_effect=[
                {"total_count": 101, "workflow_runs": [source(id=i) for i in range(200, 300)]},
                {"total_count": 101, "workflow_runs": [marker]}]) as request:
            result = api.history("2026-10-05T18:00:00Z")
        self.assertIn(marker, result)
        self.assertEqual(request.call_count, 2)
        queries = [parse_qs(urlparse(call.args[0]).query) for call in request.call_args_list]
        self.assertEqual([q["page"] for q in queries], [["1"], ["2"]])
        self.assertEqual(queries[0]["created"], [">=2026-10-05T18:00:00Z"])

    def test_incomplete_history_fails_closed_instead_of_dispatching(self):
        api = recovery.GitHubAPI(REPOSITORY, "fixture-token")
        batch = {"total_count": 1000, "workflow_runs": [source(id=i) for i in range(100)]}
        with patch.object(api, "request", return_value=batch) as request:
            with self.assertRaises(RuntimeError):
                api.history("2026-10-05T18:00:00Z")
        self.assertEqual(request.call_count, recovery.MAX_HISTORY_PAGES)

    def test_active_queries_have_no_creation_filter_and_deduplicate_results(self):
        api = recovery.GitHubAPI(REPOSITORY, "fixture-token")
        active = source(id=99, status="in_progress")
        with patch.object(api, "request", return_value={"total_count": 1,
                          "workflow_runs": [active]}) as request:
            self.assertEqual(api.active_runs(), [active])
        self.assertEqual(request.call_count, len(recovery.ACTIVE_STATUSES))
        for call in request.call_args_list:
            query = parse_qs(urlparse(call.args[0]).query)
            self.assertNotIn("created", query)
            self.assertNotIn("branch", query)

    def test_http_error_and_timeout_do_not_leak_token_or_retry_post(self):
        api = recovery.GitHubAPI(REPOSITORY, "fixture-sensitive-token")
        failures = (HTTPError("https://fixture.invalid", 403, "fixture-sensitive-token", {}, None),
                    URLError("fixture-sensitive-token"))
        for failure in failures:
            with self.subTest(error=type(failure).__name__), \
                    patch("workflow_recovery.urlopen", side_effect=failure) as request:
                with self.assertRaises(RuntimeError) as raised:
                    api.dispatch(123)
                self.assertNotIn("fixture-sensitive-token", str(raised.exception))
                request.assert_called_once()


class WorkerDurationTests(unittest.TestCase):
    def test_normal_default_and_bounded_smoke_durations(self):
        for value, expected in ((None, 18000), ("", 18000), ("18000", 18000),
                                ("60", 60), ("120", 120)):
            with self.subTest(value=value):
                self.assertEqual(recovery.worker_seconds(value), expected)

    def test_invalid_or_shell_like_duration_is_rejected_before_worker_start(self):
        for value in ("59", "18001", "-1", "1.5", " 120", "120\n", "1e3",
                      "120; echo injected", "$(echo 120)", "`echo 120`", "0120000", 120, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                recovery.worker_seconds(value)

    def test_duration_cli_emits_only_validated_integer_and_invalid_exits_nonzero(self):
        script = str(Path("workflow_recovery.py").resolve())
        for value, expected_code, expected_stdout in (("120", 0, "120\n"),
                                                      ("120; echo injected", 1, "")):
            with self.subTest(value=value):
                result = subprocess.run([sys.executable, script, "duration"],
                                        env=dict(os.environ, BOT_SECONDS_INPUT=value),
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, expected_code)
                self.assertEqual(result.stdout, expected_stdout)
                self.assertNotIn("injected", result.stdout + result.stderr)


@unittest.skipUnless(os.name != "nt" and shutil.which("timeout") and shutil.which("bash"),
                     "Real GNU timeout and POSIX signals are verified on Linux CI")
class WorkerTimeoutExitTests(unittest.TestCase):
    def test_programmed_shutdown_keeps_normal_flush_failure_and_forced_kill_distinct(self):
        wrapper = ('set +e\n'
                   'timeout --preserve-status --signal=INT --kill-after=0.2s 0.4s "$1" -c "$2"\n'
                   'code=$?\nexit "$code"')
        for exit_code in (0, 1, None):
            with self.subTest(worker_exit=exit_code):
                handler = ('signal.SIG_IGN' if exit_code is None else
                           f'lambda *_: sys.exit({exit_code})')
                worker = (f'import signal,sys,time; signal.signal(signal.SIGINT, {handler}); '
                          'time.sleep(10)')
                result = subprocess.run(["bash", "-c", wrapper, "fixture", sys.executable, worker],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 137 if exit_code is None else exit_code,
                                 result.stderr)


if __name__ == "__main__":
    unittest.main()
