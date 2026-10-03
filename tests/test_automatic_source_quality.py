"""Automatic acquisition must not turn incomplete observations into revisions."""

import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from test_attachment_primitives import (
    HalleyFixture, act, bot, detail, listing, pagination, reference,
)


class AutomaticSourceQualityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ('DB_PATH', 'NEWS_DB_PATH', 'USER_SEEN_PATH', 'USER_SEEN_NEWS_PATH',
                     'ALBO_SAFETY_PATH', 'TELEGRAM_UPDATES_PATH', 'LAST_CHECK_PATH',
                     'SUBSCRIBERS_PATH', 'SUBSCRIBERS_NEWS_PATH'):
            self.stack.enter_context(patch.object(bot, name, self.root / name))
        self.spool = self.root / 'spool'
        self.spool.mkdir()
        self.stack.enter_context(patch.object(bot, '_ATTACHMENT_TEMP_DIR', self.spool))
        self.stack.enter_context(patch.object(bot, '_ATTACHMENT_SPOOL_BYTES', 0))
        self.stack.enter_context(patch.object(bot, '_ALBO_SESSION_LOCK', asyncio.Lock()))
        self.stack.enter_context(patch.object(bot, '_CHAT_DELIVERY_LOCKS', {}))
        self.stack.enter_context(patch.object(bot, '_fernet', None))
        self.stack.enter_context(patch.object(bot, 'MAX_RETRIES', 1))
        self.stack.enter_context(patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock))
        self.stack.enter_context(patch.object(bot, 'git_commit_and_push', return_value=True))
        self.stack.enter_context(patch.object(bot, 'get_all_recipients', return_value={1}))
        self.notify = self.stack.enter_context(patch.object(
            bot, 'notify', new_callable=AsyncMock, return_value=({1}, set())))
        self.telegram = AsyncMock()
        now = datetime.now(timezone.utc)
        self.item = act(date=now.strftime('%d-%m-%Y'),
                        date_end=(now + timedelta(days=30)).strftime('%d-%m-%Y'))
        self.key = bot.item_id(self.item)
        self.previous = bot.item_revision_snapshot(dict(
            self.item, _detail_captured=True, _attachment_fetch_ok=True,
            _attachment_expected_count=1,
            allegati=[dict(filename='previous.pdf', sha256='previous-content')],
        ))
        self.record = dict(self.item, notified=True, revision=1,
                           revision_snapshot=self.previous,
                           revision_fingerprint=bot.item_revision_fingerprint(self.previous))
        bot.remember_identity(self.record, self.item)
        self.seed()
        bot.ALBO_SAFETY_PATH.write_text('{"identity_v2_ready": true}', encoding='utf-8')
        bot.save_user_seen({}, push=False)

    def seed(self, **changes):
        bot.save_db({self.key: dict(self.record, **changes)}, push=False)

    def fixture(self, markup=None, **kwargs):
        return HalleyFixture(
            detail_html=detail(self.item, reference()) if markup is None else markup,
            pages=kwargs.pop('pages', [listing(self.item)]), **kwargs,
        )

    async def cycle(self, fixture):
        client_class = httpx.AsyncClient

        def client(**kwargs):
            return client_class(transport=httpx.MockTransport(fixture), **kwargs)

        with patch.object(bot.httpx, 'AsyncClient', side_effect=client):
            result = await bot._run_check_albo(self.telegram)
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)
        self.assertEqual(list(self.spool.iterdir()), [])
        return result

    async def fetch(self, fixture):
        client_class = httpx.AsyncClient

        def client(**kwargs):
            return client_class(transport=httpx.MockTransport(fixture), **kwargs)

        with patch.object(bot.httpx, 'AsyncClient', side_effect=client):
            items = await bot.fetch_albo_html(
                detail_selector=lambda items: items, force_detail=True,
                write_cache=False, push_cache=False,
            )
        self.addCleanup(bot.cleanup_attachment_files, items)
        return items[0]

    async def test_repeated_or_empty_announced_page_stops_before_details_or_delivery(self):
        for second in (listing(self.item) + pagination(2, 2),
                       '<li id="btSucc">Avanti</li>'):
            with self.subTest(second=second):
                original = bot.DB_PATH.read_bytes()
                fixture = self.fixture(pages=[listing(self.item) + pagination(1, 2), second])
                result = await self.cycle(fixture)
                self.assertFalse(result['ok'])
                self.assertEqual(result['new'], 0)
                self.assertNotIn('MC02', fixture.events)
                self.notify.assert_not_awaited()
                self.assertEqual(bot.DB_PATH.read_bytes(), original)

    async def test_incomplete_pagination_does_not_archive_a_new_candidate(self):
        fresh = dict(self.item, num_pub='124')
        fixture = self.fixture(pages=[listing(fresh) + pagination(1, 2),
                                      listing(fresh) + pagination(2, 2)])
        original = bot.DB_PATH.read_bytes()
        result = await self.cycle(fixture)
        self.assertFalse(result['ok'])
        self.assertEqual(bot.DB_PATH.read_bytes(), original)
        self.assertNotIn(bot.item_id(fresh), bot.load_db())
        self.notify.assert_not_awaited()
        self.assertNotIn('MC02', fixture.events)

    async def test_empty_incomplete_snapshot_with_empty_archive_is_not_success(self):
        bot.save_db({}, push=False)
        fixture = self.fixture(pages=['<li id="btSucc">Avanti</li>',
                                      '<li id="btSucc">Avanti</li>'])
        result = await self.cycle(fixture)
        self.assertFalse(result['ok'])
        self.assertEqual(bot.load_db(), {})
        self.notify.assert_not_awaited()

    async def test_unknown_malformed_and_truncated_details_never_create_zero_revision(self):
        invalid = [
            detail(self.item, '<a onclick="MC95(1)">unknown.pdf</a>'),
            detail(self.item, '<a onclick="MC96(x)">broken.pdf</a>'),
            detail(self.item, reference() + '<a onclick="MC97(x)">broken.p7m</a>'),
            detail(self.item, '<a href="/document.pdf">document.pdf</a>'),
            detail(self.item, '<div class="alert alert-danger">Documenti non caricati</div>'),
            detail(self.item, footer=False),
            detail(self.item).replace('id="allegati"', 'id="unrecognized"'),
        ]
        history = bot.USER_SEEN_PATH.read_bytes()
        for markup in invalid:
            with self.subTest(markup=markup):
                self.seed()
                fixture = self.fixture(markup)
                result = await self.cycle(fixture)
                self.assertFalse(result['ok'])
                self.assertEqual(result['detail_failures'], 1)
                self.assertEqual(result['failed'], 0)
                self.assertEqual(result['updated'], 0)
                current = bot.load_db()[self.key]
                self.assertEqual(current['revision_snapshot'], self.previous)
                self.assertEqual(current['revision_fingerprint'], self.record['revision_fingerprint'])
                self.assertEqual(current['revision'], 1)
                self.assertNotIn('last_revision_check_at', current)
                self.assertEqual(bot.USER_SEEN_PATH.read_bytes(), history)
                self.notify.assert_not_awaited()
                self.assertNotIn('GET', fixture.events)
                self.assertNotIn('MC96', fixture.events)

    async def test_partial_document_download_degrades_cycle_without_revision_or_history(self):
        fixture = self.fixture(
            detail(self.item, reference() + reference('MC97', '2', 'second.p7m')),
            payloads={'MC96': b'%PDF-1.4 valid', 'MC97': b'<html>error</html>'},
        )
        history = bot.USER_SEEN_PATH.read_bytes()
        result = await self.cycle(fixture)
        self.assertFalse(result['ok'])
        self.assertEqual(result['detail_failures'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(bot.load_db()[self.key]['revision_snapshot'], self.previous)
        self.assertEqual(bot.USER_SEEN_PATH.read_bytes(), history)
        self.notify.assert_not_awaited()

    async def test_failed_pending_revision_remains_pending_without_advancing_snapshot(self):
        self.seed(delivery_pending=True, pending_kind='revision', pending_version='pending-version',
                  pending_revision_changes=['allegati'])
        result = await self.cycle(self.fixture(detail(self.item, footer=False)))
        self.assertFalse(result['ok'])
        self.assertEqual(result['detail_failures'], 1)
        current = bot.load_db()[self.key]
        self.assertTrue(current['delivery_pending'])
        self.assertEqual(current['pending_version'], 'pending-version')
        self.assertEqual(current['revision_snapshot'], self.previous)
        self.notify.assert_not_awaited()

    async def test_confirmed_zero_can_still_notify_a_real_attachment_removal(self):
        result = await self.cycle(self.fixture(detail(self.item)))
        self.assertTrue(result['ok'])
        self.assertEqual(result['detail_failures'], 0)
        self.assertEqual(result['updated'], 1)
        self.notify.assert_awaited_once()
        sent = self.notify.call_args.args[1]
        self.assertTrue(sent['_attachment_zero_confirmed'])
        self.assertEqual(sent['_revision_changes'], ['allegati'])
        self.assertEqual(bot.load_db()[self.key]['revision_snapshot']['attachment_count'], 0)

    async def test_unselected_recently_checked_record_does_not_count_as_detail_failure(self):
        self.seed(last_revision_check_at=datetime.now(timezone.utc).isoformat())
        fixture = self.fixture()
        result = await self.cycle(fixture)
        self.assertTrue(result['ok'])
        self.assertEqual(result['detail_failures'], 0)
        self.assertNotIn('MC02', fixture.events)
        self.notify.assert_not_awaited()

    async def test_complete_attachment_section_without_expiry_is_preserved(self):
        no_expiry = dict(self.item, date_end='')
        for attachments in ('', reference('MC97', '1', 'signed.p7m')):
            with self.subTest(attachments=attachments):
                markup = detail(no_expiry, attachments)
                markup = markup.replace('<div class="calendar-date-day"><span><strong></strong></span></div>', '')
                item = await self.fetch(self.fixture(markup, pages=[listing(no_expiry)],
                                                     payloads={'MC97': b'\x30\x82signed-file'}))
                self.assertTrue(item['_attachment_fetch_ok'])
                self.assertTrue(item['_attachment_list_complete'])
                self.assertEqual(item['date_end'], '')
                self.assertIsNotNone(bot.item_revision_snapshot(item))
                self.assertEqual(item['_attachment_zero_confirmed'], not bool(attachments))
                bot.cleanup_attachment_files(item)

    async def test_non_pdf_documents_use_validated_references_and_original_delivery(self):
        names = ['signed.p7m', 'document.docx', 'sheet.xlsx', 'archive.zip']
        functions = ['MC96', 'MC97', 'MC98', 'MC99']
        markup = detail(self.item, ''.join(reference(function, str(index), name)
            for index, (function, name) in enumerate(zip(functions, names), 1)))
        item = await self.fetch(self.fixture(markup, payloads={
            function: b'fixture-content-' + function.encode() for function in functions}))
        self.assertTrue(item['_attachment_fetch_ok'])
        self.assertEqual(item['_attachment_expected_count'], 4)
        self.assertTrue(await bot.send_item_to_chat(self.telegram, 1, item))
        self.assertEqual([call.kwargs['filename'] for call in self.telegram.send_document.await_args_list], names)
        bot.cleanup_attachment_files(item)
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)

    async def test_verified_p7m_advisory_remains_a_confirmed_empty_section(self):
        advisory = ('<div class="alert alert-info mt-5">Per leggere i file firmati digitalmente '
                    "(estensione '.p7m') è necessario aver installato il software "
                    '<a href="https://www.firma.infocert.it/installazione/installazione_DiKe.php">'
                    'Dike (download)</a></div>')
        item = await self.fetch(self.fixture(detail(self.item, advisory)))
        self.assertTrue(item['_attachment_zero_confirmed'])
        self.assertTrue(item['_attachment_fetch_ok'])
        self.assertEqual(item['allegati'], [])


if __name__ == '__main__':
    unittest.main()
