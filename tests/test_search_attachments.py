"""On-demand search retrieval: identity, session ownership, delivery and state."""
import asyncio
import copy
import hashlib
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

os.environ.setdefault('BOT_TOKEN', 'test-token')
os.environ.setdefault('CHAT_IDS', '1')
os.environ.pop('GITHUB_ACTIONS', None)
import httpx
import bot


EXPIRED = '⌛ Questa richiesta di allegati è scaduta. Ripeti /cerca.'
UNCERTAIN = "⚠️ Non riesco a identificare con certezza l'atto sul portale. Nessun allegato è stato inviato."
UNAVAILABLE = "ℹ️ L'atto è presente nello storico del bot, ma al momento non riesco a recuperarne gli allegati dal portale Halley."
EMPTY = "ℹ️ L'atto non presenta allegati disponibili sul portale."
PARTIAL = '⚠️ Non sono riuscito a recuperare tutti gli allegati. Nessun documento è stato inviato; puoi riprovare.'


def act(**changes):
    item = dict(title='Determina manutenzione illuminazione cittadina', num_pub='123',
                date='24-09-2026', date_end='24-10-2026', tipo='DETERMINE',
                sender='Comune', act_number='12', register_number='57', num_riga='901')
    item.update(changes)
    return item


def buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row] if keyboard else []


class IsolatedSearchState:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stack.close)
        for name in ('DB_PATH', 'NEWS_DB_PATH', 'USER_SEEN_PATH', 'USER_SEEN_NEWS_PATH',
                     'ALBO_SAFETY_PATH', 'TELEGRAM_UPDATES_PATH', 'LAST_CHECK_PATH',
                     'SUBSCRIBERS_PATH', 'SUBSCRIBERS_NEWS_PATH'):
            self.stack.enter_context(patch.object(bot, name, self.root / name.lower()))
        self.spool_dir = self.root / 'spool'
        self.spool_dir.mkdir()
        self.stack.enter_context(patch.object(bot, '_ATTACHMENT_TEMP_DIR', self.spool_dir))
        self.stack.enter_context(patch.object(bot, '_ATTACHMENT_SPOOL_BYTES', 0))
        self.stack.enter_context(patch.object(bot, '_ALBO_SESSION_LOCK', asyncio.Lock()))
        self.stack.enter_context(patch.object(bot, '_CHAT_DELIVERY_LOCKS', {}))
        self.stack.enter_context(patch.dict(bot._search_sessions, {}, clear=True))
        self.stack.enter_context(patch.dict(bot._search_attachment_requests, {}, clear=True))
        self.push = self.stack.enter_context(patch.object(bot, 'git_commit_and_push', return_value=True))
        self.notify = self.stack.enter_context(patch.object(bot, 'notify', new_callable=AsyncMock))
        self.mark_seen = self.stack.enter_context(patch.object(bot, 'mark_user_seen'))
        self.live = act()
        self.key = bot.item_id_v2(self.live)
        self.record = dict(self.live, num_riga='historical-row-must-never-be-used',
                           notified=True, revision=4, revision_fingerprint='stable-fingerprint',
                           revision_snapshot={'attachment_count': 1, 'attachment_sha256': ['old']},
                           delivery_pending=True, pending_kind='revision')
        bot.save_db({self.key: self.record}, push=False)
        bot.ALBO_SAFETY_PATH.write_text('{"identity_v2_ready": true}', encoding='utf-8')
        bot.USER_SEEN_PATH.write_text('{"fixture": []}', encoding='utf-8')
        self.context = SimpleNamespace(bot=AsyncMock(), args=['illuminazione'])

    def issue(self, chat_id=101, *, expires=None):
        ref = 'session-fixture'
        records = [dict(self.record, _id=self.key)]
        bot._search_sessions[ref] = dict(chat_id=chat_id, records=records, news=False,
                                        expires=expires or datetime.now(timezone.utc).timestamp() + 1800)
        _, keyboard = bot.render_search_page(records, 0, ref)
        return next(b.callback_data for b in buttons(keyboard)
                    if b.callback_data.startswith('search_attach:'))

    def callback(self, data, chat_id=101, update_id=1001):
        query = AsyncMock()
        query.data = data
        return SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=chat_id),
                               effective_message=None, message=query.message, update_id=update_id)

    def text(self, update):
        calls = list(update.callback_query.mock_calls) + list(self.context.bot.mock_calls)
        return '\n'.join(str(call) for call in calls)

    def file_item(self, filenames=('atto.pdf',), *, expected=None, complete=True):
        attachments = []
        paths = []
        for index, filename in enumerate(filenames, 1):
            content = ('fixture-content-' + filename).encode()
            path = self.spool_dir / filename
            path.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            self.assertTrue(bot._reserve_attachment_spool(len(content)))
            spool = bot._AttachmentSpool(path, len(content), digest)
            attachments.append(dict(filename=filename, position=index, size=len(content),
                                    sha256=digest, _spool=spool))
            paths.append(path)
        item = dict(self.live, allegati=attachments, _detail_captured=True,
                    _attachment_expected_count=len(attachments) if expected is None else expected,
                    _attachment_fetch_ok=complete)
        return item, paths


