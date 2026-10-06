import asyncio
import os
import signal
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault('BOT_TOKEN', 'test-token')
os.environ.setdefault('CHAT_IDS', '1')
os.environ.pop('GITHUB_ACTIONS', None)
import bot


class ShutdownCheckpointTests(IsolatedAsyncioTestCase):
    async def exercise(self, *, checkpoint=True, unresponsive=False, unexpected_exit=False,
                       drain_error=False):
        app = AsyncMock()
        app.add_handler = Mock()
        app.add_error_handler = Mock()
        builder = Mock()
        builder.token.return_value = builder
        builder.request.return_value = builder
        builder.build.return_value = app
        handlers, events = {}, []

        async def worker(_app, stop):
            if unexpected_exit:
                return
            asyncio.get_running_loop().call_soon(handlers[signal.SIGTERM], signal.SIGTERM, None)
            try:
                await stop.wait()
                if unresponsive:
                    await asyncio.Event().wait()
                await asyncio.sleep(.01)
                if drain_error:
                    raise RuntimeError('fixture shutdown failure')
                events.append('receipt_saved')
            except asyncio.CancelledError:
                events.append('cancelled')
                raise

        def flush(**_kwargs):
            events.append('final_flush')
            return checkpoint

        with patch.object(bot, '_SHUTDOWN_REQUESTED', False), \
                patch.object(bot, '_SHUTDOWN_CHECKPOINT_DEADLINE', None), \
                patch.object(bot, '_SHUTDOWN_GIT_DEADLINE', None), \
                patch.object(bot, '_SHUTDOWN_DRAIN_SECONDS', .05), \
                patch.object(bot.Application, 'builder', return_value=builder), \
                patch('telegram.request.HTTPXRequest'), \
                patch.object(bot.signal, 'signal', side_effect=lambda sig, fn: handlers.update({sig: fn})), \
                patch.object(bot, 'polling_loop', side_effect=worker), \
                patch.object(bot, 'telegram_polling', side_effect=worker), \
                patch.object(bot, 'git_commit_and_push', side_effect=flush):
            error = None
            try:
                await bot.main()
            except RuntimeError as exc:
                error = str(exc)
        return events, error

    async def test_signal_allows_completed_deliveries_to_checkpoint_before_final_flush(self):
        events, error = await self.exercise()
        self.assertIsNone(error)
        self.assertEqual(events, ['receipt_saved', 'receipt_saved', 'final_flush'])

    async def test_unresponsive_worker_is_cancelled_within_drain_budget(self):
        events, error = await self.exercise(unresponsive=True)
        self.assertIsNone(error)
        self.assertEqual(events, ['cancelled', 'cancelled', 'final_flush'])

    async def test_failed_final_flush_is_a_failure_instead_of_green_exit(self):
        events, error = await self.exercise(checkpoint=False)
        self.assertIn('Flush Git finale fallito', error)
        self.assertEqual(events[-1], 'final_flush')

    async def test_unexpected_worker_exit_is_a_failure_instead_of_green_exit(self):
        events, error = await self.exercise(unexpected_exit=True)
        self.assertIn('terminato senza richiesta', error)
        self.assertEqual(events, ['final_flush'])

    async def test_worker_error_during_drain_is_not_discarded_after_successful_flush(self):
        events, error = await self.exercise(drain_error=True)
        self.assertIn('fixture shutdown failure', error)
        self.assertEqual(events, ['final_flush'])

    async def test_repeated_stop_signals_do_not_extend_shared_git_budgets(self):
        with patch.object(bot, '_SHUTDOWN_REQUESTED', False), \
                patch.object(bot, '_SHUTDOWN_CHECKPOINT_DEADLINE', None), \
                patch.object(bot, '_SHUTDOWN_GIT_DEADLINE', None), \
                patch.object(bot.time, 'monotonic', side_effect=[100, 130]):
            bot._mark_shutdown_requested()
            bot._mark_shutdown_requested()
            self.assertEqual(bot._SHUTDOWN_CHECKPOINT_DEADLINE, 140)
            self.assertEqual(bot._SHUTDOWN_GIT_DEADLINE, 160)
