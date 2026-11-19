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

    def test_acknowledge_hsp_uses_issue_date_not_dob_and_total_deposit(self):
        text = ('Student LeƩer of Oﬀer and Student Acceptance Agreement\n'
                'Oﬀer/Student ID: 95833 DOB: 11/09/2011 Date of Issue: 03/08/2026\n'
                'Acknowledge Education Pty Ltd, trading as Melbourne Language Centre\n'
                'CRICOS Code Course Name Mode of Study Start Date End Date\n'
                '080627B HSP English for Secondary Schools C 31/08/2026 25/09/2026 Melbourne 4 Week $1,700.00\n'
                'Total TuiƟon Fees $1,700.00\n'
                'Deposit required to conﬁrm acceptance $2,280.00\n')
        details = self.ns['infer_offer_details'](upload(text))
        self.assertEqual(details['school_name'], 'Melbourne Language Centre')
        self.assertEqual(details['offer_date'], '2026-08-03')
        self.assertEqual(details['course_start_date'], '2026-08-31')
        self.assertEqual(float(details['deposit_amount']), 2280)
        self.assertEqual(details['school_student_number'], '95833')

    def test_acknowledge_hsp_does_not_guess_missing_or_conflicting_fields(self):
        parse = self.ns['infer_acknowledge_hsp_offer_details']
        self.assertIsNone(parse('Melbourne Language Centre DOB: 11/09/2011'))
        text = ('Acknowledge Education Pty Ltd trading as Melbourne Language Centre\n'
                'Offer/Student ID: 95833 DOB: 11/09/2011\n'
                '080627B HSP English for Secondary Schools C 31/08/2026\n'
                '080627B HSP English for Secondary Schools C 07/09/2026\n'
                'Deposit required to confirm acceptance $2,280.00\n'
                'Deposit required to confirm acceptance $2,500.00\n')
        details = parse(text)
        self.assertEqual(details['offer_date'], '')
        self.assertEqual(details['course_start_date'], '')
        self.assertEqual(details['deposit_amount'], '')

    def test_acknowledge_hsp_requires_both_course_dates_in_valid_order(self):
        parse = self.ns['infer_acknowledge_hsp_offer_details']
        header = 'Acknowledge Education Pty Ltd trading as Melbourne Language Centre\n'
        for dates in ('25/09/2026', '25/09/2026 31/08/2026', '31/02/2026 25/09/2026'):
            details = parse(header + '080627B HSP English for Secondary Schools C ' + dates)
            self.assertEqual(details['course_start_date'], '')

    def test_nsw_offer_uses_language_program_and_its_start_date(self):
        text = (
            "COURSE DETAILS\n"
            "Program name and location High School Preparation Delivered through the High School Curriculum at Central Sydney Intensive\n"
            "English High School\n57-77 Mitchell Road\n"
            "Start date 21-Jul-2026 Finish date 17-Dec-2026 Term(s) 2\n"
            "Program name and location Mosman High School - Year 9 to 10\n"
            "Start date 03-Feb-2027 Finish date 18-Dec-2028 Term(s) 8\n"
        )
        details = self.ns["infer_nsw_public_offer_details"](upload(text), text, text.splitlines())
        self.assertEqual(details.get("language_school_name"), "Central Sydney Intensive English High School")
        self.assertEqual(details.get("course_start_date"), "2026-07-21")

    def test_nsw_program_location_preserves_iec_and_full_main_school_name(self):
        parse = self.ns['nsw_coe_cover_letter_school_name']
        self.assertEqual(parse('High School Preparation Delivered through the High School Curriculum at IEC Marrickville\nNorthcote Street\nMarrickville NSW 2204'), 'IEC Marrickville')
        self.assertEqual(parse('Dulwich High School of Visual Arts and Design - Year 8 to 10\nSeaview St\nDulwich Hill NSW 2203'), 'Dulwich High School of Visual Arts and Design')
        self.assertEqual(parse('High School Preparation Delivered through the High School Curriculum at Central Sydney Intensive\nEnglish High School\n57-77 Mitchell Road'), 'Central Sydney Intensive English High School')

    def test_nsw_ocr_language_value_precedes_split_program_label(self):
        text = ('COURSE DETAILS\nCRICOS course Junior Secondary (Years 7 to 10)\n'
                'High School Preparation Delivered through the High School Curriculum at IEC Marrickville\n'
                'Program name Northcote Street\nand location\nMarrickville NSW 2204\n'
                'Start date 03-Feb-2027 Finish date 02-Jul-2027 Term(s) 2\n'
                'Program name Dulwich High School of Visual Arts and Design - Year 8 to 10\n'
                'and location Seaview St\nDulwich Hill NSW 2203\n'
                'Start date 20-Jul-2027 Finish date 18-Dec-2029 Term(s) 10\n')
        details = self.ns['infer_nsw_public_offer_details'](upload(text), text, text.splitlines())
        self.assertEqual(details['language_school_name'], 'IEC Marrickville')
        self.assertEqual(details['school_name'], 'Dulwich High School of Visual Arts and Design')
        self.assertEqual(details['course_start_date'], '2027-02-03')

    def test_nsw_document_name_survives_abbreviated_catalogue_match(self):
        class Catalogue:
            def all(self, query):
                return [{'school_name': 'Dulwich High School'}]
        from difflib import SequenceMatcher
        with patch.dict(self.ns, db=Catalogue(), SequenceMatcher=SequenceMatcher):
            name, score, status = self.ns['nsw_offer_school_name']([
                {'type': 'main', 'school': 'Dulwich High School of Visual Arts and Design'}
            ])
        self.assertEqual(name, 'Dulwich High School of Visual Arts and Design')
        self.assertEqual(status, 'Needs Review')

    def test_nsw_same_line_address_does_not_become_part_of_school_name(self):
        parse = self.ns['nsw_coe_cover_letter_school_name']
        self.assertEqual(parse('Mosman High School Avenue Road Mosman NSW 2088'), 'Mosman High School')
        self.assertEqual(parse('Dulwich High School of Visual Arts and Design'),
                         'Dulwich High School of Visual Arts and Design')

    def test_nsw_ocr_program_values_on_first_label_line(self):
        text = ('NSW Department of Education\nCOURSE DETAILS\n'
                'Program name High School Preparation Delivered through the High School Curriculum at IEC Marrickville\n'
                'Northcote Street\nand location\nMarrickville NSW 2204\n'
                'Start date 03-Feb-2027 Finish date 02-Jul-2027 Term(s) 2\n'
                'Program name Dulwich High School of Visual Arts and Design - Year 8 to 10\n'
                'and location Seaview St\nDulwich Hill NSW 2203\n'
                'Start date 20-Jul-2027 Finish date 18-Dec-2029 Term(s) 10\n')
        details = self.ns['infer_offer_details'](upload(text))
        self.assertEqual(details['school_name'], 'Dulwich High School of Visual Arts and Design')
        self.assertEqual(details['language_school_name'], 'IEC Marrickville')
        self.assertEqual(details['course_start_date'], '2027-02-03')

    def test_victorian_offer_reads_course_dates_range(self):
        text = self.offer("Not applicable").replace(
            "School Name and Year Level", "School Name and Year Level Course Course Dates"
        ).replace("Year Level:\n10\n", "Year Level:\n10\n019048F Secondary\n"
                  "27 January 2027\nto\n21 December 2029\n124 weeks\n")
        details = self.ns["infer_offer_details"](upload(text))
        self.assertEqual(details["course_start_date"], "2027-01-27")
        self.assertEqual(details["language_school_name"], "")

    def test_nsw_cover_letter_distinguishes_provider_code_from_name(self):
        text = ("Confirmation of Enrolment Cover Letter\n"
                "NSW Department of Education | CRICOS Provider Code: 00588M\n"
                "COURSE DETAILS\nProgram name and location Example High School\n"
                "Start date 03-Feb-2027 Finish date 18-Dec-2028\n")
        details = self.ns["infer_coe_oshc_details"](
            upload(text, "CoE Cover Letter.pdf"), "LetterOfConfirmation"
        )
        self.assertEqual(details["coe_provider"], "NSW Department of Education")
        self.assertEqual(details["letter_main_course_name"], "Example High School")

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

    def hybrid_pdf(self):
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
        from PIL import Image, ImageDraw

        data = io.BytesIO()
        pdf = canvas.Canvas(data)
        pdf.setFont("Helvetica", 11)
        pdf.drawString(50, 780, "Victorian College")
        pdf.drawString(50, 760, "International Student Offer")
        pdf.drawString(50, 740, "Offer Date: 26/02/2026")
        pdf.drawString(50, 720, "Offer Expiry Date: 05/03/2026")
        pdf.drawString(50, 700, "Student Name: Test Example")
        pdf.showPage()
        image = Image.new("RGB", (1200, 500), "white")
        draw = ImageDraw.Draw(image)
        draw.text((20, 20), "Course Details", fill="black")
        draw.text((20, 60), "School:", fill="black")
        draw.text((20, 100), "East Doncaster Secondary College", fill="black")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        pdf.drawImage(ImageReader(buffer), 40, 500, width=520, height=217)
        pdf.showPage()
        pdf.save()
        return data.getvalue()

    def test_hybrid_pdf_ocrs_only_pages_without_text_layer(self):
        u = upload(self.hybrid_pdf(), 'offer.pdf', 'application/pdf')
        calls = []

        def fake_ocr_pages(data, page_indexes, diagnostics=None):
            calls.append(list(page_indexes))
            if diagnostics is not None:
                diagnostics['status'] = 'ok'
            return {index: ('Course Details\nSchool:\nEast Doncaster Secondary College\n'
                            'English Language Centre:\nAddress:\n20 George Street\nYear Level:\n10')
                    for index in page_indexes}

        with patch.dict(self.ns, ocr_pdf_page_texts=fake_ocr_pages):
            text = self.ns['extract_uploaded_text'](u)
            meta = self.ns['upload_text_extraction_meta'](u)
            details = self.ns['infer_offer_details'](u)
        self.assertEqual(calls, [[1]])
        self.assertIn('East Doncaster Secondary College', text)
        self.assertEqual(details['school_name'], 'East Doncaster Secondary College')
        self.assertEqual(meta['source'], 'pdf_text_layer_and_ocr')
        self.assertTrue(meta['ocr_fallback_used'])
        self.assertEqual(meta['text_layer_pages'], 1)
        self.assertEqual(meta['unread_pages'], 0)

    def test_mixed_signed_pdf_rejects_garbled_course_page_and_cid_fallback(self):
        u = upload(b'%PDF fixture', 'signed-offer.pdf', 'application/pdf')
        garbled = ('\x03&2856( \x03 ABcd \x04 XYzt ' * 80)
        readable = 'Student signature Anyang Zhao Date 17/08/2026\nParent signature Date 17/08/2026'
        recovered = 'COURSE DETAILS\nProgram name and location IEC Marrickville\nStart date 03-Feb-2027 Finish date 02-Jul-2027'
        calls = []
        def ocr(data, indexes, diagnostics=None):
            calls.append(indexes)
            return {0: recovered}
        with patch.dict(self.ns,
                        pdf_native_page_texts=lambda data: [garbled, readable],
                        pdfplumber_page_texts=lambda *a: {0: '(cid:49)(cid:54)' * 100},
                        ocr_pdf_page_texts=ocr):
            text = self.ns['extract_uploaded_text'](u)
        self.assertEqual(calls, [[0]])
        self.assertIn(recovered, text)
        self.assertIn(readable, text)
        self.assertNotIn('(cid:', text)
        self.assertEqual(self.ns['upload_text_extraction_meta'](u)['unread_pages'], 0)

    def test_text_only_pdf_never_runs_ocr(self):
        from reportlab.pdfgen import canvas

        data = io.BytesIO()
        pdf = canvas.Canvas(data)
        pdf.setFont("Helvetica", 11)
        for offset, line in enumerate(('Victorian College', 'International Student Offer',
                                       'Offer Date: 26/02/2026', 'Offer Expiry Date: 05/03/2026',
                                       'Student Name: Test Example', 'Year Level: 10')):
            pdf.drawString(50, 780 - offset * 20, line)
        pdf.showPage()
        pdf.save()
        u = upload(data.getvalue(), 'offer.pdf', 'application/pdf')
        calls = []
        with patch.dict(self.ns,
                        ocr_pdf_page_texts=lambda *a, **kw: calls.append(a) or {},
                        ocr_pdf_text=lambda *a, **kw: calls.append(a) or ''):
            self.ns['extract_uploaded_text'](u)
        self.assertEqual(calls, [])
        meta = self.ns['upload_text_extraction_meta'](u)
        self.assertFalse(meta['ocr_fallback_used'])
        self.assertEqual(meta['source'], 'pdf_text_layer')
        self.assertEqual(meta['unread_pages'], 0)

    def test_pdf_falls_back_to_second_text_parser_when_pypdf_cannot_read(self):
        u = upload(b'%PDF broken', 'scan.pdf', 'application/pdf')
        with patch.dict(self.ns,
                        pdf_native_page_texts=lambda data, max_pages=12: [],
                        pdfplumber_all_page_texts=lambda data, max_pages=12: [
                            'Course Details\nSchool:\nEast Doncaster Secondary College\n'
                            'Address:\n20 George Street\nYear Level:\n10']):
            text = self.ns['extract_uploaded_text'](u)
        self.assertIn('East Doncaster Secondary College', text)
        self.assertEqual(self.ns['upload_text_extraction_meta'](u)['source'], 'pdf_text_layer')

    def test_offer_page_texts_reuses_extracted_pages(self):
        u = upload(b'%PDF-1.4 stub', 'offer.pdf', 'application/pdf')
        setattr(u, '_pdf_page_texts', ['Page one offer text', 'Page two SCHEDULE OF FEES text'])
        pages = self.ns['nsw_offer_page_texts'](u, 'SCHEDULE OF FEES fallback')
        self.assertEqual(pages, ['Page one offer text', 'Page two SCHEDULE OF FEES text'])

    def test_nsw_offer_falls_back_to_split_text_when_pages_are_empty(self):
        u = upload(b'%PDF-1.4 stub', 'offer.pdf', 'application/pdf')
        setattr(u, '_pdf_page_texts', ['', ''])
        pages = self.ns['nsw_offer_page_texts'](u, 'Main offer body\nSCHEDULE OF FEES\nDeposit')
        self.assertEqual(len(pages), 2)
        self.assertIn('Main offer body', pages[0])
        self.assertIn('SCHEDULE OF FEES', pages[1])

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