class SearchInterfaceTests(IsolatedSearchState, unittest.IsolatedAsyncioTestCase):
    async def test_albo_search_adds_opaque_attachment_button_without_downloading(self):
        update = SimpleNamespace(message=AsyncMock(), effective_chat=SimpleNamespace(id=101))
        with patch.object(bot, 'fetch_search_attachment', new_callable=AsyncMock) as fetch:
            await bot.cmd_cerca(update, self.context)
        fetch.assert_not_awaited()
        keyboard = update.message.reply_text.call_args.kwargs['reply_markup']
        attachment_buttons = [b for b in buttons(keyboard) if b.callback_data.startswith('search_attach:')]
        self.assertEqual(len(attachment_buttons), 1)
        data = attachment_buttons[0].callback_data
        self.assertIn('📎 Scarica allegati', attachment_buttons[0].text)
        self.assertLessEqual(len(data.encode()), 64)
        self.assertRegex(data, r'^search_attach:[A-Za-z0-9_-]{12,}$')
        for sensitive in (self.key, 'historical-row', 'http', 'PATH', 'NUMRIGA'):
            self.assertNotIn(sensitive, data)
        request = bot._search_attachment_requests[data.split(':', 1)[1]]
        self.assertEqual(request['chat_id'], 101)
        self.assertEqual(request['item_key'], self.key)
        self.assertLessEqual(set(request), {'chat_id', 'item_key', 'expires', 'session_ref'})

    async def test_news_search_has_no_attachment_buttons_or_tokens(self):
        update = SimpleNamespace(message=AsyncMock(), effective_chat=SimpleNamespace(id=101))
        with patch.object(bot, 'load_news_db', return_value={str(i): dict(title='illuminazione news') for i in range(6)}):
            await bot.cmd_cerca_news(update, self.context)
        keyboard = update.message.reply_text.call_args.kwargs['reply_markup']
        self.assertTrue(buttons(keyboard))  # News still has its normal pagination.
        self.assertFalse(any(b.callback_data.startswith('search_attach:') for b in buttons(keyboard)))
        self.assertEqual(bot._search_attachment_requests, {})

    async def test_search_keeps_case_and_accent_insensitive_matching(self):
        bot.save_db({self.key: dict(self.record, title='Manutènzione Élettrica')}, push=False)
        self.context.args = ['MANUTENZIONE', 'ELETTRICA']
        update = SimpleNamespace(message=AsyncMock(), effective_chat=SimpleNamespace(id=101))
        await bot.cmd_cerca(update, self.context)
        self.assertIn('Manutènzione Élettrica', update.message.reply_text.call_args.args[0])
        self.assertEqual(len(bot._search_attachment_requests), 1)

    async def test_failed_search_response_does_not_leave_unreachable_tokens(self):
        update = SimpleNamespace(message=AsyncMock(), effective_chat=SimpleNamespace(id=101))
        update.message.reply_text.side_effect = RuntimeError('Telegram unavailable')
        with self.assertRaises(RuntimeError):
            await bot.cmd_cerca(update, self.context)
        self.assertEqual(bot._search_sessions, {})
        self.assertEqual(bot._search_attachment_requests, {})

    async def test_pagination_displays_five_results_and_bounds_tokens(self):
        records = [dict(self.record, _id=str(i), title=f'Atto pagina {i}') for i in range(12)]
        bot._search_sessions['pages'] = dict(chat_id=101, records=records, news=False, expires=10**12)
        for page, expected_count in ((0, 5), (1, 5), (2, 2), (0, 5)):
            update = self.callback(f'search:pages:{page}')
            await bot.cmd_search_page(update, self.context)
            call = update.callback_query.edit_message_text.call_args
            self.assertIn(f'pagina {page + 1}/3', call.args[0])
            self.assertEqual(call.args[0].count('Atto pagina'), expected_count)
            self.assertEqual(sum(b.callback_data.startswith('search_attach:') for b in buttons(call.kwargs['reply_markup'])), expected_count)
            self.assertEqual(len(bot._search_attachment_requests), expected_count)

    async def test_search_pagination_other_chat_cannot_generate_attachment_tokens(self):
        original = self.issue()
        before = copy.deepcopy(bot._search_attachment_requests)
        update = self.callback('search:session-fixture:0', chat_id=202)
        await bot.cmd_search_page(update, self.context)
        update.callback_query.edit_message_text.assert_not_awaited()
        self.assertEqual(bot._search_attachment_requests, before)
        self.assertIn(original.split(':')[1], before)

    async def test_expired_parent_session_purges_its_attachment_tokens(self):
        self.issue()
        bot._search_sessions['session-fixture']['expires'] = 1
        bot._purge_search_requests()
        self.assertEqual(bot._search_sessions, {})
        self.assertEqual(bot._search_attachment_requests, {})

    async def test_session_eviction_removes_child_tokens(self):
        for index in range(105):
            update = SimpleNamespace(message=AsyncMock(), effective_chat=SimpleNamespace(id=101))
            await bot.cmd_cerca(update, self.context)
        self.assertLessEqual(len(bot._search_sessions), 100)
        self.assertLessEqual(len(bot._search_attachment_requests), 500)
        self.assertTrue(all(request['session_ref'] in bot._search_sessions
                            for request in bot._search_attachment_requests.values()))


