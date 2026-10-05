"""Document downloads preserve bytes and reject portal responses before delivery."""
import gzip
import hashlib
import os
import tempfile
import unittest
from contextlib import ExitStack
from html import unescape
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('BOT_TOKEN', 'test-token')
os.environ.setdefault('CHAT_IDS', '1')
os.environ.pop('GITHUB_ACTIONS', None)
import httpx
import bot


RTF = b"{\\rtf1\\ansi\\ansicpg1252{\\fonttbl{\\f0 Arial;}}\\f0 Documento \\'e8 autentico.\\par}"


def visible_units(text):
    visible = unescape(re.sub(r'<[^>]*>', '', text))
    return len(visible.encode('utf-16-le')) // 2


class AttachmentDownloadFormatsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(bot, '_ATTACHMENT_TEMP_DIR', self.root))
        self.stack.enter_context(patch.object(bot, '_ATTACHMENT_SPOOL_BYTES', 0))
        self.spools = []
        self.addCleanup(lambda: [spool.cleanup() for spool in self.spools])

    async def download(self, payload, name, content_type='application/octet-stream',
                       *, headers=None, status=200, url='https://fixture.invalid/file'):
        response_headers = {'content-type': content_type, **(headers or {})}

        def response(request):
            return httpx.Response(status, headers=response_headers, content=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            spool, filename = await bot._download_attachment_to_spool(client, url, name, 0)
        self.spools.append(spool)
        return spool, filename

    def assert_bytes_unchanged(self, spool, expected):
        self.assertTrue(spool.verify())
        self.assertEqual(spool.size, len(expected))
        self.assertEqual(spool.sha256, hashlib.sha256(expected).hexdigest())
        with spool.open() as document:
            self.assertEqual(document.read(), expected)
        spool.cleanup()
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)
        self.assertEqual(list(self.root.iterdir()), [])

    async def assert_rejected(self, payload, name, content_type='application/octet-stream', **options):
        try:
            with self.assertRaises(ValueError):
                await self.download(payload, name, content_type, **options)
        finally:
            # If an invalid file is unexpectedly accepted, isolate that failure
            # so later cases can still verify their own downloader cleanup.
            for spool in self.spools:
                spool.cleanup()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)

    async def test_real_rtf_preserves_bytes_with_generic_and_specific_headers(self):
        for content_type in ('text/rtf', 'application/rtf', 'application/x-rtf',
                             'application/octet-stream', 'text/plain; charset=windows-1252'):
            for prefix in (b'', b' \r\n', b'\xef\xbb\xbf\r\n'):
                with self.subTest(content_type=content_type, prefix=prefix):
                    payload = prefix + RTF
                    spool, filename = await self.download(payload, 'delibera.rtf', content_type)
                    self.assertEqual(filename, 'delibera.rtf')
                    self.assert_bytes_unchanged(spool, payload)

    async def test_rtf_without_an_extension_is_inferred_from_content(self):
        for content_type in ('application/octet-stream', 'text/plain'):
            with self.subTest(content_type=content_type):
                spool, filename = await self.download(b'\xef\xbb\xbf\n' + RTF, 'delibera', content_type)
                self.assertEqual(filename, 'delibera.rtf')
                self.assert_bytes_unchanged(spool, b'\xef\xbb\xbf\n' + RTF)

    async def test_structured_and_text_documents_are_not_error_pages(self):
        cases = (
            ('dati.json', 'application/json', b'{"nome":"Comune","error":null}'),
            ('dati.json', 'application/json', b'{"error":false,"value":1}'),
            ('dati.json', 'application/json', b'{"error":false}'),
            ('dati.json', 'application/json', b'{"error":null}'),
            ('dati.json', 'application/octet-stream', b'[{"value":1}]'),
            ('dati.geojson', 'application/geo+json',
             b'{"type":"Feature","properties":{"error":"dato"},"geometry":null}'),
            ('dati.xml', 'application/xml', b'<?xml version="1.0"?><atti><atto>1</atto></atti>'),
            ('dati.xml', 'text/xml', '<?xml version="1.0"?><atti>\u00e8</atti>'.encode('utf-16')),
            ('note.txt', 'text/plain', b'{ testo letterale, senza struttura JSON }'),
            ('note.txt', 'application/octet-stream', b'{ testo letterale }'),
            ('tabella.csv', 'text/csv', b'nome,valore\nComune,1\n'),
            ('tabella.tsv', 'text/tab-separated-values', b'nome\tvalore\nComune\t1\n'),
            ('dati.jsonl', 'application/x-ndjson', b'{"a":1}\n{"a":2}\n'),
            ('immagine.svg', 'image/svg+xml', b'<svg xmlns="http://www.w3.org/2000/svg"><path/></svg>'),
        )
        for name, content_type, payload in cases:
            with self.subTest(name=name, content_type=content_type):
                spool, filename = await self.download(payload, name, content_type)
                self.assertEqual(filename, name)
                self.assert_bytes_unchanged(spool, payload)

    async def test_a_binary_opening_bracket_is_not_a_json_signature(self):
        payload = b'[\x00\x01\xffopaque-binary\x00]'
        spool, filename = await self.download(payload, 'originale.bin')
        self.assertEqual(filename, 'originale.bin')
        self.assert_bytes_unchanged(spool, payload)

    async def test_long_json_is_not_rejected_because_its_prefix_is_incomplete(self):
        cases = (
            ('dati.json', 'application/json', b'{"description":"' + b'A' * 4096 + b'","value":1}'),
            ('dati.geojson', 'application/geo+json',
             b'{"type":"Feature","properties":{"error":"dato ' + b'A' * 4096 + b'"},"geometry":null}'),
        )
        for name, content_type, payload in cases:
            with self.subTest(name=name):
                spool, filename = await self.download(payload, name, content_type)
                self.assertEqual(filename, name)
                self.assert_bytes_unchanged(spool, payload)

    async def test_json_mime_types_infer_extensions_for_unnamed_documents(self):
        for content_type, suffix, payload in (
            ('application/json', '.json', b'{"documento":"autentico"}'),
            ('application/geo+json', '.geojson', b'{"type":"FeatureCollection","features":[]}'),
        ):
            with self.subTest(content_type=content_type):
                spool, filename = await self.download(payload, 'allegato', content_type)
                self.assertEqual(filename, 'allegato' + suffix)
                self.assert_bytes_unchanged(spool, payload)
                await self.assert_rejected(b'{"error":"Session expired"}', 'allegato', content_type)

    async def test_document_mime_types_infer_real_extensions(self):
        cases = (
            ('application/vnd.oasis.opendocument.text', '.odt', b'PK\x03\x04odt'),
            ('application/vnd.oasis.opendocument.spreadsheet', '.ods', b'PK\x03\x04ods'),
            ('application/vnd.oasis.opendocument.presentation', '.odp', b'PK\x03\x04odp'),
            ('application/pkcs7-mime', '.p7m', b'\x30\x82signed'),
            ('text/tab-separated-values', '.tsv', b'a\tb\n'),
            ('message/rfc822', '.eml', b'From: comune@example.invalid\r\n\r\nAvviso'),
            ('image/svg+xml', '.svg', b'<svg/>'),
            ('image/webp', '.webp', b'RIFF\x00\x00\x00\x00WEBP'),
        )
        for content_type, suffix, payload in cases:
            with self.subTest(content_type=content_type):
                spool, filename = await self.download(payload, 'documento', content_type)
                self.assertEqual(filename, 'documento' + suffix)
                self.assert_bytes_unchanged(spool, payload)

    async def test_explicit_html_attachment_is_preserved(self):
        payload = b'\xef\xbb\xbf<!--documento--><!doctype html><html><body>Avviso</body></html>'
        spool, filename = await self.download(payload, 'avviso.html', 'text/html', headers={
            'content-disposition': 'attachment; filename="avviso.html"'})
        self.assertEqual(filename, 'avviso.html')
        self.assert_bytes_unchanged(spool, payload)

    async def test_html_and_json_masquerading_as_documents_are_rejected(self):
        cases = (
            (b'<html>Session expired</html>', 'application/octet-stream'),
            (b'\xef\xbb\xbf<html>Session expired</html>', 'application/octet-stream'),
            (b'<!--server error--><html>Session expired</html>', 'application/octet-stream'),
            (b'<?xml version="1.0"?><html>Session expired</html>', 'application/octet-stream'),
            (b'<html>Session expired</html>', 'text/html'),
            (b'{"error":"Session expired"}', 'application/octet-stream'),
            (b'{"value":1}', 'application/json'),
            (b'[{"value":1}]', 'application/octet-stream'),
        )
        for payload, content_type in cases:
            with self.subTest(payload=payload, content_type=content_type):
                await self.assert_rejected(payload, 'atto.pdf', content_type)

    async def test_error_envelopes_are_rejected_even_with_text_document_names(self):
        cases = (
            ('dati.json', b'{"error":"Session expired"}'),
            ('dati.json', b'{"errors":["Session expired"]}'),
            ('dati.json', b'{"K":"999","PATH":"","message":"Session expired"}'),
            ('dati.json', b'{"errorCode":403,"message":"Session expired"}'),
            ('dati.json', b'{"error_code":403,"message":"Session expired"}'),
            ('note.txt', b'{"error":"Session expired"}'),
            ('dati.xml', b'<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>'),
            ('dati.xml', b'<!--storage error--><Error><Code>AccessDenied</Code></Error>'),
        )
        for name, payload in cases:
            with self.subTest(name=name, payload=payload):
                await self.assert_rejected(payload, name)

    async def test_long_or_diagnostic_error_envelopes_do_not_bypass_prefix_validation(self):
        cases = (
            b'{"error":"Session expired ' + b'x' * 4096 + b'"}',
            b'{"error":"Session expired","trace_id":"fixture"}',
        )
        for payload in cases:
            with self.subTest(payload_length=len(payload)):
                await self.assert_rejected(payload, 'dati.json', 'application/json')

    async def test_html_download_requires_a_declared_attachment(self):
        for name, disposition in (
            ('avviso.html', ''), ('avviso.html', 'inline; filename="avviso.html"'),
            ('atto.pdf', 'attachment; filename="atto.pdf"')):
            with self.subTest(name=name, disposition=disposition):
                await self.assert_rejected(b'<html>Session expired</html>', name, 'text/html', headers={
                    'content-disposition': disposition})

    async def test_partial_status_range_and_truncated_length_leave_no_spool(self):
        payload = b'%PDF-1.4 incomplete'
        for status, headers in (
            (206, {'content-range': 'bytes 0-17/999'}),
            (200, {'content-range': 'bytes 0-17/999'}),
            (200, {'content-length': str(len(payload) + 10)}),
            (200, {'content-encoding': 'identity', 'content-length': str(len(payload) + 10)}),
        ):
            with self.subTest(status=status, headers=headers):
                await self.assert_rejected(payload, 'atto.pdf', 'application/pdf',
                                           status=status, headers=headers)

    async def test_compressed_transfer_length_is_not_compared_to_decoded_document(self):
        compressed = gzip.compress(RTF)
        spool, filename = await self.download(compressed, 'atto.rtf', 'text/rtf', headers={
            'content-encoding': 'gzip', 'content-length': str(len(compressed))})
        self.assertEqual(filename, 'atto.rtf')
        self.assert_bytes_unchanged(spool, RTF)

    async def test_empty_and_interrupted_downloads_release_reserved_space(self):
        with self.assertRaises(ValueError):
            await self.download(b'', 'atto.rtf', 'text/rtf')
        self.assertEqual(list(self.root.iterdir()), [])

        class Interrupted(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield RTF
                raise httpx.ReadError('Connection interrupted')

        def response(request):
            return httpx.Response(200, headers={'content-type': 'text/rtf'}, stream=Interrupted())

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            with self.assertRaises(httpx.ReadError):
                await bot._download_attachment_to_spool(client, 'https://fixture.invalid/file', 'atto.rtf', 0)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(bot._ATTACHMENT_SPOOL_BYTES, 0)


class AttachmentFilenameAndCaptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(bot, '_CHAT_DELIVERY_LOCKS', {}))
        self.stack.enter_context(patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock))

    def test_cross_platform_names_controls_and_long_suffixes(self):
        for name in ('../../atto.rtf', r'C:\download\atto.rtf', r'folder/sub\atto.rtf',
                     'atto\x00\r\n.rtf'):
            with self.subTest(name=name):
                self.assertEqual(bot._attachment_basename(name), 'atto.rtf')
        long_name = 'A' * 250 + '.geojson'
        resolved = bot._resolved_attachment_filename(long_name, 'https://fixture.invalid/file',
                                                     'application/octet-stream', '', b'content')
        self.assertLessEqual(len(resolved), 180)
        self.assertTrue(resolved.endswith('.geojson'))
        self.assertNotIn('\x00', resolved)
        for name in ('', '.', '..', r'folder\..'):
            self.assertEqual(bot._attachment_basename(name), '')

    def test_extended_disposition_filename_has_priority_and_decodes_language(self):
        headers = (
            ('attachment; filename="fallback.txt"; filename*=UTF-8\'it\'delibera%20%C3%A8.rtf', 'delibera \u00e8.rtf'),
            ('attachment; filename*=ISO-8859-1\'it\'delibera%20%E8.rtf', 'delibera \u00e8.rtf'),
            ('attachment; filename="folder\\atto.rtf"', 'atto.rtf'),
            ('attachment; filename*=UTF-8\'\'%2E%2E%2Fatto.rtf', 'atto.rtf'),
        )
        for header, expected in headers:
            with self.subTest(header=header):
                self.assertEqual(bot._content_disposition_filename(header), expected)

    def test_bounded_emoji_and_html_text_respects_visible_limits(self):
        item = dict(title='<&>\U0001f600' * 3000, tipo='<&>' * 300,
                    num_pub='<&>\U0001f600' * 600, date='D' * 200, date_end='E' * 200,
                    _notification_kind='revision', _revision_changes=['<&>' * 1000],
                    _attachment_expected_count=12, _attachment_fetch_ok=True, allegati=[])
        self.assertLessEqual(visible_units(bot.format_caption(item)), 4096)
        for name in ('atto.p7m', 'ATTO.P7M', 'atto.rtf', 'atto.pdf'):
            with self.subTest(name=name):
                caption = bot._document_caption(item, {'filename': name}, 12, 12)
                self.assertLessEqual(visible_units(caption), 1024)
                self.assertEqual('File P7M' in caption, name.lower().endswith('.p7m'))
                self.assertNotIn('\ufffd', caption)

    async def test_twelve_documents_keep_their_order_bytes_and_p7m_advice(self):
        names = ['atto.pdf', 'firma.p7m', 'testo.rtf', 'testo.odt', 'tabella.csv',
                 'dati.json', 'dati.xml', 'figura.svg', 'archivio.zip', 'firma.P7M',
                 'testo.docx', 'tabella.xlsx']
        attachments = [dict(filename=name, position=index, content=b'original-' + name.encode())
                       for index, name in enumerate(names, 1)]
        item = dict(title='Titolo senza numero ' + '<&>\U0001f600' * 1400,
                    allegati=attachments, _attachment_expected_count=12, _attachment_fetch_ok=True)
        telegram = AsyncMock()

        async def valid_text(**request):
            self.assertLessEqual(visible_units(request['text']), 4096)

        async def valid_document(**request):
            self.assertLessEqual(visible_units(request['caption']), 1024)

        telegram.send_message.side_effect = valid_text
        telegram.send_document.side_effect = valid_document
        self.assertTrue(await bot.send_item_to_chat(telegram, 1, item))
        telegram.send_message.assert_awaited_once()
        telegram.send_media_group.assert_not_awaited()
        sent = telegram.send_document.await_args_list
        self.assertEqual(len(sent), 12)
        for index, (call, attachment) in enumerate(zip(sent, attachments), 1):
            self.assertEqual(call.kwargs['filename'], attachment['filename'])
            self.assertEqual(call.kwargs['document'], attachment['content'])
            self.assertIn(f'Allegato {index}/12', call.kwargs['caption'])
            self.assertEqual('File P7M' in call.kwargs['caption'],
                             attachment['filename'].lower().endswith('.p7m'))
            self.assertTrue(call.kwargs['disable_content_type_detection'])

    async def test_reply_documents_also_preserve_p7m_type_and_advice(self):
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=1), message=AsyncMock())
        item = dict(title='Firma', num_pub='123', allegati=[dict(filename='firma.p7m',
                    content=b'\x30\x82original-signature')], _attachment_expected_count=1,
                    _attachment_fetch_ok=True)
        self.assertTrue(await bot.reply_item(update, item))
        request = update.message.reply_document.await_args.kwargs
        self.assertEqual(request['document'], b'\x30\x82original-signature')
        self.assertIn('File P7M', request['caption'])
        self.assertTrue(request['disable_content_type_detection'])


if __name__ == '__main__':
    unittest.main()
