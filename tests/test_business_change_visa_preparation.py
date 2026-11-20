import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class VisaLetterBytePreparationTests(unittest.TestCase):
    def test_pdf_uses_supplied_bytes_without_opening_a_path(self):
        from visa_legacy_document_adapter import read_legacy_letter_bytes
        def reader(source):
            self.assertIsInstance(source,io.BytesIO)
            self.assertEqual(b'synthetic bytes',source.read())
            return SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda:'Synthetic letter')])
        with patch.object(Path,'read_bytes',side_effect=AssertionError('disk read inside byte preparation')):
            self.assertEqual('Synthetic letter',read_legacy_letter_bytes('synthetic.pdf',b'synthetic bytes',reader))

    def test_utf8_validation_and_corrupt_pdf_keep_existing_validation_errors(self):
        from visa_legacy_document_adapter import read_legacy_letter_bytes
        from pypdf import PdfReader
        self.assertEqual('Synthetic text',read_legacy_letter_bytes('synthetic.txt',b'Synthetic text',PdfReader))
        for name,data in [('synthetic.txt',b'\xff'),('synthetic.pdf',b'%PDF-1.7\ninvalid synthetic')]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):read_legacy_letter_bytes(name,data,PdfReader)

    def test_programming_errors_are_not_hidden_as_empty_extraction(self):
        from visa_legacy_document_adapter import read_legacy_letter_bytes
        def reader(source):raise RuntimeError('synthetic programming failure')
        with self.assertRaises(RuntimeError):read_legacy_letter_bytes('synthetic.pdf',b'test',reader)


class VisaRegistrationPreparationPortTests(unittest.TestCase):
    def test_registration_uses_preverified_bytes_and_prepared_text_without_disk_reread(self):
        from tests import test_visa_legacy_document_adapter as fixture
        from visa_workspace_candidate import VisaWorkspaceCandidateMixin
        import hashlib
        f=fixture.LegacyTests();f.setUp();self.addCleanup(f.doCleanups)
        h=object.__new__(VisaWorkspaceCandidateMixin);h.user={'id':1}
        h._visa_runtime=lambda:{'db':SimpleNamespace(),'now_iso':lambda:'2026-11-19T00:00:00+00:00'}
        h._visa_document_path=lambda case,document:f.path
        h.visa_material_link_document_expr=lambda alias:f'{alias}.student_document_id'
        for definition in ('document_category TEXT','document_type TEXT','used_in_this_case INTEGER'):
            f.c.execute('ALTER TABLE visa_case_document_links ADD COLUMN '+definition)
        def forbidden(*args):raise AssertionError('letter or hash reread under writer')
        h._visa_read_text=forbidden
        with patch.object(Path,'read_bytes',side_effect=forbidden),patch('visa_documents_candidate.save_extraction') as extraction:
            h._visa_register_uploaded_document({'id':1,'student_id':'S1'},1,connection=f.c,
                prepared_bytes=b'synthetic',prepared_text='Synthetic prepared text')
        self.assertEqual(hashlib.sha256(b'synthetic').hexdigest(),f.c.execute('SELECT sha256 FROM visa_case_documents').fetchone()[0])
        self.assertEqual('Synthetic prepared text',extraction.call_args.kwargs['text'])