class LiveIdentityTests(IsolatedSearchState, unittest.TestCase):
    def test_current_row_replaces_historical_row(self):
        selected = bot.resolve_search_attachment([self.live], self.key, self.record)
        self.assertIs(selected, self.live)
        self.assertEqual(selected['num_riga'], '901')
        self.assertEqual(self.record['num_riga'], 'historical-row-must-never-be-used')

    def test_legacy_identity_and_v2_alias_find_same_live_act(self):
        legacy = bot.legacy_item_id(self.live)
        old = dict(self.record, primary_id=self.key, legacy_ids=[legacy])
        self.assertIs(bot.resolve_search_attachment([self.live], legacy, old), self.live)

    def test_cosmetic_identity_changes_are_accepted(self):
        live = dict(self.live, title='  DETERMINA manutenzione illuminazione cittadina ',
                    num_pub='Pubblicazione n. 00123', sender=' comune ', tipo='determine')
        self.assertIs(bot.resolve_search_attachment([live], self.key, self.record), live)

    def test_ambiguous_equivalent_live_rows_are_rejected(self):
        with self.assertRaises(bot.SearchAttachmentError) as caught:
            bot.resolve_search_attachment([self.live, dict(self.live, num_riga='902')], self.key, self.record)
        self.assertEqual(caught.exception.reason, 'uncertain')

    def test_conflicting_strong_fields_fail_closed_even_with_matching_primary_id(self):
        for field, value in [('title', 'Oggetto diverso'), ('date', '25-09-2026'),
                             ('tipo', 'ORDINANZA'), ('sender', 'Altro ente'),
                             ('act_number', '99'), ('register_number', '98')]:
            with self.subTest(field=field), self.assertRaises(bot.SearchAttachmentError) as caught:
                bot.resolve_search_attachment([dict(self.live, **{field: value})], self.key, self.record)
            self.assertEqual(caught.exception.reason, 'uncertain')

    def test_insufficient_history_is_not_guessed_from_a_hash(self):
        with self.assertRaises(bot.SearchAttachmentError) as caught:
            bot.resolve_search_attachment([self.live], self.key, {'notified': True})
        self.assertEqual(caught.exception.reason, 'uncertain')

    def test_act_absent_online_remains_unavailable_in_history(self):
        before = bot.DB_PATH.read_bytes()
        with self.assertRaises(bot.SearchAttachmentError) as caught:
            bot.resolve_search_attachment([act(num_pub='999', title='Altro atto')], self.key, self.record)
        self.assertEqual(caught.exception.reason, 'unavailable')
        self.assertEqual(bot.DB_PATH.read_bytes(), before)


