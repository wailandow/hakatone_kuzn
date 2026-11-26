from datetime import date
from decimal import Decimal
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app.pdf.kp_generator import QuoteLine, generate_pdf


def font_dir():
    for folder in (os.getenv('STAGE4_TEST_FONT_DIR'), '/usr/share/fonts/truetype/dejavu'):
        if folder and all((Path(folder) / f'DejaVuSans{suffix}.ttf').is_file() for suffix in ('', '-Bold')):
            return Path(folder)
    return None


class PdfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lines = [QuoteLine('RU-1', 'Российское ПО', 2, Decimal('120.10'), True),
                      QuoteLine('VAT-2', 'Лицензия < А и Б', 3, Decimal('120'), False)]

    def test_cyrillic_quote_and_decimal_vat_extract_from_pdf(self):
        fonts, extractor = font_dir(), shutil.which('pdftotext')
        if not fonts or not extractor:
            self.skipTest('requires DejaVu fonts (STAGE4_TEST_FONT_DIR) and pdftotext')
        path = self.root / 'kp_test.pdf'
        total = generate_pdf(path, fonts, self.lines, Decimal('5'), 'Предоплата 100%',
                             'Поставка в течение 5 дней', today=date(2026, 11, 26))
        self.assertEqual(total, Decimal('570.20'))
        text = subprocess.check_output([extractor, '-enc', 'UTF-8', str(path), '-']).decode('utf-8-sig')
        text = ' '.join(text.split())
        for expected in ('Уважаемый заказчик!', 'ПАО «Софтлайн»', 'Российское ПО', 'Лицензия < А и Б',
                         '114.10', 'Без НДС (реестр ПО)', '57.00', 'Итого', '570.20', 'Скидка 5%',
                         '10.12.2026', 'Предоплата 100%', 'Поставка в течение 5 дней', 'ИИ-ассистентом'):
            self.assertIn(expected, text)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_failed_atomic_publish_does_not_leave_partial_file(self):
        if not font_dir():
            self.skipTest('requires DejaVu fonts (STAGE4_TEST_FONT_DIR)')
        path = self.root / 'kp_test.pdf'
        with patch('app.pdf.kp_generator.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                generate_pdf(path, font_dir(), self.lines, Decimal('0'), 'Оплата', 'Поставка')
        self.assertEqual(list(self.root.iterdir()), [])
