"""Admin access, honest counters and read-only runtime diagnostics."""
import asyncio
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('BOT_TOKEN', 'test-token')
os.environ.setdefault('CHAT_IDS', '1')
os.environ.pop('GITHUB_ACTIONS', None)
import bot


def update(chat=1, user=1, kind='private'):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat, type=kind),
        effective_user=SimpleNamespace(id=user) if user is not None else None,
        message=SimpleNamespace(reply_text=AsyncMock()))


class AdminStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ('DB_PATH', 'NEWS_DB_PATH', 'SUBSCRIBERS_PATH',
                     'SUBSCRIBERS_NEWS_PATH', 'ALBO_SAFETY_PATH', 'LAST_CHECK_PATH'):
            self.stack.enter_context(patch.object(bot, name, self.root / name))
        self.stack.enter_context(patch.dict(bot.CONFIG, ADMIN_IDS=[1, 2]))
        self.stack.enter_context(patch.object(bot, '_fernet', None))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.push = self.stack.enter_context(patch.object(bot, 'git_commit_and_push'))
        self.write(bot.DB_PATH, {'old': {'notified': True},
                                'retry': {'notified': False, 'delivery_pending': True},
                                'cache': {'notified': False}})
        self.write(bot.NEWS_DB_PATH, {'old': {}, 'retry': {'delivery_pending': True}})
        self.write(bot.SUBSCRIBERS_PATH, [1, 30, 40])
        self.write(bot.SUBSCRIBERS_NEWS_PATH, [40, 50])
        self.write(bot.ALBO_SAFETY_PATH, {'identity_v2_ready': True})

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    async def test_denied_before_reading_data_or_forcing_checks(self):
        with patch.object(bot, 'build_admin_status') as report, \
                patch.object(bot, 'run_check', new_callable=AsyncMock) as check:
            for request in (update(99, 99), update(-10, 1, 'supergroup'),
                            update(1, 99), update(1, None)):
                for handler in (bot.cmd_status, bot.cmd_controlla):
                    await handler(request, SimpleNamespace(bot=None))
                    self.assertIn('riservato', request.message.reply_text.call_args.args[0])
            report.assert_not_called()
            check.assert_not_awaited()

    async def test_private_admin_gets_aggregates_without_writes_or_ids(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        request = update()
        await bot.cmd_status(request, SimpleNamespace(bot=None))
        text = request.message.reply_text.call_args.args[0]
        for expected in ('Albo: 3', 'News: 2', 'Chat iscritte uniche: 4',
                         'Entrambe: 1', 'Destinatari Albo: 4', 'Destinatari News: 4',
                         'Destinatari unici: 5', 'Atti conservati: 3',
                         'Notificati/baseline: 1', 'Atti con consegne pendenti: 1',
                         'News con consegne pendenti: 1'):
            self.assertIn(expected, text)
        self.assertLess(len(text), 4096)
        self.assertNotIn('chat_id', text)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.push.assert_not_called()

    def test_legacy_archive_is_not_migrated_by_report(self):
        self.write(bot.DB_PATH, ['old', 'older'])
        original = bot.DB_PATH.read_bytes()
        self.assertIn('Atti conservati: 2', bot.build_admin_status())
        self.assertEqual(bot.DB_PATH.read_bytes(), original)
        self.push.assert_not_called()

    def test_absent_subscriptions_do_not_count_admins_as_subscribers(self):
        bot.SUBSCRIBERS_PATH.unlink()
        bot.SUBSCRIBERS_NEWS_PATH.unlink()
        text = bot.build_admin_status()
        self.assertIn('Chat iscritte uniche: 0', text)
        self.assertIn('Destinatari Albo: 2', text)
        self.assertIn('Destinatari News: 2', text)

    def test_encrypted_subscriptions_are_counted_without_disclosure(self):
        from cryptography.fernet import Fernet
        with patch.object(bot, '_fernet', Fernet(Fernet.generate_key())):
            bot.SUBSCRIBERS_PATH.write_bytes(bot._encrypt_json([123456789, 40]))
            bot.SUBSCRIBERS_NEWS_PATH.write_bytes(bot._encrypt_json([40]))
            text = bot.build_admin_status()
        self.assertIn('Chat iscritte uniche: 2', text)
        self.assertNotIn('123456789', text)

    def test_unreadable_personal_state_is_not_reported_as_zero(self):
        bot.SUBSCRIBERS_PATH.write_bytes(b'gAAAAA-unreadable')
        text = bot.build_admin_status()
        self.assertIn('Iscrizioni: dati non disponibili', text)
        self.assertNotIn('Chat iscritte uniche: 0', text)
        self.assertIn('Atti conservati: 3', text)
        self.assertNotIn('gAAAAA', text)

    def test_corrupt_archive_does_not_hide_other_diagnostics(self):
        bot.DB_PATH.write_text('{broken')
        self.write(bot.ALBO_SAFETY_PATH, {'last_stop': 'sensitive-url-token', 'identity_v2_ready': True})
        text = bot.build_admin_status()
        self.assertIn('Archivio Albo: dati non disponibili', text)
        self.assertIn('News conservate: 2', text)
        self.assertIn('Blocco Albo registrato: sì', text)
        self.assertNotIn('sensitive-url-token', text)

    def test_old_timestamp_is_only_an_attempt_not_proof_of_health(self):
        bot.LAST_CHECK_PATH.write_text('2026-09-29T09:00:00Z')
        from admin_status import CheckHealth
        with patch.object(bot, '_CHECK_HEALTH', CheckHealth()):
            text = bot.build_admin_status()
        self.assertIn('Ultimo tentativo Albo salvato', text)
        self.assertIn('non ancora verificato', text)
        self.assertIn('Ultimo successo: non disponibile', text)

    async def test_check_wrappers_preserve_results_and_record_failures(self):
        from admin_status import CheckHealth
        with patch.object(bot, '_CHECK_HEALTH', CheckHealth()):
            for name, wrapped, lock in (('Albo', '_run_check_albo', '_ALBO_CHECK_LOCK'),
                                       ('News', '_run_check_news', '_NEWS_CHECK_LOCK')):
                result = {'ok': False, 'new': 2, 'failed': 3, 'total': 7}
                handler = bot.run_check if name == 'Albo' else bot.run_check_news
                with patch.object(bot, wrapped, new=AsyncMock(return_value=result)), \
                        patch.object(bot, lock, asyncio.Lock()):
                    self.assertIs(await handler(None), result)
                text = bot.build_admin_status()
                self.assertIn(f'{name}: incompleto o bloccato', text)
                self.assertIn('Consegne fallite nel ciclo: 3', text)


class CheckHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_success_failure_and_previous_success(self):
        from admin_status import CheckHealth
        monitor = CheckHealth()
        entered, release = asyncio.Event(), asyncio.Event()

        async def check(_):
            entered.set()
            await release.wait()
            return {'ok': True, 'new': 1}

        task = asyncio.create_task(monitor.run('Albo', check, None))
        await entered.wait()
        self.assertIn('Albo: in corso', monitor.lines())
        release.set()
        self.assertEqual(await task, {'ok': True, 'new': 1})
        self.assertIn('Albo: completato', monitor.lines())
        success = monitor.checks['Albo']['last_success']
        await monitor.run('Albo', AsyncMock(return_value={'ok': False}), None)
        self.assertEqual(monitor.checks['Albo']['last_success'], success)
        self.assertIn('Albo: incompleto o bloccato', monitor.lines())

    async def test_exceptions_and_cancellation_propagate_without_details(self):
        from admin_status import CheckHealth
        for error, label in ((RuntimeError('secret-token'), 'errore'),
                             (asyncio.CancelledError(), 'interrotto')):
            monitor = CheckHealth()
            with self.assertRaises(type(error)):
                await monitor.run('News', AsyncMock(side_effect=error), None)
            self.assertIn(f'News: {label}', monitor.lines())
            self.assertNotIn('secret-token', monitor.lines())
            self.assertIsNone(monitor.checks['News']['last_success'])


if __name__ == '__main__':
    unittest.main()