class AttachmentCallbackTests(IsolatedSearchState, unittest.IsolatedAsyncioTestCase):
    async def invoke(self, item, *, data=None, chat_id=101):
        update = self.callback(data or self.issue(), chat_id=chat_id)
        with patch.object(bot, 'fetch_search_attachment', new_callable=AsyncMock, return_value=item) as fetch, \
             patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock):
            await bot.cmd_search_attachments(update, self.context)
        return update, fetch

    async def test_correct_chat_downloads_one_attachment_then_rejects_token_replay(self):
        item, paths = self.file_item()
        data = self.issue()
        update, fetch = await self.invoke(item, data=data)
        fetch.assert_awaited_once_with(self.key)
        self.context.bot.send_document.assert_awaited_once()
        self.assertEqual(self.context.bot.send_document.call_args.kwargs['chat_id'], 101)
        self.assertIn('✅ Allegati recuperati: 1', self.text(update))
        self.assertIn('🔎 Recupero gli allegati dal portale...', self.text(update))
        self.assertTrue(all(not path.exists() for path in paths))
        replay, fetch = await self.invoke(item, data=data)
        fetch.assert_not_awaited()
        self.assertIn(EXPIRED, self.text(replay))
        self.context.bot.send_document.assert_awaited_once()

    async def test_other_chat_is_rejected_without_consuming_owner_request(self):
        data = self.issue()
        item, _ = self.file_item()
        update, fetch = await self.invoke(item, data=data, chat_id=202)
        fetch.assert_not_awaited()
        self.context.bot.send_document.assert_not_awaited()
        self.assertTrue(update.callback_query.answer.called)
        self.assertIn(data.split(':')[1], bot._search_attachment_requests)
        await self.invoke(item, data=data)
        self.context.bot.send_document.assert_awaited_once()

    async def test_callback_answer_failure_does_not_abandon_valid_download(self):
        item, paths = self.file_item()
        update = self.callback(self.issue())
        update.callback_query.answer.side_effect = RuntimeError('callback answer expired')
        with patch.object(bot, 'fetch_search_attachment', new_callable=AsyncMock, return_value=item) as fetch, \
             patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock):
            await bot.cmd_search_attachments(update, self.context)
        fetch.assert_awaited_once_with(self.key)
        self.context.bot.send_document.assert_awaited_once()
        self.assertIn('✅ Allegati recuperati: 1', self.text(update))
        self.assertTrue(all(not path.exists() for path in paths))

    async def test_expired_and_unknown_tokens_are_safe_after_restart(self):
        data = self.issue()
        bot._search_attachment_requests[data.split(':')[1]]['expires'] = 1
        for candidate in (data, 'search_attach:unknown-after-restart'):
            with self.subTest(candidate=candidate):
                update, fetch = await self.invoke(None, data=candidate)
                fetch.assert_not_awaited()
                self.assertIn(EXPIRED, self.text(update))
        self.context.bot.send_document.assert_not_awaited()

    async def test_zero_attachments_has_distinct_message(self):
        item, _ = self.file_item(())
        update, _ = await self.invoke(item)
        self.context.bot.send_document.assert_not_awaited()
        self.assertIn(EMPTY, self.text(update))
        self.assertNotIn('✅ Allegati recuperati:', self.text(update))

    async def test_multiple_supported_formats_reach_telegram_and_all_spools_are_cleaned(self):
        formats = ('atto.pdf', 'firma.p7m', 'testo.doc', 'testo.docx', 'archivio.zip', 'tabella.csv')
        item, paths = self.file_item(formats)
        update, _ = await self.invoke(item)
        sent = [call.kwargs['filename'] for call in self.context.bot.send_document.call_args_list]
        self.assertEqual(sent, list(formats))
        self.assertIn('✅ Allegati recuperati: 6', self.text(update))
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)

    async def test_partial_acquisition_sends_no_subset_and_cleans_up(self):
        item, paths = self.file_item(expected=2, complete=False)
        update, _ = await self.invoke(item)
        self.context.bot.send_document.assert_not_awaited()
        self.assertIn(PARTIAL, self.text(update))
        self.assertTrue(all(not path.exists() for path in paths))

    async def test_preflight_rejects_tampered_spool_before_any_document(self):
        item, paths = self.file_item(('a.pdf', 'b.pdf'))
        paths[1].write_bytes(b'tampered')
        update, _ = await self.invoke(item)
        self.context.bot.send_document.assert_not_awaited()
        self.assertIn(PARTIAL, self.text(update))
        self.assertTrue(all(not path.exists() for path in paths))

    async def test_halley_failures_have_specific_messages_and_no_documents(self):
        for reason, message in [('uncertain', UNCERTAIN), ('unavailable', UNAVAILABLE),
                                ('incomplete', PARTIAL), ('network', None)]:
            with self.subTest(reason=reason):
                update = self.callback(self.issue())
                with patch.object(bot, 'fetch_search_attachment', new_callable=AsyncMock,
                                  side_effect=bot.SearchAttachmentError(reason)):
                    await bot.cmd_search_attachments(update, self.context)
                text = self.text(update)
                if message:
                    self.assertIn(message, text)
                else:
                    self.assertNotIn(EMPTY, text)
                    self.assertNotIn('✅ Allegati recuperati:', text)
        self.context.bot.send_document.assert_not_awaited()

    async def test_telegram_failure_cleans_spool_and_never_reports_success(self):
        item, paths = self.file_item()
        self.context.bot.send_document.side_effect = RuntimeError('telegram failure')
        update, _ = await self.invoke(item)
        self.assertNotIn('✅ Allegati recuperati:', self.text(update))
        self.assertTrue(all(not path.exists() for path in paths))

    async def test_fetch_exception_after_spool_creation_cleans_unreturned_files(self):
        paths = []
        async def interrupted(_):
            item, created = self.file_item()
            paths.extend(created)
            raise RuntimeError('after download before return')
        update = self.callback(self.issue())
        with patch.object(bot, 'fetch_search_attachment', side_effect=interrupted):
            await bot.cmd_search_attachments(update, self.context)
        self.assertTrue(paths)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)
        self.context.bot.send_document.assert_not_awaited()

    async def test_task_cancellation_propagates_and_cleans_spool(self):
        paths = []
        async def cancelled(_):
            item, created = self.file_item()
            paths.extend(created)
            raise asyncio.CancelledError()
        update = self.callback(self.issue())
        with patch.object(bot, 'fetch_search_attachment', side_effect=cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await bot.cmd_search_attachments(update, self.context)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)
        self.context.bot.send_document.assert_not_awaited()

    async def test_cancellation_during_telegram_send_cleans_returned_spool(self):
        item, paths = self.file_item()
        update = self.callback(self.issue())
        self.context.bot.send_document.side_effect = asyncio.CancelledError()
        with patch.object(bot, 'fetch_search_attachment', new_callable=AsyncMock, return_value=item):
            with self.assertRaises(asyncio.CancelledError):
                await bot.cmd_search_attachments(update, self.context)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)
        self.assertIn('Recupero interrotto', self.text(update))
        self.assertNotIn('✅ Allegati recuperati:', self.text(update))

    async def test_timeout_cleans_unreturned_spool_and_replaces_progress(self):
        paths = []
        async def blocked(_):
            _, created = self.file_item()
            paths.extend(created)
            await asyncio.Event().wait()
        update = self.callback(self.issue())
        with patch.object(bot, 'fetch_search_attachment', side_effect=blocked), \
             patch.object(bot, 'SEARCH_ATTACHMENT_TIMEOUT_SECONDS', 0.01):
            await asyncio.wait_for(bot.cmd_search_attachments(update, self.context), timeout=2)
        self.assertTrue(paths)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)
        self.context.bot.send_document.assert_not_awaited()
        self.context.bot.send_message.return_value.edit_text.assert_awaited_once()
        self.assertNotIn(EMPTY, self.text(update))

    async def test_concurrent_replay_cannot_reenter_download_before_first_finishes(self):
        item, paths = self.file_item()
        data = self.issue()
        entered = asyncio.Event()
        release = asyncio.Event()
        async def blocked(_):
            entered.set()
            await release.wait()
            return item
        with patch.object(bot, 'fetch_search_attachment', side_effect=blocked) as fetch, \
             patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock):
            first = asyncio.create_task(bot.cmd_search_attachments(self.callback(data), self.context))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                duplicate = self.callback(data, update_id=1002)
                await bot.cmd_search_attachments(duplicate, self.context)
                self.assertIn(EXPIRED, self.text(duplicate))
            finally:
                release.set()
                await asyncio.wait_for(first, timeout=2)
        fetch.assert_awaited_once()
        self.context.bot.send_document.assert_awaited_once()
        self.assertTrue(all(not path.exists() for path in paths))

    async def test_manual_download_preserves_every_persistent_state_byte(self):
        before = {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        item, _ = self.file_item()
        await self.invoke(item)
        after = {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        self.assertEqual(after, before)
        self.notify.assert_not_awaited()
        self.mark_seen.assert_not_called()  # A manual download does not consume revision notifications.
        self.push.assert_not_called()

    async def test_duplicate_telegram_update_does_not_execute_a_second_attachment_request(self):
        data = self.issue()
        update = self.callback(data)
        duplicate = self.callback(data)
        app = AsyncMock()
        stop = asyncio.Event()
        app.bot.get_updates.return_value = [update, duplicate]
        deliveries = 0
        async def process(received):
            nonlocal deliveries
            deliveries += 1
            await bot.cmd_search_attachments(received, self.context)
        app.process_update.side_effect = process
        async def next_batch(**kwargs):
            if next_batch.called:
                stop.set()
                return []
            next_batch.called = True
            return [update, duplicate]
        next_batch.called = False
        app.bot.get_updates.side_effect = next_batch
        item, _ = self.file_item()
        with patch.object(bot, 'fetch_search_attachment', new_callable=AsyncMock, return_value=item) as fetch, \
             patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock):
            await bot.telegram_polling(app, stop)
        self.assertEqual(deliveries, 1)
        fetch.assert_awaited_once()
        self.context.bot.send_document.assert_awaited_once()
        self.assertEqual(bot.load_telegram_updates()['last_processed_update_id'], update.update_id)


class SearchTransportTests(IsolatedSearchState, unittest.IsolatedAsyncioTestCase):
    """Exercise the existing HTTP downloader through the new wrapper, without a server."""
    def transport(self, files, *, bad_detail=False, partial=False, detail_overrides=None):
        self.requests = []
        detail_values = dict(self.record, **(detail_overrides or {}))
        def handle(request):
            self.requests.append((request.method, str(request.url), request.content.decode()))
            if request.method == 'GET':
                name = request.url.path.rsplit('/', 1)[-1]
                if partial and name == list(files)[-1]:
                    return httpx.Response(503)
                return httpx.Response(200, headers={'Content-Type': 'application/octet-stream'}, content=files[name])
            fields = parse_qs(request.content.decode())
            function = fields['F'][0]
            if function == 'MC01':
                return httpx.Response(200, text='<div>Lista</div>')
            if function == 'MC02':
                self.assertEqual(fields['NUMRIGA'], ['901'])
                title = 'Atto differente senza corrispondenza' if bad_detail else self.live['title']
                links = ''.join(f'<a onclick="MC{96 + index % 4}({index + 1})">{name}</a>'
                                for index, name in enumerate(files))
                detail = f'''<main>
                    <div class="cmp-heading"><h1>Pubblicazione n. 123</h1>
                    <h5>{escape(title)}</h5></div>
                    <section id="informazioni"><div class="richtext-wrapper">
                    <strong>Mittente:</strong> {escape(detail_values['sender'])}<br>
                    <strong>Tipo pubblicazione:</strong> {escape(detail_values['tipo'])}<br>
                    Atto n. {escape(detail_values['act_number'])} -
                    Registro generale n. {escape(detail_values['register_number'])}
                    </div></section>
                    <section id="date"><div class="calendar-date-day">
                    <span><strong>24-09-2026</strong></span>
                    <span><strong>24-10-2026</strong></span></div></section>
                    <section id="allegati"><h2>Allegati</h2>{links}</section>
                    <div class="mb-2 text-end">Fine dettaglio</div></main>'''
                return httpx.Response(200, text=detail)
            self.assertIn(function, ('MC96', 'MC97', 'MC98', 'MC99'))
            index = int(fields['NUMRIG'][0]) - 1
            return httpx.Response(200, json={'K': '000', 'PATH': '/fresh-session/' + list(files)[index]})
        return httpx.MockTransport(handle)

    async def fetch(self, files, *, live_records=None, list_diagnostics=None,
                    callback_update=None, **transport_options):
        transport = self.transport(files, **transport_options)
        client_class = httpx.AsyncClient
        self.clients = []
        def client_factory(**kwargs):
            client = client_class(transport=transport, **kwargs)
            self.clients.append(client)
            return client
        async def pages(client, session_url, **kwargs):
            self.assertFalse(client.is_closed)
            self.assertTrue(bot._ALBO_SESSION_LOCK.locked())
            self.assertEqual(session_url, 'https://halley.invalid/current-session')
            if list_diagnostics:
                kwargs['diagnostics'].update(list_diagnostics)
            return ([dict(self.live), act(num_pub='999', title='Atto estraneo', num_riga='902')]
                    if live_records is None else live_records)
        with patch.object(bot.httpx, 'AsyncClient', side_effect=client_factory), \
             patch.object(bot, 'open_session', new_callable=AsyncMock, return_value='https://halley.invalid/current-session'), \
             patch.object(bot, 'fetch_all_pages', side_effect=pages), \
             patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock), \
             patch.object(bot, 'MAX_RETRIES', 1):
            if callback_update is not None:
                return await bot.cmd_search_attachments(callback_update, self.context)
            return await bot.fetch_search_attachment(self.key)

    async def test_same_fresh_session_uses_current_row_and_immediate_paths_without_writes(self):
        before = bot.DB_PATH.read_bytes()
        item = await self.fetch({'a.pdf': b'%PDF-1.7 fixture', 'b.p7m': b'\x30\x82signature'})
        try:
            prepared, error = bot._preflight_attachments(item)
            self.assertIsNone(error)
            self.assertEqual(len(prepared), 2)
            requests = self.requests
            self.assertEqual([r[0] for r in requests], ['POST', 'POST', 'POST', 'GET', 'POST', 'GET'])
            self.assertTrue(all(url == 'https://halley.invalid/current-session' for method, url, _ in requests if method == 'POST'))
            self.assertFalse(any('historical-row' in body for _, _, body in requests))
            self.assertFalse(any('NUMRIGA=902' in body for _, _, body in requests))
            self.assertEqual(bot.DB_PATH.read_bytes(), before)
            self.push.assert_not_called()
            for attachment in item['allegati']:
                self.assertFalse(set(attachment).intersection({'url', 'PATH', 'num_riga'}))
        finally:
            bot.cleanup_attachment_files(item)
        self.assertEqual(list(self.spool_dir.iterdir()), [])
        self.assertTrue(all(client.is_closed for client in self.clients))
        self.assertFalse(bot._ALBO_SESSION_LOCK.locked())

    async def test_all_supported_document_formats_use_real_spool_and_preflight(self):
        for extension, content in [('pdf', b'%PDF fixture'), ('p7m', b'\x30\x82signed'),
                                   ('doc', b'\xd0\xcf\x11\xe0doc'), ('docx', b'PK\x03\x04docx'),
                                   ('zip', b'PK\x03\x04zip'), ('csv', b'one,two\n1,2')]:
            with self.subTest(extension=extension):
                filename = 'fixture.' + extension
                item = await self.fetch({filename: content})
                try:
                    prepared, error = bot._preflight_attachments(item)
                    self.assertIsNone(error)
                    self.assertEqual(prepared[0]['filename'], filename)
                    with prepared[0]['_spool'].open() as stream:
                        self.assertEqual(stream.read(), content)
                finally:
                    bot.cleanup_attachment_files(item)
        self.assertEqual(list(self.spool_dir.iterdir()), [])

    async def test_identified_detail_without_attachments_is_empty(self):
        item = await self.fetch({})
        self.assertTrue(item['_detail_captured'])
        self.assertTrue(item['_attachment_fetch_ok'])
        self.assertEqual(item['_attachment_expected_count'], 0)
        self.assertEqual(item['allegati'], [])

    async def test_legacy_delivery_key_finds_live_v2_without_migrating_state(self):
        primary = self.key
        self.key = bot.legacy_item_id(self.live)
        bot.save_db({self.key: dict(self.record, primary_id=primary, legacy_ids=[self.key])}, push=False)
        before = bot.DB_PATH.read_bytes()
        item = await self.fetch({'legacy.pdf': b'%PDF fixture'})
        try:
            self.assertEqual(len(item['allegati']), 1)
            self.assertEqual(item['num_riga'], '901')
            self.assertEqual(bot.DB_PATH.read_bytes(), before)
        finally:
            bot.cleanup_attachment_files(item)

    async def test_alias_owned_by_multiple_historical_records_rejects_all_downloads(self):
        conflicting = dict(self.record, primary_id=self.key)
        bot.save_db({self.key: self.record, 'second-delivery-record': conflicting}, push=False)
        before = bot.DB_PATH.read_bytes()
        with self.assertRaises(bot.SearchAttachmentError) as caught:
            await self.fetch({'a.pdf': b'%PDF fixture'})
        self.assertEqual(caught.exception.reason, 'uncertain')
        self.assertEqual(self.requests, [])
        self.assertEqual(bot.DB_PATH.read_bytes(), before)
        self.assertTrue(all(client.is_closed for client in self.clients))

    async def test_ambiguous_live_rows_stop_before_mc02_or_any_download(self):
        with self.assertRaises(bot.SearchAttachmentError) as caught:
            await self.fetch({'a.pdf': b'%PDF fixture'},
                             live_records=[dict(self.live), dict(self.live, num_riga='902')])
        self.assertEqual(caught.exception.reason, 'uncertain')
        self.assertEqual(self.requests, [])

    async def test_empty_live_list_preserves_history_and_reports_unavailable(self):
        before = bot.DB_PATH.read_bytes()
        with self.assertRaises(bot.SearchAttachmentError) as caught:
            await self.fetch({}, live_records=[])
        self.assertEqual(caught.exception.reason, 'unavailable')
        self.assertEqual(self.requests, [])
        self.assertEqual(bot.DB_PATH.read_bytes(), before)

    async def test_incomplete_live_list_cannot_hide_an_ambiguous_candidate(self):
        for problem in ('skipped_cards', 'unreadable_pages', 'partial_pages', 'incomplete_pagination'):
            with self.subTest(problem=problem), self.assertRaises(bot.SearchAttachmentError) as caught:
                await self.fetch({'a.pdf': b'%PDF fixture'}, list_diagnostics={problem: [1]})
            self.assertEqual(caught.exception.reason, 'uncertain')
            self.assertEqual(self.requests, [])

    async def test_unreadable_empty_snapshot_is_uncertain_instead_of_absent(self):
        with self.assertRaises(bot.SearchAttachmentError) as caught:
            await self.fetch({}, live_records=[], list_diagnostics={'unreadable_pages': [1]})
        self.assertEqual(caught.exception.reason, 'uncertain')
        self.assertEqual(self.requests, [])

    async def test_mc02_conflicting_history_is_rejected_when_live_card_omits_field(self):
        for field, conflicting in (('sender', 'Altro ente'), ('tipo', 'ORDINANZA'),
                                   ('act_number', '99'), ('register_number', '98')):
            with self.subTest(field=field), self.assertRaises(bot.SearchAttachmentError) as caught:
                await self.fetch({'a.pdf': b'%PDF fixture'},
                                 live_records=[dict(self.live, **{field: ''})],
                                 detail_overrides={field: conflicting})
            self.assertEqual(caught.exception.reason, 'uncertain')
            self.assertFalse(any(method == 'GET' for method, _, _ in self.requests))
            self.assertEqual(list(self.spool_dir.iterdir()), [])

    async def test_full_callback_download_path_preserves_state_and_delivers_only_to_owner(self):
        before = {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        files = {'atto.pdf': b'%PDF fixture', 'firma.p7m': b'\x30\x82signature'}
        update = self.callback(self.issue())
        await self.fetch(files, callback_update=update)
        self.assertEqual(self.context.bot.send_document.await_count, 2)
        self.assertTrue(all(call.kwargs['chat_id'] == 101
                            for call in self.context.bot.send_document.call_args_list))
        self.assertIn('✅ Allegati recuperati: 2', self.text(update))
        self.assertEqual(list(self.spool_dir.iterdir()), [])
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}, before)
        self.notify.assert_not_awaited()
        self.mark_seen.assert_not_called()
        self.push.assert_not_called()

    async def test_partial_real_download_sends_no_subset_through_callback(self):
        update = self.callback(self.issue())
        await self.fetch({'a.pdf': b'%PDF fixture', 'b.pdf': b'%PDF second'},
                         partial=True, callback_update=update)
        self.assertEqual(sum(method == 'GET' for method, _, _ in self.requests), 2)
        self.context.bot.send_document.assert_not_awaited()
        self.assertIn(PARTIAL, self.text(update))
        self.assertEqual(list(self.spool_dir.iterdir()), [])
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)

    async def test_mc02_identity_mismatch_does_not_download_files(self):
        with self.assertRaises(bot.SearchAttachmentError) as caught:
            await self.fetch({'a.pdf': b'%PDF fixture'}, bad_detail=True)
        self.assertEqual(caught.exception.reason, 'uncertain')
        self.assertFalse(any(method == 'GET' for method, _, _ in self.requests))
        self.assertEqual(list(self.spool_dir.iterdir()), [])

    async def test_partial_http_download_is_rejected_and_spools_cleaned(self):
        with self.assertRaises(bot.SearchAttachmentError) as caught:
            await self.fetch({'a.pdf': b'%PDF fixture', 'b.pdf': b'%PDF second'}, partial=True)
        self.assertEqual(caught.exception.reason, 'incomplete')
        self.assertEqual(sum(method == 'GET' for method, _, _ in self.requests), 2)
        self.assertEqual(list(self.spool_dir.iterdir()), [])
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)
        self.assertTrue(all(client.is_closed for client in self.clients))

    async def test_oversized_download_is_rejected_without_leaking_spool(self):
        with patch.object(bot, 'MAX_ATTACHMENT_BYTES', 8):
            with self.assertRaises(bot.SearchAttachmentError) as caught:
                await self.fetch({'a.pdf': b'%PDF fixture exceeds limit'})
        self.assertEqual(caught.exception.reason, 'incomplete')
        self.assertEqual(sum(method == 'GET' for method, _, _ in self.requests), 1)
        self.assertEqual(list(self.spool_dir.iterdir()), [])
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)

    async def test_fetch_wrapper_requests_strict_read_only_enrichment(self):
        item, _ = self.file_item()
        async def fetch(**kwargs):
            self.assertFalse(kwargs['push_cache'])
            self.assertFalse(kwargs['write_cache'])
            self.assertTrue(kwargs['force_detail'])
            self.assertTrue(kwargs['raise_on_failure'])
            self.assertTrue(kwargs['strict_attachments'])
            self.assertIsInstance(kwargs['diagnostics'], dict)
            self.assertEqual(kwargs['detail_selector']([item, act(num_pub='999')]), [item])
            return [item]
        with patch.object(bot, 'fetch_albo_html', side_effect=fetch):
            result = await bot.fetch_search_attachment(self.key)
        self.assertIs(result, item)
        bot.cleanup_attachment_files(result)


if __name__ == '__main__':
    unittest.main()
