import copy
import os
import unittest
import tempfile
import json
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

os.environ.setdefault('BOT_TOKEN', 'test-token')
os.environ.setdefault('CHAT_IDS', '1')
os.environ.pop('GITHUB_ACTIONS', None)  # Unit tests never persist to production Git.
import bot


class RegressionTests(unittest.TestCase):
    def test_cosmetic_identity(self):
        a = dict(title='DETERMINA À', num_pub='Pubblicazione n. 123', tipo='DETERMINE', date='24-09-2026')
        b = dict(a, title='  determina   À ', tipo=' determine ')
        self.assertEqual(bot.item_id(a), bot.item_id(b))

    def test_placeholder_revision(self):
        a = dict(date='24-09-2026', register_number='', act_number='')
        b = dict(a, register_number='0', act_number='del')
        self.assertEqual(bot.revision_changes(a, b), [])
        self.assertEqual(bot.item_revision_fingerprint(a), bot.item_revision_fingerprint(b))

    def test_content_revisions(self):
        a = dict(attachment_count=1, attachment_sha256=['abc'])
        for b in [dict(a, attachment_sha256=['def']), dict(attachment_count=2, attachment_sha256=['abc', 'def']), dict(attachment_count=0, attachment_sha256=[])]:
            self.assertIn('allegati', bot.revision_changes(a, b))

    def test_parser_placeholders_and_context(self):
        html = '''<div class="cmp-card"><span class="fw-semibold">WRONG</span>
        <a onclick="MC02(12)"><h5>Determina <span>prova</span></h5></a>
        <span>Pubblicazione n. 123</span> Pubblicazione dal 24-09-2026 al 30-09-2026
        Tipo: DETERMINE Mittente: Comune Atto n. del Registro generale n. 0</div>'''
        item = bot.parse_albo_html(html)[0]
        self.assertEqual(item['act_number'], '')
        self.assertEqual(item['register_number'], '')
        self.assertEqual(item['num_pub'], '123')
        self.assertEqual(item['sender'], 'Comune')

    def test_single_malformed_card_is_skipped(self):
        html = '<div class="cmp-card"><a onclick="MC02(1)"></a></div><div class="cmp-card"><a onclick="MC02(2)"><h5>Atto valido</h5></a>Pubblicazione n. 2</div>'
        diagnostics = {}
        items = bot.parse_albo_html(html, diagnostics=diagnostics, page_number=1)
        self.assertEqual(len(items), 1)
        self.assertEqual(diagnostics['skipped_cards'], 1)

    def test_attachment_formats_are_preserved(self):
        for extension in ['pdf', 'p7m', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'zip', 'png', 'jpg', 'txt', 'csv']:
            name = 'atto.' + extension
            resolved = bot._resolved_attachment_filename(name, 'https://example.invalid/file', 'application/octet-stream', '', b'data')
            self.assertEqual(resolved, name)

    def test_real_act_numbers_and_new_publication_year(self):
        self.assertEqual(bot.normalize_number('A/12-2026'), 'a/12-2026')
        self.assertEqual(bot.normalize_number('000'), '')
        item = dict(title='Atto', num_pub='123', date='24-09-2026')
        record = dict(item, date='24-09-2025', notified=True)
        self.assertIsNone(bot.find_existing_equivalent_item(item, {bot.legacy_item_id(item): record}))
        record.pop('num_pub')  # Legacy records did not retain publication metadata.
        self.assertIsNone(bot.find_existing_equivalent_item(item, {bot.legacy_item_id(item): record}))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patchers = [patch.object(bot, name, self.root / filename) for name, filename in [
            ('DB_PATH', 'seen.json'), ('USER_SEEN_PATH', 'users.json'),
            ('TELEGRAM_UPDATES_PATH', 'updates.json'), ('ALBO_SAFETY_PATH', 'safety.json'),
            ('LAST_CHECK_PATH', 'last.txt')]]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def test_update_restart(self):
        self.assertTrue(bot.claim_telegram_update(123))
        self.assertFalse(bot.claim_telegram_update(123))
        self.assertTrue(bot.claim_telegram_update(124))
        self.assertNotIn('chat_id', bot.TELEGRAM_UPDATES_PATH.read_text())

    def test_update_claim_requires_remote_persistence(self):
        with patch.object(bot, 'git_commit_and_push', return_value=False):
            with self.assertRaises(RuntimeError):
                bot.claim_telegram_update(123)

    def test_atomic_failure_preserves_database(self):
        bot.save_db({'old': {'notified': True}}, push=False)
        original = bot.DB_PATH.read_bytes()
        with patch.object(bot.os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                bot.save_db({'new': {'notified': False}}, push=False)
        self.assertEqual(bot.DB_PATH.read_bytes(), original)
        self.assertEqual(len(bot.load_db()), 1)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_invalid_database_never_becomes_empty(self):
        bot.DB_PATH.write_text('{broken')
        with self.assertRaises(json.JSONDecodeError):
            bot.load_db()

    def test_encrypted_history_roundtrip_and_alias(self):
        from cryptography.fernet import Fernet
        with patch.object(bot, '_fernet', Fernet(Fernet.generate_key())):
            bot.save_user_seen({'123': ['accidental']}, push=False)
            self.assertNotIn(b'123', bot.USER_SEEN_PATH.read_bytes())
            bot.save_db({'original': {'notified': True, 'legacy_ids': ['accidental']}}, push=False)
            self.assertIn('original', bot.get_user_seen_hashes(123))
            self.assertEqual(bot.load_user_seen(), {'123': ['accidental']})

    def test_legacy_content_match_and_republication(self):
        snapshot = dict(date='24-09-2026', sender='Comune', attachment_count=1, attachment_sha256=['abc'])
        record = dict(notified=True, revision_snapshot=snapshot)
        item = dict(title='Atto', date='24-09-2026', num_pub='123')
        self.assertEqual(bot.find_existing_equivalent_item(item, {'old': record}, snapshot), 'old')
        self.assertIsNone(bot.find_existing_equivalent_item(item, {'a': record, 'b': record}, snapshot))
        self.assertIsNone(bot.find_existing_equivalent_item(item, {'old': dict(record, num_pub='122')}, snapshot))

    def test_no_attachment_equivalence_is_not_guessed(self):
        snapshot = dict(date='24-09-2026', sender='Comune', attachment_count=0)
        self.assertIsNone(bot.find_existing_equivalent_item({'title': 'Atto'}, {'old': {'revision_snapshot': snapshot}}, snapshot))

    def test_search_accents_pages_and_resend_ownership(self):
        records = {str(i): dict(title='Città determina') for i in range(12)}
        found = bot.search_records(records, 'CITTA')
        self.assertEqual(len(found), 12)
        text, keyboard = bot.render_search_page(found, 0, 'test')
        self.assertEqual(text.count('Città'), 5)
        self.assertEqual(keyboard.inline_keyboard[0][0].text, 'Avanti')
        token = bot.store_pending_resend(123, ['a'])
        self.assertIsNone(bot.pop_pending_resend(token, 456))
        self.assertEqual(bot.pop_pending_resend(token, 123), ['a'])
        self.assertIsNone(bot.pop_pending_resend(token, 123))


class CycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = StateTests()
        self.state.setUp()
        self.bot = AsyncMock()

    async def asyncTearDown(self):
        self.state.tearDown()

    def item(self, number=1):
        return dict(title=f'Atto {number}', num_pub=str(number), tipo='Determina', sender='Comune',
                    date=datetime.now(timezone.utc).strftime('%d-%m-%Y'), date_end='',
                    allegati=[dict(filename='a.pdf', sha256='abc')],
                    _detail_captured=True, _attachment_fetch_ok=True, _attachment_expected_count=1)

    async def run_cycle(self, items, **extra):
        async def fetch(**kwargs):
            kwargs['detail_selector'](items)
            return items
        with patch.object(bot, 'fetch_albo_html', side_effect=fetch), \
             patch.object(bot, 'touch_last_check'), \
             patch.object(bot, 'get_all_recipients', return_value={1}), \
             patch.object(bot, 'notify', new_callable=AsyncMock, return_value=({1}, set())) as notify, \
             patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock):
            result = await bot.run_check(self.bot)
        return result, notify

    async def test_fuse_102_of_122_no_notifications(self):
        items = [self.item(i) for i in range(122)]
        bot.save_db({bot.item_id(i): {'notified': True} for i in items[:20]}, push=False)
        result, notify = await self.run_cycle(items)
        self.assertFalse(result['ok'])
        notify.assert_not_called()
        self.assertEqual(self.bot.send_message.await_count, 1)
        await self.run_cycle(items)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_first_v2_cycle_is_silent(self):
        result, notify = await self.run_cycle([self.item()])
        notify.assert_not_called()
        self.assertEqual(result['new'], 0)
        self.assertTrue(next(iter(bot.load_db().values()))['notified'])

    async def test_recent_new_is_delivered_after_migration(self):
        bot.ALBO_SAFETY_PATH.write_text('{"identity_v2_ready": true}')
        result, notify = await self.run_cycle([self.item()])
        notify.assert_awaited_once()
        self.assertEqual(result['new'], 1)
        result, notify = await self.run_cycle([self.item()])
        notify.assert_not_called()

    async def test_old_unknown_is_silent(self):
        bot.ALBO_SAFETY_PATH.write_text('{"identity_v2_ready": true}')
        item = dict(self.item(), date='01-01-2015')
        result, notify = await self.run_cycle([item])
        notify.assert_not_called()
        self.assertEqual(result['new'], 0)

    async def test_semantic_revision(self):
        item = self.item()
        snapshot = bot.item_revision_snapshot(item)
        snapshot['attachment_sha256'] = ['old']
        bot.save_db({bot.item_id(item): dict(notified=True, revision=1,
                    revision_snapshot=snapshot, revision_fingerprint=bot.item_revision_fingerprint(snapshot))}, push=False)
        result, notify = await self.run_cycle([item])
        self.assertEqual(result['updated'], 1)
        self.assertEqual(notify.call_args.args[1]['_notification_kind'], 'revision')

    async def test_old_fingerprint_cosmetic_change_suppressed(self):
        item = dict(self.item(), register_number='0')
        snapshot = bot.item_revision_snapshot(dict(item, register_number=''))
        bot.save_db({bot.item_id(item): dict(notified=True, revision=1,
                    revision_snapshot=snapshot, revision_fingerprint='legacy-unnormalized')}, push=False)
        result, notify = await self.run_cycle([item])
        notify.assert_not_called()
        self.assertEqual(result['updated'], 0)
        self.assertEqual(next(iter(bot.load_db().values()))['revision'], 1)

    async def test_spool_cleanup_on_cancellation(self):
        path = bot._ATTACHMENT_TEMP_DIR / 'test-cancel.txt'
        path.write_bytes(b'hello')
        @bot.cleanup_spool_scope
        async def interrupted():
            bot._AttachmentSpool(path, 0, '')
            raise bot.asyncio.CancelledError()
        with self.assertRaises(bot.asyncio.CancelledError):
            await interrupted()
        self.assertFalse(path.exists())

    async def test_callback_update_replay_is_ignored_but_downtime_update_runs(self):
        from types import SimpleNamespace
        stop = bot.asyncio.Event()
        bot.claim_telegram_update(123)
        app = AsyncMock()
        app.bot.get_updates.return_value = [SimpleNamespace(update_id=i, effective_message=None, callback_query=True) for i in [123, 124]]
        async def process(update):
            self.assertEqual(update.update_id, 124)
            stop.set()
        app.process_update.side_effect = process
        await bot.telegram_polling(app, stop)
        app.process_update.assert_awaited_once()
        app.bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        self.assertEqual(bot.load_telegram_updates()['last_processed_update_id'], 124)

    async def test_search_callback_cannot_cross_chats(self):
        from types import SimpleNamespace
        bot._search_sessions['test'] = dict(chat_id=1, expires=10**12, records=[])
        query = AsyncMock(); query.data = 'search:test:0'
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=2))
        await bot.cmd_search_page(update, None)
        query.edit_message_text.assert_not_called()
        self.assertTrue(query.answer.call_args.kwargs['show_alert'])
        bot._search_sessions.clear()

    async def test_sigterm_stops_workers_and_flushes_state(self):
        from unittest.mock import Mock
        app = AsyncMock()
        app.add_handler = Mock()
        app.add_error_handler = Mock()
        builder = Mock()
        builder.token.return_value = builder
        builder.request.return_value = builder
        builder.build.return_value = app
        stopped = []
        async def worker(*args):
            try:
                await bot.asyncio.Event().wait()
            finally:
                stopped.append(True)
        def register(sig, callback):
            if sig == bot.signal.SIGTERM:
                bot.asyncio.get_running_loop().call_soon(callback, sig, None)
        with patch.object(bot.Application, 'builder', return_value=builder), \
             patch('telegram.request.HTTPXRequest'), \
             patch.object(bot.signal, 'signal', side_effect=register), \
             patch.object(bot, 'polling_loop', side_effect=worker), \
             patch.object(bot, 'telegram_polling', side_effect=worker), \
             patch.object(bot, 'git_commit_and_push', return_value=True) as flush:
            await bot.main()
        app.stop.assert_awaited_once()
        self.assertEqual(len(stopped), 2)
        flush.assert_called_once()


if __name__ == '__main__':
    unittest.main()
