"""Application-level time policy, independent manual commands and real signals."""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault('BOT_TOKEN', 'test-token')
os.environ.setdefault('CHAT_IDS', '1')
os.environ.pop('GITHUB_ACTIONS', None)
import bot
from runtime_policy import RuntimePolicy


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(bot, 'RUNTIME_POLICY', RuntimePolicy.from_environment({})))
        self.now = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
        self.stack.enter_context(patch.object(bot, '_runtime_now', side_effect=lambda: self.now))
        self.mocks = {}
        for name in ('_ensure_albo_baseline', '_ensure_news_baseline',
                     'run_check', 'run_check_news', 'send_heartbeat'):
            value = True if name.startswith('_ensure') else {'ok': True, 'new': 0, 'total': 1}
            self.mocks[name] = self.stack.enter_context(patch.object(bot, name, new=AsyncMock(return_value=value)))
        for name in ('load_seen', 'load_seen_news'):
            self.stack.enter_context(patch.object(bot, name, return_value={'known'}))

    async def test_night_suppresses_checks_and_baseline_without_stopping_telegram(self):
        self.now = datetime(2026, 10, 1, 1, tzinfo=timezone.utc)
        await bot._automatic_cycle(None)
        for mock in self.mocks.values():
            mock.assert_not_awaited()

    async def test_day_checks_both_sources_and_reports_their_results(self):
        await bot._automatic_cycle(None)
        for mock in self.mocks.values():
            mock.assert_awaited_once()
        self.assertTrue(self.mocks['send_heartbeat'].call_args.kwargs['albo_ok'])

    async def test_crossing_night_finishes_albo_but_does_not_start_news(self):
        self.now = datetime(2026, 10, 1, 20, 59, tzinfo=timezone.utc)
        async def finish_albo(_, **kwargs):
            self.now += timedelta(minutes=2)
            return {'ok': True, 'new': 0}
        self.mocks['run_check'].side_effect = finish_albo
        await bot._automatic_cycle(None)
        self.mocks['run_check'].assert_awaited_once()
        self.mocks['_ensure_news_baseline'].assert_not_awaited()
        self.mocks['run_check_news'].assert_not_awaited()
        self.mocks['send_heartbeat'].assert_not_awaited()

    async def test_bootstrap_crossing_night_does_not_admit_a_following_check(self):
        self.now = datetime(2026, 10, 1, 20, 59, tzinfo=timezone.utc)
        async def bootstrap():
            self.now += timedelta(minutes=2)
            return True
        self.mocks['_ensure_albo_baseline'].side_effect = bootstrap
        await bot._automatic_cycle(None)
        self.mocks['run_check'].assert_not_awaited()
        self.mocks['_ensure_news_baseline'].assert_not_awaited()

    async def test_source_failure_does_not_suppress_other_source(self):
        self.mocks['run_check'].side_effect = RuntimeError('fixture failure')
        await bot._automatic_cycle(None)
        self.mocks['run_check_news'].assert_awaited_once()
        self.assertFalse(self.mocks['send_heartbeat'].call_args.kwargs['albo_ok'])
        self.assertTrue(self.mocks['send_heartbeat'].call_args.kwargs['news_ok'])

    async def test_private_manual_check_is_available_at_night(self):
        self.now = datetime(2026, 10, 1, 1, tzinfo=timezone.utc)
        request = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1, type='private'),
            effective_user=SimpleNamespace(id=1), message=SimpleNamespace(reply_text=AsyncMock()))
        with patch.dict(bot.CONFIG, ADMIN_IDS=[1]):
            await bot.cmd_controlla(request, SimpleNamespace(bot=None))
        self.mocks['run_check'].assert_awaited_once()
        self.mocks['run_check_news'].assert_awaited_once()

    async def test_same_worker_resumes_at_seven_without_a_new_github_trigger(self):
        await self.drive_loop(datetime(2026, 10, 1, 4, 59, tzinfo=timezone.utc), 17 * 60,
                              [60, 960])

    async def test_same_worker_stops_admitting_cycles_at_twenty_three(self):
        await self.drive_loop(datetime(2026, 10, 1, 20, 59, tzinfo=timezone.utc), 32 * 60,
                              [0])

    async def drive_loop(self, start, horizon, expected):
        elapsed = 0.0
        calls = []
        async def sleep(seconds):
            nonlocal elapsed
            self.assertGreater(seconds, 0)
            self.assertLessEqual(seconds, 60)
            elapsed += seconds
            self.now = start + timedelta(seconds=elapsed)
            if elapsed >= horizon:
                raise asyncio.CancelledError()
        async def cycle(_):
            calls.append(elapsed)
        self.now = start
        with patch.object(bot, 'time', SimpleNamespace(monotonic=lambda: elapsed)), \
                patch.object(bot.asyncio, 'sleep', side_effect=sleep), \
                patch.object(bot, '_automatic_cycle', side_effect=cycle):
            with self.assertRaises(asyncio.CancelledError):
                await bot.polling_loop(SimpleNamespace(bot=None))
        self.assertEqual(calls, expected)


