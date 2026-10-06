"""Exercise real fanout across cancellation, persistence failures and revisions."""

import asyncio
import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault('BOT_TOKEN', 'test-token')
os.environ.setdefault('CHAT_IDS', '1')
os.environ.pop('GITHUB_ACTIONS', None)

import bot


class RecipientCheckpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.paths = []
        for name in ('DB_PATH', 'NEWS_DB_PATH', 'USER_SEEN_PATH', 'USER_SEEN_NEWS_PATH',
                     'ALBO_SAFETY_PATH', 'LAST_CHECK_PATH', 'SUBSCRIBERS_PATH',
                     'SUBSCRIBERS_NEWS_PATH', 'TELEGRAM_UPDATES_PATH'):
            path = self.root / name
            self.paths.append(path)
            self.stack.enter_context(patch.object(bot, name, path))
        self.stack.enter_context(patch.object(bot, '_fernet', None))
        self.stack.enter_context(patch.object(bot, 'shutdown_requested', return_value=False))
        self.stack.enter_context(patch.object(bot, '_revision_check_due', return_value=True))
        self.stack.enter_context(patch.object(bot, 'get_all_recipients', return_value={1, 2}))
        self.stack.enter_context(patch.object(bot, 'get_all_news_recipients', return_value={1, 2}))
        self.sleep = self.stack.enter_context(patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock))
        self.stack.enter_context(patch.object(bot.httpx, 'AsyncClient',
                                              side_effect=AssertionError('Unexpected network')))
        self.send_albo = self.stack.enter_context(patch.object(
            bot, 'send_item_to_chat', new_callable=AsyncMock, return_value=True))
        self.send_news = self.stack.enter_context(patch.object(
            bot, 'send_news_to_chat', new_callable=AsyncMock, return_value=True))
        self.fetch_albo = self.stack.enter_context(patch.object(
            bot, 'fetch_albo_html', new_callable=AsyncMock, side_effect=self.albo_listing))
        self.fetch_news = self.stack.enter_context(patch.object(
            bot, 'fetch_news_html', new_callable=AsyncMock))
        self.remote = {}
        self.push = self.stack.enter_context(patch.object(
            bot, 'git_commit_and_push', side_effect=self.persist))
        self.telegram = AsyncMock()
        today = datetime.now(timezone.utc)
        self.item = dict(title='Atto di prova', num_pub='123', tipo='DETERMINE',
                         date=today.strftime('%d-%m-%Y'),
                         date_end=(today + timedelta(days=30)).strftime('%d-%m-%Y'),
                         sender='Comune', act_number='12', register_number='57',
                         num_riga='901', _detail_captured=True, _attachment_fetch_ok=True,
                         _attachment_expected_count=1,
                         allegati=[dict(filename='document.pdf', sha256='version-A')])
        self.key = bot.item_id(self.item)
        self.news = dict(id='news-1', title='News di prova', date=today.strftime('%d-%m-%Y'),
                         category='Avviso', description='Descrizione',
                         url='https://example.invalid/news-1')
        self.fetch_news.return_value = [self.news]
        bot.save_db({}, push=False)
        bot.save_news_db({}, push=False)
        bot.save_user_seen({}, push=False)
        bot.save_user_seen_news({}, push=False)
        bot.ALBO_SAFETY_PATH.write_text('{"identity_v2_ready": true}', encoding='utf-8')
        self.persist()

    def persist(self, paths=None, **kwargs):
        for path in map(Path, paths or self.paths):
            if path.exists():
                self.remote[path.name] = path.read_bytes()
        return True

    def remote_json(self, path):
        return json.loads(self.remote[path.name])

    def restart(self):
        for name, content in self.remote.items():
            (self.root / name).write_bytes(content)
        self.send_albo.reset_mock()
        self.send_news.reset_mock()
        self.send_albo.side_effect = self.send_news.side_effect = None
        self.send_albo.return_value = self.send_news.return_value = True
        self.sleep.side_effect = None

    async def albo_listing(self, *, detail_selector, **kwargs):
        items = [copy.deepcopy(self.item)]
        detail_selector(items)
        return items

    def known_albo(self):
        snapshot = bot.item_revision_snapshot(self.item)
        record = dict(notified=True, revision=1, revision_snapshot=snapshot,
                      revision_fingerprint=bot.item_revision_fingerprint(snapshot))
        bot.remember_identity(record, self.item)
        bot.save_db({self.key: record}, push=False)
        bot.save_user_seen({'1': [self.key], '2': [self.key]}, push=False)
        self.persist()

    def version(self, version):
        self.item['allegati'][0]['sha256'] = f'version-{version}'

    async def test_albo_cancel_during_second_recipient_keeps_first_receipt(self):
        self.send_albo.side_effect = [True, asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await bot._run_check_albo(self.telegram)
        self.assertEqual(self.remote_json(bot.USER_SEEN_PATH), {'1': [self.key]})
        record = self.remote_json(bot.DB_PATH)[self.key]
        self.assertTrue(record['delivery_pending'])
        self.assertTrue(record['notified'])
        self.restart()
        self.assertTrue((await bot._run_check_albo(self.telegram))['ok'])
        self.assertEqual([call.args[1] for call in self.send_albo.await_args_list], [2])
        record = self.remote_json(bot.DB_PATH)[self.key]
        self.assertFalse(record['delivery_pending'])
        self.assertFalse(any(key.startswith('pending_') for key in record))

    async def test_news_cancel_during_second_recipient_keeps_first_receipt(self):
        self.send_news.side_effect = [True, asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await bot._run_check_news(self.telegram)
        self.assertEqual(self.remote_json(bot.USER_SEEN_NEWS_PATH), {'1': ['news-1']})
        self.assertTrue(self.remote_json(bot.NEWS_DB_PATH)['news-1']['delivery_pending'])
        self.restart()
        self.fetch_news.return_value = []
        self.assertTrue((await bot._run_check_news(self.telegram))['ok'])
        self.assertEqual([call.args[1] for call in self.send_news.await_args_list], [2])

    async def test_receipt_is_durable_before_flood_pause_for_both_channels(self):
        for cycle, send, path, key in (
            (bot._run_check_albo, self.send_albo, bot.USER_SEEN_PATH, self.key),
            (bot._run_check_news, self.send_news, bot.USER_SEEN_NEWS_PATH, 'news-1'),
        ):
            with self.subTest(channel=cycle.__name__):
                self.sleep.side_effect = asyncio.CancelledError()
                with self.assertRaises(asyncio.CancelledError):
                    await cycle(self.telegram)
                self.assertEqual(self.remote_json(path), {'1': [key]})
                send.assert_awaited_once()
                self.sleep.side_effect = None

    async def test_failed_intent_checkpoint_prevents_any_send(self):
        for cycle, send in ((bot._run_check_albo, self.send_albo),
                            (bot._run_check_news, self.send_news)):
            with self.subTest(channel=cycle.__name__):
                def fail_intent(paths=None, **kwargs):
                    if kwargs.get('message', '').startswith('checkpoint consegna'):
                        return False
                    return self.persist(paths, **kwargs)

                self.push.side_effect = fail_intent
                with self.assertRaisesRegex(RuntimeError, 'Checkpoint consegna'):
                    await cycle(self.telegram)
                send.assert_not_awaited()

    async def test_receipt_push_failure_stops_fanout_and_local_retry_deduplicates(self):
        for cycle, send, path, key in (
            (bot._run_check_albo, self.send_albo, bot.USER_SEEN_PATH, self.key),
            (bot._run_check_news, self.send_news, bot.USER_SEEN_NEWS_PATH, 'news-1'),
        ):
            with self.subTest(channel=cycle.__name__):
                calls = 0

                def fail_receipt(paths=None, **kwargs):
                    nonlocal calls
                    if kwargs.get('message', '').startswith('checkpoint consegna'):
                        calls += 1
                        if calls == 2:
                            return False
                    return self.persist(paths, **kwargs)

                self.push.side_effect = fail_receipt
                with self.assertRaisesRegex(RuntimeError, 'Checkpoint consegna'):
                    await cycle(self.telegram)
                self.assertEqual(send.await_count, 1)
                self.assertEqual(json.loads(path.read_text()), {'1': [key]})
                self.assertEqual(self.remote_json(path), {})
                self.push.side_effect = self.persist
                send.reset_mock()
                self.assertTrue((await cycle(self.telegram))['ok'])
                self.assertEqual([call.args[1] for call in send.await_args_list], [2])

    async def test_all_failed_initial_albo_retries_after_age_limit(self):
        self.send_albo.return_value = False
        self.assertFalse((await bot._run_check_albo(self.telegram))['ok'])
        record = self.remote_json(bot.DB_PATH)[self.key]
        self.assertFalse(record['notified'])
        self.assertTrue(record['delivery_pending'])
        self.restart()
        with patch.object(bot, '_age', return_value=bot.ALBO_NOTIFY_MAX_AGE_DAYS + 1):
            result = await bot._run_check_albo(self.telegram)
        self.assertTrue(result['ok'])
        self.assertEqual([call.args[1] for call in self.send_albo.await_args_list], [1, 2])
        self.assertFalse(self.remote_json(bot.DB_PATH)[self.key]['delivery_pending'])

    async def test_each_return_to_earlier_content_gets_a_new_persisted_epoch(self):
        self.known_albo()
        for epoch, version in enumerate(('B', 'A', 'B'), start=1):
            self.version(version)
            self.send_albo.reset_mock()
            result = await bot._run_check_albo(self.telegram)
            self.assertTrue(result['ok'])
            self.assertEqual(result['updated'], 1)
            self.assertEqual([call.args[1] for call in self.send_albo.await_args_list], [1, 2])
            record = self.remote_json(bot.DB_PATH)[self.key]
            self.assertEqual(record['delivery_epoch'], epoch)
            self.assertFalse(any(key.startswith('pending_') for key in record))
            expected = bot.revision_delivery_key(self.key, record['revision_fingerprint'], epoch)
            self.assertIn(expected, self.remote_json(bot.USER_SEEN_PATH)['1'])
            self.restart()
        self.assertEqual(len(self.remote_json(bot.USER_SEEN_PATH)['1']), 4)

    async def test_retry_reuses_revision_epoch_and_skips_successful_recipient(self):
        self.known_albo()
        self.version('B')
        self.send_albo.side_effect = [True, False]
        self.assertFalse((await bot._run_check_albo(self.telegram))['ok'])
        pending = self.remote_json(bot.DB_PATH)[self.key]
        self.assertEqual(pending['delivery_epoch'], 1)
        pending_key = pending['pending_delivery_key']
        self.restart()
        self.assertTrue((await bot._run_check_albo(self.telegram))['ok'])
        self.assertEqual([call.args[1] for call in self.send_albo.await_args_list], [2])
        self.assertEqual(self.remote_json(bot.DB_PATH)[self.key]['delivery_epoch'], 1)
        for receipt in self.remote_json(bot.USER_SEEN_PATH).values():
            self.assertIn(pending_key, receipt)

    async def test_legacy_pending_revision_keeps_existing_receipts_after_upgrade(self):
        self.known_albo()
        self.version('B')
        snapshot = bot.item_revision_snapshot(self.item)
        fingerprint = bot.item_revision_fingerprint(snapshot)
        legacy_key = bot.revision_delivery_key(self.key, fingerprint)
        database = bot.load_db()
        database[self.key].update(delivery_pending=True, pending_kind='revision',
                                  pending_version=fingerprint, pending_revision_changes=['allegati'])
        bot.save_db(database, push=False)
        bot.save_user_seen({'1': [self.key, legacy_key], '2': [self.key]}, push=False)
        self.persist()
        self.assertTrue((await bot._run_check_albo(self.telegram))['ok'])
        self.assertEqual([call.args[1] for call in self.send_albo.await_args_list], [2])
        self.assertIn(legacy_key, self.remote_json(bot.USER_SEEN_PATH)['2'])
        self.restart()
        self.version('A')
        self.assertTrue((await bot._run_check_albo(self.telegram))['ok'])
        self.assertEqual([call.args[1] for call in self.send_albo.await_args_list], [1, 2])
        self.assertEqual(self.remote_json(bot.DB_PATH)[self.key]['delivery_epoch'], 1)

    async def test_cooperative_stop_checkpoints_first_and_leaves_second_pending(self):
        for cycle, send, history_path, database_path, key in (
            (bot._run_check_albo, self.send_albo, bot.USER_SEEN_PATH, bot.DB_PATH, self.key),
            (bot._run_check_news, self.send_news, bot.USER_SEEN_NEWS_PATH, bot.NEWS_DB_PATH, 'news-1'),
        ):
            with self.subTest(channel=cycle.__name__):
                stopped = False

                async def complete_first(*args):
                    nonlocal stopped
                    stopped = True
                    return True

                send.side_effect = complete_first
                with patch.object(bot, 'shutdown_requested', side_effect=lambda: stopped):
                    result = await cycle(self.telegram)
                self.assertFalse(result['ok'])
                self.assertEqual(result['failed'], 1)
                send.assert_awaited_once()
                self.assertEqual(self.remote_json(history_path), {'1': [key]})
                self.assertTrue(self.remote_json(database_path)[key]['delivery_pending'])

    async def test_changed_content_during_initial_pending_reaches_every_recipient(self):
        self.send_albo.side_effect = [True, False]
        self.assertFalse((await bot._run_check_albo(self.telegram))['ok'])
        self.restart()
        self.version('B')
        self.assertTrue((await bot._run_check_albo(self.telegram))['ok'])
        self.assertEqual([call.args[1] for call in self.send_albo.await_args_list], [1, 2])
        self.assertTrue(all(call.args[2]['_notification_kind'] == 'revision'
                            for call in self.send_albo.await_args_list))
        self.assertEqual(self.remote_json(bot.DB_PATH)[self.key]['delivery_epoch'], 1)

    async def test_changed_content_during_revision_pending_uses_fresh_key(self):
        self.known_albo()
        self.version('B')
        self.send_albo.side_effect = [True, False]
        await bot._run_check_albo(self.telegram)
        old_key = self.remote_json(bot.DB_PATH)[self.key]['pending_delivery_key']
        self.restart()
        self.version('C')
        self.assertTrue((await bot._run_check_albo(self.telegram))['ok'])
        self.assertEqual([call.args[1] for call in self.send_albo.await_args_list], [1, 2])
        record = self.remote_json(bot.DB_PATH)[self.key]
        self.assertEqual(record['delivery_epoch'], 2)
        key = bot.revision_delivery_key(self.key, record['revision_fingerprint'], 2)
        self.assertNotEqual(key, old_key)
        self.assertIn(key, self.remote_json(bot.USER_SEEN_PATH)['2'])

    async def test_return_to_baseline_after_partial_revision_notifies_again(self):
        self.known_albo()
        self.version('B')
        self.send_albo.side_effect = [True, False]
        await bot._run_check_albo(self.telegram)
        self.restart()
        self.version('A')
        self.assertTrue((await bot._run_check_albo(self.telegram))['ok'])
        self.assertEqual([call.args[1] for call in self.send_albo.await_args_list], [1, 2])
        record = self.remote_json(bot.DB_PATH)[self.key]
        self.assertEqual(record['delivery_epoch'], 2)
        self.assertFalse(record['delivery_pending'])

    async def test_return_to_baseline_before_any_revision_delivery_stays_silent(self):
        self.known_albo()
        self.version('B')
        self.send_albo.return_value = False
        await bot._run_check_albo(self.telegram)
        self.restart()
        self.version('A')
        self.assertTrue((await bot._run_check_albo(self.telegram))['ok'])
        self.send_albo.assert_not_awaited()
        record = self.remote_json(bot.DB_PATH)[self.key]
        self.assertFalse(record['delivery_pending'])
        self.assertFalse(any(key.startswith('pending_') for key in record))


if __name__ == '__main__':
    unittest.main()
