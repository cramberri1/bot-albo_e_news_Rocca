"""Only the verified public Halley P7M advisory may be ignored as non-document."""
import unittest

from bs4 import BeautifulSoup

from test_attachment_primitives import bot, detail, reference


ADVISORY = '''<div class="alert alert-info mt-5">
Per leggere i file firmati digitalmente (estensione '.p7m') è necessario
aver installato il software <a
href="https://www.firma.infocert.it/installazione/installazione_DiKe.php"
target="_blank">Dike (download)</a></div>'''


class SearchDetailAdvisoryTests(unittest.TestCase):
    def inspect(self, attachments):
        return bot._strict_attachment_references(
            BeautifulSoup(detail(attachments=attachments), 'html.parser'))

    def test_verified_p7m_advisory_allows_confirmed_zero(self):
        references, errors = self.inspect(ADVISORY)
        self.assertEqual(references, [])
        self.assertEqual(errors, [])

    def test_error_alert_never_implies_zero(self):
        for classes in ('alert', 'alert alert-info', 'alert alert-danger'):
            with self.subTest(classes=classes):
                references, errors = self.inspect(
                    f'<div class="{classes}">Errore caricamento allegati.</div>')
                self.assertEqual(references, [])
                self.assertTrue(errors)

    def test_plain_document_link_inside_alert_is_not_advisory(self):
        for classes in ('alert', 'alert alert-info'):
            with self.subTest(classes=classes):
                references, errors = self.inspect(
                    f'<div class="{classes}"><a href="/documento.pdf">documento.pdf</a></div>')
                self.assertEqual(references, [])
                self.assertTrue(errors)

    def test_modified_advisory_or_added_controls_fail_closed(self):
        invalid = [
            ADVISORY.replace('installazione_DiKe.php', 'documento.pdf'),
            ADVISORY.replace('alert-info', 'alert-danger'),
            ADVISORY.replace('è necessario', 'non è possibile'),
            ADVISORY.replace('target="_blank"', 'onclick="MC95(1)"'),
            ADVISORY.replace('target="_blank"', 'download'),
            ADVISORY.replace('</div>', '<button></button></div>'),
        ]
        for markup in invalid:
            with self.subTest(markup=markup):
                _, errors = self.inspect(markup)
                self.assertTrue(errors)

    def test_error_alongside_valid_document_rejects_partial_acquisition(self):
        references, errors = self.inspect(
            reference() + '<div class="alert alert-danger">Allegati non caricati.</div>')
        self.assertEqual(len(references), 1)
        self.assertTrue(errors)


if __name__ == '__main__':
    unittest.main()
