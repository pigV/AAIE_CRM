import ast
import io
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from pypdf import PdfWriter
from tests.test_transfer_source_routes import handler_method
from student_lifecycle import transfer_contract_authority as authority

class TransferSignedArtifactGateTests(TestCase):
    def test_published_v2_visible_fee_labels_are_accepted(self):
        contract = {
            'contract_type': authority.TRANSFER,
            'consulting_and_visa_service_fee': '1200.00',
            'final_fee_amount': '1200.00',
        }
        text = (
            '标准服务费（不含 GST） AUD 1200.00\n'
            '优惠后服务费（不含 GST） AUD 1200.00\n'
            '最终应付总额 AUD 1200.00'
        )

        authority.validate_fee_text(text, contract)

    def test_published_v2_pdf_line_break_between_label_and_amount_is_accepted(self):
        contract = {
            'contract_type': authority.TRANSFER,
            'consulting_and_visa_service_fee': '2400.00',
            'final_fee_amount': '2400.00',
        }
        text = (
            '标准服务费（不含 GST）\nAUD 2400.00\n'
            '优惠后服务费（不含 GST）\nAUD 2400.00\n'
            '最终应付总额\nAUD 2400.00'
        )

        authority.validate_fee_text(text, contract)

    def test_duplicate_authoritative_fee_labels_are_rejected(self):
        contract = {
            'contract_type': authority.TRANSFER,
            'consulting_and_visa_service_fee': '1200.00',
            'final_fee_amount': '1200.00',
        }
        cases = (
            (
                '标准服务费 AUD 1200.00\n标准服务费 AUD 1200.00\n'
                '最终应付总额 AUD 1200.00'
            ),
            (
                '标准服务费 AUD 1200.00\n最终应付总额 AUD 1200.00\n'
                '最终应付总额 AUD 1200.00'
            ),
        )

        for text in cases:
            with self.subTest(text=text), self.assertRaises(authority.source.SourceAuthorityError):
                authority.validate_fee_text(text, contract)

    def test_prefixed_noncanonical_fee_labels_are_rejected(self):
        contract = {
            'contract_type': authority.TRANSFER,
            'consulting_and_visa_service_fee': '1200.00',
            'final_fee_amount': '1200.00',
        }

        for prefix in ('预计', '非'):
            text = (
                '标准服务费 AUD 1200.00\n'
                f'{prefix}最终应付总额 AUD 1200.00'
            )
            with self.subTest(prefix=prefix), self.assertRaises(authority.source.SourceAuthorityError):
                authority.validate_fee_text(text, contract)

    def test_blank_pdf_and_invalid_pdf_cannot_be_signed(self):
        contract={'contract_type':authority.TRANSFER,'consulting_and_visa_service_fee':'1200','final_fee_amount':'1320'}
        stream=io.BytesIO(); writer=PdfWriter(); writer.add_blank_page(width=612,height=792); writer.write(stream)
        for payload in (stream.getvalue(),b'not PDF'):
            with self.subTest(payload=payload[:8]), self.assertRaises(authority.source.SourceAuthorityError):
                authority.validate_pdf_data(payload,contract)

    def test_both_signed_uploads_reject_bad_content_before_save(self):
        class Hold(ValueError): pass
        def invalid(*args): raise Hold('invalid fee artifact')
        contract={'id':1,'student_id':'S1','contract_type':authority.TRANSFER}
        for name,args in [('contract_center_signed_upload',('POST',1)),('contract_signed_upload',('POST','S1',1))]:
            with self.subTest(route=name):
                namespace={'can_create_contract_center':lambda u:True,'can_generate_contract':lambda u:True,
                    'can_manage_transfer_contract':lambda u:True,'normalize_contract_type':lambda x:x,
                    'db':SimpleNamespace(one=lambda sql,params: {'student_id':'S1','nas_status':'ready'} if 'FROM students' in sql else contract),
                    'require_transfer_contract_signable':lambda c:None,'require_transfer_pdf_bytes':invalid,'Path':Path}
                method=handler_method(name,namespace)
                with self.assertRaises(Hold):
                    method(SimpleNamespace(user={'id':1},uploaded_for=lambda:SimpleNamespace(filename='signed.pdf',data=b'bad')), *args)

    def test_status_change_checks_actual_artifact_before_database_write(self):
        class Hold(ValueError): pass
        def invalid(*args): raise Hold('invalid fee artifact')
        for name,args in [('contract_center_status_update',('POST',1)),('contract_status_update',('POST','S1',1))]:
            with self.subTest(route=name):
                namespace={'can_create_contract_center':lambda u:True,'normalize_contract_status':lambda x:x,
                    'normalize_contract_type':lambda x:x,'can_manage_transfer_contract':lambda u:True,
                    'CONTRACT_STATUS_LABELS':{'FullyExecuted':'Done'},
                    'db':SimpleNamespace(one=lambda *args:{'id':1,'contract_type':authority.TRANSFER}),
                    'require_transfer_contract_signable':lambda c:None,'require_transfer_status_artifact':invalid}
                method=handler_method(name,namespace)
                with self.assertRaises(Hold):
                    method(SimpleNamespace(user={'id':1},require=lambda x:True,form={'contract_status':'FullyExecuted'}),*args)
