"""Real HTTP transport coverage for strict, read-only Halley retrieval."""
import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

os.environ.setdefault('BOT_TOKEN', 'test-token')
os.environ.setdefault('CHAT_IDS', '1')
os.environ.pop('GITHUB_ACTIONS', None)
import httpx
import bot


def act(**changes):
    item = dict(title='Determina manutenzione riservata', num_pub='123',
                date='24-09-2026', date_end='24-10-2026', tipo='DETERMINE',
                sender='Comune', act_number='12', register_number='57', num_riga='901')
    item.update(changes)
    return item


def listing(item=None, next_page=False):
    item = item or act()
    return f'''<div class="cmp-card"><a onclick="MC02({item['num_riga']})">
        <h5>{item['title']}</h5></a><span>Pubblicazione n. {item['num_pub']}</span>
        Pubblicazione dal {item['date']} al {item['date_end']}
        Tipo: {item['tipo']} Mittente: {item['sender']} Atto n. {item['act_number']}
        Registro generale n. {item['register_number']}</div>''' + (
            '<li id="btSucc">Avanti</li>' if next_page else '')


def pagination(current, total):
    # Shape observed on the live portal: even the final page retains btSucc
    # and its PMC02 handler; the numeric counters identify the end reliably.
    return f'''<nav class="pagination-wrapper"><ul class="pagination"
        aria-label="Pagina {current} di {total}">
        <li class="page-item"><span id="pagCorrente" class="page-link">{current}</span></li>
        <li class="page-item"><span id="totalePagine" class="page-link">{total}</span></li>
        <li id="btSucc" class="page-item"><a class="page-link" onclick="PMC02()">
        <span class="visually-hidden">Pagina successiva</span></a></li></ul></nav>'''


def detail(item=None, attachments='', footer=True):
    item = item or act()
    return f'''<div class="cmp-heading"><h1>Pubblicazione n. {item['num_pub']}</h1>
      <h5>{item['title']}</h5></div>
      <section id="informazioni"><div class="richtext-wrapper">
      <strong>Tipo pubblicazione:</strong> {item['tipo']}<br>
      <strong>Mittente:</strong> {item['sender']}<br>
      Atto n. {item['act_number']} Registro generale n. {item['register_number']}
      </div></section><section id="date">
      <div class="calendar-date-day"><span><strong>{item['date']}</strong></span></div>
      <div class="calendar-date-day"><span><strong>{item['date_end']}</strong></span></div>
      </section><section id="allegati"><h2>Allegati</h2>{attachments}</section>''' + (
          '<div class="mb-2 text-end">Torna alla lista</div>' if footer else '')


def reference(function='MC96', number='1', name='documento-riservato.pdf'):
    return f'<div class="card"><a onclick="{function}(\'{number}\')">{name}</a></div>'


class HalleyFixture:
    """A reused PATH exposes any attempt to delay download until the next ref."""
    session_path = '/session-private-7f82'
    spool_path = '/spool-private-55/file'

    def __init__(self, detail_html=None, pages=None, payloads=None):
        self.detail_html = detail_html if detail_html is not None else detail(attachments=reference())
        self.pages = pages or [listing()]
        self.payloads = payloads or {'MC96': b'%PDF-1.4 first-document'}
        self.events = []
        self.pending = None
        self.cancel_function = None

    def __call__(self, request):
        fields = parse_qs(request.content.decode()) if request.method == 'POST' else {}
        function = fields.get('F', ['GET'])[0]
        self.events.append(function)
        if function == 'MC09':
            return httpx.Response(200, text=f'<meta name="jb" content="{self.session_path}">',
                                  headers={'set-cookie': 'halley=session-cookie; Path=/'})
        if 'halley=session-cookie' not in request.headers.get('cookie', ''):
            raise AssertionError('The same live client must keep the Halley cookie')
        if request.method == 'GET':
            if request.url.path != self.spool_path or self.pending is None:
                raise AssertionError('Only the fresh PATH may be downloaded')
            function, payload = self.pending
            self.pending = None
            if function == self.cancel_function:
                return httpx.Response(200, headers={'content-type': 'application/pdf'},
                                      stream=CancelledStream())
            return httpx.Response(200, headers={'content-type': 'application/octet-stream'}, content=payload)
        if request.url.path != self.session_path:
            raise AssertionError('All functions must use the freshly opened session')
        if function == 'MC01':
            return httpx.Response(200, text=self.pages[0])
        if function == 'PMC02':
            return httpx.Response(200, text=self.pages[int(fields['1'][0]) - 1])
        if function == 'MC02':
            if fields.get('NUMRIGA') != ['901']:
                raise AssertionError('The row must come from the current listing')
            return httpx.Response(200, text=self.detail_html)
        if function in ('MC96', 'MC97', 'MC98', 'MC99'):
            if self.pending is not None:
                raise AssertionError('PATH must be downloaded before another attachment lookup')
            self.pending = function, self.payloads[function]
            return httpx.Response(200, json={'K': '000', 'PATH': self.spool_path})
        raise AssertionError(f'Unexpected function: {function}')


class CancelledStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'%PDF-' + b'x' * (256 * 1024)
        raise asyncio.CancelledError()


class AttachmentPrimitiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(bot, 'DB_PATH', self.root / 'seen.json'))
        self.stack.enter_context(patch.object(bot, '_ATTACHMENT_TEMP_DIR', self.root / 'spool'))
        bot._ATTACHMENT_TEMP_DIR.mkdir()
        self.stack.enter_context(patch.object(bot, '_ATTACHMENT_SPOOL_BYTES', 0))
        self.stack.enter_context(patch.object(bot, '_ALBO_SESSION_LOCK', asyncio.Lock()))
        self.stack.enter_context(patch.object(bot, '_CHAT_DELIVERY_LOCKS', {}))
        self.stack.enter_context(patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock))
        self.stack.enter_context(patch.object(bot, 'MAX_RETRIES', 1))
        self.update_cache = self.stack.enter_context(patch.object(bot, 'update_item_cache'))
        self.push = self.stack.enter_context(patch.object(bot, 'git_commit_and_push'))
        self.atomic = self.stack.enter_context(patch.object(bot, '_atomic_write_text', wraps=bot._atomic_write_text))
        self.items = []
        self.addCleanup(lambda: bot.cleanup_attachment_files(self.items))

    async def fetch(self, fixture, **options):
        client_class = httpx.AsyncClient
        clients = []

        def factory(**kwargs):
            client = client_class(transport=httpx.MockTransport(fixture), **kwargs)
            clients.append(client)
            return client

        settings = dict(detail_selector=lambda items: items, force_detail=True,
                        write_cache=False, push_cache=False, strict_attachments=True,
                        raise_on_failure=True)
        settings.update(options)
        with patch.object(bot.httpx, 'AsyncClient', side_effect=factory):
            try:
                self.items = await bot.fetch_albo_html(**settings)
                return self.items
            finally:
                self.assertTrue(all(client.is_closed for client in clients))

    async def test_session_resolves_and_downloads_each_path_immediately(self):
        names = ['atto.pdf', 'firma.p7m', 'tabella.xlsx', 'archivio.zip']
        functions = ['MC96', 'MC97', 'MC98', 'MC99']
        payloads = {function: ('binary-data-' + function).encode() for function in functions}
        fixture = HalleyFixture(detail(attachments=''.join(reference(f, str(n), name)
            for n, (f, name) in enumerate(zip(functions, names), 1))), payloads=payloads)
        item, = await self.fetch(fixture)
        self.assertEqual(fixture.events, ['MC09', 'MC01', 'MC01', 'MC02',
                                         'MC96', 'GET', 'MC97', 'GET', 'MC98', 'GET', 'MC99', 'GET'])
        self.assertTrue(item['_attachment_fetch_ok'])
        self.assertEqual(item['_attachment_expected_count'], 4)
        for attachment, function, name in zip(item['allegati'], functions, names):
            self.assertEqual(attachment['filename'], name)
            self.assertEqual(attachment['_spool'].path.read_bytes(), payloads[function])
            self.assertEqual(attachment['sha256'], hashlib.sha256(payloads[function]).hexdigest())
            self.assertTrue(attachment['_spool'].verify())
        self.assertEqual([a['position'] for a in item['allegati']], [1, 2, 3, 4])
        self.update_cache.assert_not_called()
        self.push.assert_not_called()
        self.atomic.assert_not_called()

    async def test_readonly_fetch_does_not_migrate_legacy_database(self):
        for raw in (['legacy-hash'], {'legacy-hash': {'title': 'Old legacy record'}}):
            with self.subTest(raw=raw):
                bot.DB_PATH.write_text(json.dumps(raw), encoding='utf-8')
                before = bot.DB_PATH.read_bytes()
                self.items = await self.fetch(HalleyFixture(detail(attachments='')), force_detail=False)
                self.assertEqual(bot.DB_PATH.read_bytes(), before)
                self.atomic.assert_not_called()
                self.update_cache.assert_not_called()
                self.push.assert_not_called()

    async def test_readonly_loader_normalizes_in_memory_and_default_still_migrates(self):
        bot.DB_PATH.write_text('["legacy-hash"]', encoding='utf-8')
        self.assertEqual(bot.load_db(migrate=False), {'legacy-hash': {'notified': True}})
        self.atomic.assert_not_called()
        self.assertEqual(json.loads(bot.DB_PATH.read_text()), ['legacy-hash'])
        self.assertEqual(bot.load_db(), {'legacy-hash': {'notified': True}})
        self.atomic.assert_called_once()
        self.assertEqual(json.loads(bot.DB_PATH.read_text()), {'legacy-hash': {'notified': True}})

    async def test_mc02_publication_number_with_matching_year_downloads(self):
        fixture = HalleyFixture(detail(act(num_pub='123/2026'), reference()))
        item, = await self.fetch(fixture)
        self.assertTrue(item['_attachment_fetch_ok'])
        self.assertFalse(item['_attachment_identity_uncertain'])
        self.assertEqual(item['num_pub'], '123')
        self.assertIn('GET', fixture.events)
        self.update_cache.assert_not_called()

    async def test_mc02_publication_year_never_masks_strong_conflicts(self):
        for number in ('123/2025', '124/2026', '0/2026', '123/2026/1'):
            with self.subTest(number=number):
                fixture = HalleyFixture(detail(act(num_pub=number), reference()))
                item, = await self.fetch(fixture)
                self.assertFalse(item['_attachment_fetch_ok'])
                self.assertTrue(item['_attachment_identity_uncertain'])
                self.assertNotIn('GET', fixture.events)
        soup = bot.BeautifulSoup(detail(act(num_pub='123/2026')), 'html.parser')
        self.assertFalse(bot._strict_detail_identity_matches(act(date='invalid'), soup))

    async def test_exact_normalized_detail_title_is_required(self):
        for title in ('Determina manutenzione riservata parte diversa', 'Atto completamente differente'):
            with self.subTest(title=title):
                fixture = HalleyFixture(detail(act(title=title), reference()))
                item, = await self.fetch(fixture)
                self.assertTrue(item['_attachment_identity_uncertain'])
                self.assertFalse(item['_attachment_fetch_ok'])
                self.assertNotIn('MC96', fixture.events)
        fixture = HalleyFixture(detail(act(title='  DETERMINA  MANUTENZIONE RISERVATA  '), reference()))
        item, = await self.fetch(fixture)
        self.assertTrue(item['_attachment_fetch_ok'])

    async def test_normal_retrieval_still_updates_and_pushes_cache(self):
        item, = await self.fetch(HalleyFixture(), write_cache=True, push_cache=True,
                                strict_attachments=False)
        self.assertTrue(item['_attachment_fetch_ok'])
        self.assertEqual(self.update_cache.call_count, 2)
        self.push.assert_called_once()

    async def test_rtf_stream_is_preserved_in_manual_and_automatic_acquisition(self):
        payload = b'\xef\xbb\xbf\r\n{\\rtf1\\ansi Documento originale.\\par}'
        for strict in (True, False):
            with self.subTest(strict_attachments=strict):
                fixture = HalleyFixture(detail(attachments=reference(name='delibera.rtf')),
                                        payloads={'MC96': payload})
                item, = await self.fetch(fixture, strict_attachments=strict)
                self.assertTrue(item['_attachment_fetch_ok'])
                self.assertTrue(item['_attachment_list_complete'])
                self.assertEqual(item['_attachment_expected_count'], 1)
                attachment, = item['allegati']
                self.assertEqual(attachment['filename'], 'delibera.rtf')
                self.assertEqual(attachment['sha256'], hashlib.sha256(payload).hexdigest())
                with attachment['_spool'].open() as document:
                    self.assertEqual(document.read(), payload)
                bot.cleanup_attachment_files(item)
                self.assertEqual(list(bot._ATTACHMENT_TEMP_DIR.iterdir()), [])
                self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)

    async def test_historical_strong_identity_checked_when_live_listing_omits_it(self):
        for field, value in (('sender', 'Altro ente'), ('act_number', '99')):
            with self.subTest(field=field):
                live = act(**{field: ''})
                fixture = HalleyFixture(detail(act(**{field: value}), reference()), pages=[listing(live)])

                def selected(items):
                    items[0]['_search_expected_identity'] = act()
                    return items

                item, = await self.fetch(fixture, detail_selector=selected)
                self.assertTrue(item['_attachment_identity_uncertain'])
                self.assertFalse(item['_detail_captured'])
                self.assertNotIn('MC96', fixture.events)

    async def test_strong_detail_conflicts_stop_before_document_lookup(self):
        for field, value in (('num_pub', '124'), ('date', '23-09-2026'),
                             ('date_end', '23-10-2026'), ('tipo', 'ORDINANZE'),
                             ('sender', 'Altro ente'), ('act_number', '99'),
                             ('register_number', '88')):
            with self.subTest(field=field):
                fixture = HalleyFixture(detail(act(**{field: value}), reference()))
                item, = await self.fetch(fixture)
                self.assertTrue(item['_attachment_identity_uncertain'])
                self.assertFalse(item['_detail_captured'])
                self.assertNotIn('MC96', fixture.events)

    async def test_complete_detail_can_confirm_zero_attachments(self):
        for content in ('', 'Nessun allegato presente.',
                        '<div class="alert alert-info mt-5">Per leggere i file firmati digitalmente '
                        "(estensione '.p7m') è necessario aver installato il software "
                        '<a href="https://www.firma.infocert.it/installazione/installazione_DiKe.php">'
                        'Dike (download)</a></div>'):
            with self.subTest(content=content):
                item, = await self.fetch(HalleyFixture(detail(attachments=content)))
                self.assertTrue(item['_detail_captured'])
                self.assertTrue(item['_attachment_fetch_ok'])
                self.assertTrue(item['_attachment_list_complete'])
                self.assertTrue(item['_attachment_zero_confirmed'])
                self.assertEqual(item['_attachment_expected_count'], 0)

    async def test_incomplete_or_unknown_document_markup_never_confirms_zero(self):
        malformed = [detail(footer=False), '<html>session expired</html>',
                     detail(attachments='<a onclick="MC96(\'x\')">broken.pdf</a>'),
                     detail(attachments='<a onclick="MC96(1); evil()">broken.pdf</a>'),
                     detail(attachments='<button onclick="MC95(1)">new.pdf</button>'),
                     detail(attachments='<div class="alert"><a onclick="MC95(1)">new.pdf</a></div>'),
                     detail(attachments='<div class="alert"><a href="/new.pdf" download>new.pdf</a></div>'),
                     detail(attachments='<div class="alert card"><a href="/new.pdf">new.pdf</a></div>'),
                     detail(attachments='<a href="/download/new.pdf">new.pdf</a>'),
                     detail(attachments='<div class="card">unreadable document</div>'),
                     detail(attachments='Documenti non caricati'),
                     detail().replace('24-10-2026', '00-00-0000'),
                     detail() + '<a onclick="MC96(1)">outside.pdf</a>']
        for html in malformed:
            with self.subTest(html=html):
                fixture = HalleyFixture(html)
                item, = await self.fetch(fixture)
                self.assertFalse(item['_attachment_zero_confirmed'])
                self.assertFalse(item['_attachment_list_complete'])
                self.assertFalse(item['_attachment_fetch_ok'])
                self.assertNotIn('MC96', fixture.events)

    async def test_malformed_reference_does_not_send_valid_subset(self):
        fixture = HalleyFixture(detail(attachments=reference() + '<a onclick="MC97(x)">broken</a>'))
        item, = await self.fetch(fixture)
        self.assertFalse(item['_attachment_fetch_ok'])
        self.assertNotIn('MC96', fixture.events)
        self.assertEqual(list(bot._ATTACHMENT_TEMP_DIR.iterdir()), [])

    async def test_partial_download_fails_delivery_preflight(self):
        fixture = HalleyFixture(detail(attachments=reference() + reference('MC97', '2')),
                                payloads={'MC96': b'%PDF-1.4 good', 'MC97': b'<html>error</html>'})
        item, = await self.fetch(fixture)
        self.assertFalse(item['_attachment_fetch_ok'])
        self.assertEqual(item['_attachment_expected_count'], 2)
        self.assertEqual(len(item['allegati']), 1)
        item['_search_manual'] = True
        telegram = AsyncMock()
        self.assertFalse(await bot.send_item_to_chat(telegram, 123, item))
        telegram.send_document.assert_not_awaited()
        telegram.send_message.assert_not_awaited()
        bot.cleanup_attachment_files(item)
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)
        self.assertEqual(list(bot._ATTACHMENT_TEMP_DIR.iterdir()), [])

    async def test_cancellation_cleans_completed_and_partial_stream_spools(self):
        fixture = HalleyFixture(detail(attachments=reference() + reference('MC97', '2')),
                                payloads={'MC96': b'%PDF-1.4 good', 'MC97': b'unused'})
        fixture.cancel_function = 'MC97'
        with self.assertRaises(asyncio.CancelledError):
            await self.fetch(fixture)
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)
        self.assertEqual(list(bot._ATTACHMENT_TEMP_DIR.iterdir()), [])

    async def test_duplicate_page_marks_snapshot_incomplete(self):
        diagnostics = {}
        fixture = HalleyFixture(pages=[listing(next_page=True), listing(next_page=True)])
        items = await self.fetch(fixture, detail_selector=None, diagnostics=diagnostics)
        self.assertEqual(len(items), 1)
        self.assertEqual(diagnostics['incomplete_pagination'], [2])
        self.assertEqual(diagnostics['pages_read'], [1, 2])
        self.assertNotIn('MC02', fixture.events)

    async def test_halley_page_counters_stop_without_requesting_phantom_page(self):
        diagnostics = {}
        fixture = HalleyFixture(pages=[listing() + pagination(1, 2),
            listing(act(num_riga='902', num_pub='124')) + pagination(2, 2)])
        items = await self.fetch(fixture, detail_selector=None, diagnostics=diagnostics)
        self.assertEqual(len(items), 2)
        self.assertEqual(diagnostics['incomplete_pagination'], [])
        self.assertEqual(diagnostics['pages_read'], [1, 2])
        self.assertEqual(fixture.events, ['MC09', 'MC01', 'PMC02'])

    async def test_last_page_counter_does_not_hide_a_repeated_page(self):
        diagnostics = {}
        fixture = HalleyFixture(pages=[listing() + pagination(1, 2),
                                      listing() + pagination(2, 2)])
        await self.fetch(fixture, detail_selector=None, diagnostics=diagnostics)
        self.assertEqual(diagnostics['incomplete_pagination'], [2])
        self.assertNotIn('MC02', fixture.events)

    async def test_last_page_counter_does_not_hide_unreadable_cards(self):
        diagnostics = {}
        fixture = HalleyFixture(pages=[listing() + pagination(1, 2),
            '<div class="cmp-card"><a onclick="MC02(902)"><h5></h5></a></div>'
            + pagination(2, 2)])
        await self.fetch(fixture, detail_selector=None, diagnostics=diagnostics)
        self.assertEqual(diagnostics['unreadable_pages'], [2])
        self.assertEqual(diagnostics['partial_pages'], [2])
        self.assertNotIn('MC02', fixture.events)

    def test_invalid_page_counters_do_not_claim_end_of_listing(self):
        for current, total in [('0', '0'), ('3', '2'), ('?', '2'), ('2', ''), ('', '2')]:
            with self.subTest(current=current, total=total):
                self.assertTrue(bot.has_next_page(pagination(current, total)))

    async def test_empty_page_with_next_button_marks_snapshot_incomplete(self):
        diagnostics = {}
        fixture = HalleyFixture(pages=[listing(next_page=True), '<li id="btSucc">Avanti</li>'])
        await self.fetch(fixture, detail_selector=None, diagnostics=diagnostics)
        self.assertEqual(diagnostics['incomplete_pagination'], [2])

    async def test_strict_success_and_failure_logs_omit_sensitive_identifiers(self):
        fixture = HalleyFixture(detail(attachments=reference() + reference('MC97', '2')),
                                payloads={'MC96': b'%PDF-1.4 good', 'MC97': b'<html>error</html>'})
        with self.assertLogs(bot.log, level='INFO') as captured:
            await self.fetch(fixture)
        output = '\n'.join(captured.output)
        for sensitive in (act()['title'], 'documento-riservato', fixture.session_path,
                          fixture.spool_path, act()['num_riga'], 'session-cookie'):
            self.assertNotIn(sensitive, output)
        self.assertIn('previsti=2 acquisiti=1', output)
        self.assertIn('ValueError', output)


if __name__ == '__main__':
    unittest.main()
