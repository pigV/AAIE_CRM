"""Transfer pricing uses verified source context, with original GST mechanics."""
import copy
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from tests import test_contract_generation_authority_cutover as fixture_module
import contract_generation_authority as authority


class TransferPublishedFeeAuthorityTests(unittest.TestCase):
    def setUp(self):
        previous_tempdir = tempfile.tempdir
        configured_temp = Path(tempfile.gettempdir()).resolve()
        test_root = configured_temp if configured_temp.is_relative_to(ROOT) else ROOT / 'SAFEBOX/transfer-acceptance-20260907/final-business-gate-r5/published'
        test_root.mkdir(parents=True,exist_ok=True)
        tempfile.tempdir = str(test_root)
        self.addCleanup(setattr,tempfile,'tempdir',previous_tempdir)
        self.fixture = fixture_module.ContractGenerationAuthorityCutoverTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture._published_v2('TransferApplicationContract')
        self.package = authority.load_published_template(self.fixture.conn, 'TransferApplicationContract')
        self.payload = self.fixture._payload('TransferApplicationContract')
        self.source = dict(authority_id=7, transfer_contract_id=8, student_id='T-AUTH-001',
                           source_classification='aaie_existing', rule_version='transfer-source-v1')

    def bind(self, **kw):
        return authority.bind_new_contract(self.package, self.payload, actor={'id': 1, 'role_name': 'Admin'}, **kw)

    def test_existing_and_new_source_base_with_preserved_gst(self):
        for classification, base, final in [('aaie_existing', '1200.00', '1320.00'), ('new_transfer', '2400.00', '2640.00')]:
            self.source['source_classification'] = classification
            self.payload['payment_method'] = '澳洲公户'
            bound = self.bind(trusted_transfer_source=self.source)
            self.assertEqual(bound['commercial']['standard_service_fee'], base)
            self.assertEqual(bound['commercial']['final_fee_amount'], final)
            self.assertEqual(bound['published_template_snapshot']['commercial']['standard_service_fee'], '2400.00')
            self.assertEqual(bound['transfer_fee_authority']['standard_service_fee_ex_gst'], base)
            self.assertEqual(bound['contract_instance']['consulting_and_visa_service_fee'], base)
            authority.validate_transfer_fee_authority(bound)

    def test_missing_or_payload_forged_source_is_hold(self):
        self.payload['trusted_transfer_source'] = self.source
        with self.assertRaises(authority.PublishedTemplateIntegrityError):
            self.bind()

    def test_invalid_or_cross_student_source_is_hold(self):
        for key, value in [('authority_id', 0), ('transfer_contract_id', True), ('student_id', 'OTHER'), ('source_classification', 'unknown'), ('source_classification', []), ('rule_version', '')]:
            source = dict(self.source, **{key: value})
            with self.subTest(key=key), self.assertRaises(authority.PublishedTemplateIntegrityError):
                self.bind(trusted_transfer_source=source)

    def test_discount_gst_free_and_rebind_preserve_source(self):
        self.payload['payment_method'] = '澳洲公户'
        approval = dict(reason='Synthetic authorized', approved_by=1, operator_id=1, approved_at='2026-09-07')
        bound = self.bind(trusted_transfer_source=self.source, discount_approval=dict(approval, discount_amount='100'))
        self.assertEqual(bound['commercial']['final_fee_amount'], '1210.00')
        free = self.bind(trusted_transfer_source=self.source, discount_approval=dict(approval, discount_amount='100'), gst_free_approval=approval)
        self.assertEqual(free['commercial']['final_fee_amount'], '1100.00')
        rebound = authority.rebind_existing_draft(bound, {'service_scope_ids': bound['selected_service_ids']}, request_id='second')
        self.assertEqual(rebound['transfer_fee_authority'], bound['transfer_fee_authority'])
        authority.validate_transfer_fee_authority(rebound)

    def test_rehashed_wrong_fee_and_missing_provenance_rejected(self):
        bound = self.bind(trusted_transfer_source=self.source)
        for tamper in ('fee', 'source'):
            changed = copy.deepcopy(bound)
            if tamper == 'fee':
                changed['commercial']['standard_service_fee'] = '2400.00'
            else:
                changed.pop('transfer_fee_authority')
            changed.pop('bound_snapshot_hash')
            changed['bound_snapshot_hash'] = authority._hash(changed)
            with self.subTest(tamper=tamper), self.assertRaises(authority.PublishedTemplateIntegrityError):
                authority.rebind_existing_draft(changed, {'service_scope_ids': bound['selected_service_ids']}, request_id='third')

    def test_source_identity_changes_request_fingerprint(self):
        first = self.bind(trusted_transfer_source=self.source)
        second = self.bind(trusted_transfer_source=dict(self.source, authority_id=9))
        self.assertNotEqual(first['request_fingerprint'], second['request_fingerprint'])

    def test_non_transfer_rejects_transfer_source_and_keeps_2400(self):
        self.fixture._published_v2('LocalStudentApplicationContract')
        self.package = authority.load_published_template(self.fixture.conn, 'LocalStudentApplicationContract')
        self.payload = self.fixture._payload('LocalStudentApplicationContract')
        with self.assertRaises(authority.PublishedTemplateIntegrityError):
            self.bind(trusted_transfer_source=self.source)
        self.assertEqual(self.bind()['commercial']['standard_service_fee'], '2400.00')

    def test_render_transfer_fee_matches_bound(self):
        self.payload['payment_method'] = '澳洲公户'
        bound = self.bind(trusted_transfer_source=self.source)
        rendered = authority.render_bound_contract(bound)
        self.assertTrue(rendered)


if __name__ == '__main__':
    unittest.main()

