"""Load the actual parser functions without importing CRM or opening a database."""
import ast
from datetime import date, datetime
import html
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
from xml.etree import ElementTree as ET

from fields import parseAustralianDate, display_date_value

ROOT = Path(__file__).resolve().parents[1]


def parser_namespace():
    tree = ast.parse((ROOT / 'app.py').read_text())
    start = next(n.lineno for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'normalize_offer_date')
    end = next(n.lineno for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'crm_display_date_input')
    nodes = [n for n in tree.body if start <= n.lineno < end and isinstance(n, (ast.FunctionDef, ast.Assign))]
    ns = dict(re=re, Path=Path, date=date, datetime=datetime, io=io, json=json,
              os=os, subprocess=subprocess, tempfile=tempfile, zipfile=zipfile, ET=ET,
              parseAustralianDate=parseAustralianDate, display_date_value=display_date_value,
              esc=lambda x: html.escape(str(x or ''), quote=True),
              APP_DIR=ROOT/'STAGING_DATA', SOURCE_DIR=ROOT, STAGING=True)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT/'app.py'), 'exec'), ns)
    return ns


def upload(text, filename='sample.txt', content_type='text/plain'):
    return SimpleNamespace(filename=filename, content_type=content_type,
                           data=text.encode() if isinstance(text, str) else text)


class ApplicationExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = parser_namespace()

    def offer(self, language='Sample English College'):
        return ('Victorian College\n' + 'Information about the offer\n'*90 +
                'Course Details\nSchool Name and Year Level\nEnglish Language Centre:\n' +
                language + '\nSchool:\nEast Doncaster Secondary College\nAddress:\n'
                '20 George Street\nYear Level:\n10\nAccommodation and Welfare Option\n')

    def test_offer_prefers_course_details_after_page_two(self):
        details = self.ns['infer_offer_details'](upload(self.offer()))
        self.assertEqual(details['school_name'], 'East Doncaster Secondary College')
        self.assertEqual(details.get('language_school_name'), 'Sample English College')

    def test_empty_language_does_not_consume_main_school_label_or_value(self):
        details = self.ns['infer_offer_details'](upload(self.offer('')))
        self.assertEqual(details.get('language_school_name'), '')
        self.assertEqual(details['school_name'], 'East Doncaster Secondary College')

    def test_offer_ocr_cross_column_prose_is_not_a_school(self):
        text = self.offer().replace('School:\nEast Doncaster', 'School:\nor distance education.\nEast Doncaster')
        self.assertEqual(self.ns['infer_offer_details'](upload(text))['school_name'], 'East Doncaster Secondary College')

    def test_review_exposes_language_as_separate_editable_field(self):
        data = self.ns['extraction_review_data'](upload(self.offer()), 'Offer', 'Offer')
        self.assertEqual(data['fields'].get('language_school_name', {}).get('value'), 'Sample English College')
        rendered = self.ns['extraction_review_html'](0, 'Offer', data)
        self.assertIn('name="extract_language_school_name_0"', rendered)

    def test_medibank_columns_with_empty_passport_and_variant_cover_title(self):
        text = '''Your Details
Your Cover:
Membership Number:
First Name:
Last Name:
Date of Birth:
Scale:
Start date:
Valid to:
Passport No:
Dependants:
Medibank Essentials OSHC
8256495
Test
Example
29/10/2013
Single
21/01/2025
20/03/2027
None'''
        details = self.ns['infer_coe_oshc_details'](upload(text), 'OSHC')
        self.assertEqual(details['oshc_policy_number'], '8256495')
        self.assertEqual(details['oshc_commencement_date'], '2025-01-21')
        self.assertEqual(details['oshc_expiry_date'], '2027-03-20')

    def test_caaw_same_row_dates_do_not_read_signature_date(self):
        text = '''D. COURSE DETAILS
Course Start Date: 13/07/2026 Course End Date: 21/12/2029
E. WELFARE DETAILS
Nominated Welfare Period
Start Date: 06/07/2026 End Date: 31/12/2027
Type of Stay: Homestay
F. PROVIDER DETAILS
Provider Name: Department of Education [00861K]
Date: 11/03/2026'''
        details = self.ns['infer_coe_oshc_details'](upload(text), 'CAAW')
        self.assertEqual(details['caaw_welfare_start_date'], '2026-07-06')
        self.assertEqual(details['caaw_welfare_end_date'], '2027-12-31')

    def test_medibank_mixed_inline_and_separated_columns(self):
        text = '''Your Details
Your Cover:
Medibank Essentials OSHC
Membership Number: 82564695
First Name:
Last Name:
Date of Birth:
Scale:
Start date:
Valid to:
Passport No:
Dependants:
Test
Example
29/10/2013
Single
21/01/2025
20/03/2027
None'''
        details = self.ns['infer_coe_oshc_details'](upload(text), 'OSHC')
        self.assertEqual(details['oshc_commencement_date'], '2025-01-21')
        self.assertEqual(details['oshc_expiry_date'], '2027-03-20')

    def test_missing_welfare_end_never_uses_provider_signature_date(self):
        text = '''Nominated Welfare Period
Start Date: 06/07/2026
End Date:
F. PROVIDER DETAILS
Provider Name: Department of Education
Date: 11/03/2026'''
        details = self.ns['infer_coe_oshc_details'](upload(text), 'CAAW')
        self.assertEqual(details['caaw_welfare_end_date'], '')

    def test_ocr_failure_is_reported_in_review_metadata(self):
        from reportlab.pdfgen import canvas
        data = io.BytesIO(); pdf = canvas.Canvas(data); pdf.showPage(); pdf.save()
        u = upload(data.getvalue(), 'scan.pdf', 'application/pdf')
        with patch.dict(self.ns, ocr_pdf_text=lambda *a, **kw: ''):
            self.ns['extract_uploaded_text'](u)
        meta = self.ns['upload_text_extraction_meta'](u)
        self.assertTrue(meta['ocr_fallback_used'])
        self.assertEqual(meta['source'], 'pdf_ocr')
        self.assertFalse(meta['extracted_text_available'])

    def test_upload_is_extracted_once_across_detection_and_review(self):
        from reportlab.pdfgen import canvas
        data = io.BytesIO(); pdf = canvas.Canvas(data); pdf.drawString(10, 20, 'Example High School'); pdf.save()
        u = upload(data.getvalue(), 'offer.pdf', 'application/pdf')
        from pypdf import PdfReader
        with patch('pypdf.PdfReader', wraps=PdfReader) as reader:
            self.ns['extract_uploaded_text'](u)
            self.ns['extraction_review_data'](u, 'Offer', 'Offer')
        self.assertEqual(reader.call_count, 1)

    def test_staging_ocr_uses_prepared_executable_and_reports_failures(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'STAGING_DATA/tmp') as folder:
            root = Path(folder)
            (root/'tools').mkdir()
            helper = root/'tools/document-vision-ocr'
            helper.write_bytes(b'fixture executable')
            helper.chmod(0o700)
            diagnostics = {}
            with patch.dict(self.ns, APP_DIR=root):
                # The service sandbox correctly denies executing this temporary
                # fixture path. This unit test replaces process execution; real
                # executable authority is checked by the screenshot integration.
                with patch('document_ocr.os.access', return_value=True), patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='OCR text', stderr='')) as run:
                    text = self.ns['ocr_image_text'](b'image', '.png', diagnostics=diagnostics)
            self.assertEqual(text, 'OCR text')
            self.assertEqual(run.call_args.args[0][0], str(helper))
            self.assertEqual(diagnostics['status'], 'ok')

    def test_ocr_review_reports_missing_engine_instead_of_blank_document(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'STAGING_DATA/tmp') as folder:
            with patch.dict(self.ns, APP_DIR=Path(folder)):
                data = self.ns['extraction_review_data'](upload(b'image', 'scan.png', 'image/png'), 'Offer', 'Offer')
        self.assertFalse(data['text_found'])
        self.assertIn('未就绪', data['source'])
        self.assertEqual(data['text_extraction']['ocr_status'], 'engine_unavailable')

    def test_language_persistence_is_scoped_and_transactional(self):
        self.assertIn('save_offer_language_school', self.ns)
        with sqlite3.connect(':memory:') as conn:
            conn.executescript('CREATE TABLE offers(id INTEGER PRIMARY KEY, student_id TEXT, school_name TEXT); INSERT INTO offers VALUES(1,"S1","Main School");')
            migration = ROOT/'migrations/20261031_043_offer_language_school.sql'
            conn.executescript(migration.read_text())
            self.ns['save_offer_language_school'](conn, 1, 'S1', 'Language College')
            conn.commit()
            self.ns['save_offer_language_school'](conn, 1, 'S2', 'Wrong College')
            self.assertEqual(conn.execute('SELECT language_school_name FROM offers').fetchone()[0], 'Language College')
            self.ns['save_offer_language_school'](conn, 1, 'S1', '')
            self.assertEqual(conn.execute('SELECT language_school_name FROM offers').fetchone()[0], '')
            conn.rollback()
            self.assertEqual(conn.execute('SELECT language_school_name FROM offers').fetchone()[0], 'Language College')


if __name__ == '__main__':
    unittest.main()