@unittest.skipIf(os.name == 'nt', 'Real POSIX signal delivery is verified on Linux CI')
class ProcessSignalTests(unittest.TestCase):
    def test_sigint_and_sigterm_cancel_workers_and_flush_without_network(self):
        script = r'''
import asyncio, os, signal, sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import bot
app = AsyncMock()
app.add_handler = Mock()
app.add_error_handler = Mock()
builder = Mock()
builder.token.return_value = builder
builder.request.return_value = builder
builder.build.return_value = app
stopped = []
async def worker(*args):
    if not getattr(worker, 'scheduled', False):
        worker.scheduled = True
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), int(sys.argv[1]))
    try:
        await asyncio.Event().wait()
    finally:
        stopped.append(True)
def flush():
    Path(os.environ['BOT_STATE_DIR'], 'flushed').write_text(str(len(stopped)))
    return True
with patch.object(bot.Application, 'builder', return_value=builder), \
     patch('telegram.request.HTTPXRequest'), \
     patch.object(bot, 'polling_loop', side_effect=worker), \
     patch.object(bot, 'telegram_polling', side_effect=worker), \
     patch.object(bot, 'git_commit_and_push', side_effect=flush):
    asyncio.run(bot.main())
assert len(stopped) == 2
'''
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as root:
                env = dict(os.environ, BOT_TOKEN='test-token', CHAT_IDS='1', BOT_STATE_DIR=root)
                env.pop('GITHUB_ACTIONS', None)
                env.pop('STATE_ENCRYPTION_KEY', None)
                result = subprocess.run([sys.executable, '-c', script, str(int(sig))],
                                        env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(Path(root, 'flushed').read_text(), '2')


class AutomaticQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_for_manual_check_revalidates_policy_after_source_lock(self):
        for wrapper, inner, lock_name in (
            (bot.run_check, '_run_check_albo', '_ALBO_CHECK_LOCK'),
            (bot.run_check_news, '_run_check_news', '_NEWS_CHECK_LOCK'),
        ):
            with self.subTest(source=inner):
                now = datetime(2026, 10, 1, 20, 59, tzinfo=timezone.utc)
                lock = asyncio.Lock()
                entered = asyncio.Event()
                async def queued():
                    entered.set()
                    return await wrapper(None, automatic=True)
                with patch.object(bot, lock_name, lock), \
                        patch.object(bot, 'RUNTIME_POLICY', RuntimePolicy.from_environment({})), \
                        patch.object(bot, '_runtime_now', side_effect=lambda: now), \
                        patch.object(bot, inner, new=AsyncMock(return_value={'ok': True})) as check:
                    async with lock:
                        task = asyncio.create_task(queued())
                        await entered.wait()
                        now += timedelta(minutes=2)
                    result = await task
                    self.assertTrue(result['skipped'])
                    check.assert_not_awaited()
                    # A direct user request remains allowed after the boundary.
                    await wrapper(None)
                    check.assert_awaited_once()
