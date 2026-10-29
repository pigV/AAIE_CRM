import importlib.util
from pathlib import Path
from tests import test_transfer_source_authority as support

class TransferContractFeeGateTests(support.SourceAuthorityTests):
    def setUp(self):
        super().setUp()
        path=Path(__file__).resolve().parents[1]/'student_lifecycle/transfer_contract_authority.py'
        self.assertTrue(path.exists(), 'Transfer source pricing/signability gate required')
        from student_lifecycle import transfer_contract_authority
        self.fee=transfer_contract_authority
        root=Path(__file__).resolve().parents[1]
        self.db.executescript((root/'migrations/20260829_032_revenue_commission_v1.sql').read_text())
        from revenue.client_fee_rules import FeeRuleAuthorityService
        from revenue.repository import RevenueRepository
        FeeRuleAuthorityService(RevenueRepository(self.db)).initialize_2026(actor_user_id=1,actor_role='Admin',idempotency_key='contract-rules')

    def test_existing_fee_is_1200_and_company_account_adds_gst(self):
        self.existing()
        row=self.fee.commercials(self.db, 'existing', 200, payment_method='微信')
        self.assertEqual(('1200.00','1200.00'), (row['base_fee'],row['final_fee']))
        self.assertEqual('1320.00', self.fee.commercials(self.db,'existing',200,payment_method='澳洲公户')['final_fee'])

    def test_new_fee_is_2400_and_missing_source_has_no_price(self):
        with self.assertRaises(self.svc.SourceAuthorityError):
            self.fee.commercials(self.db,'new',100,payment_method='微信')
        self.save()
        self.assertEqual('2640.00',self.fee.commercials(self.db,'new',100,payment_method='澳洲公户')['final_fee'])

    def test_changed_evidence_stops_precontract_pricing(self):
        self.save()
        self.db.execute("UPDATE parent_form_tokens SET invalidated_at='later' WHERE token='new-intake'")
        with self.assertRaises(self.svc.SourceAuthorityError):
            self.fee.commercials(self.db,'new',100,payment_method='微信')

    def test_published_db_payment_and_discount_match_frozen_document(self):
        from tests import test_transfer_published_fee_authority as published
        fixture = published.TransferPublishedFeeAuthorityTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.existing()
        source = self.svc.resolve_for_contract(self.db, 'existing', 200)
        fixture.source = self.fee.trusted_binding(source)
        fixture.payload['student_snapshot']['student_id'] = 'existing'
        fixture.payload['payment_method'] = '澳洲公户'
        bound = fixture.bind(trusted_transfer_source=fixture.source)
        contract = dict(id=200, student_id='existing',contract_type='TransferApplicationContract',
            consulting_and_visa_service_fee='1200.00',discount_amount='0.00',final_fee_amount='1320.00',
            service_fee_currency='澳洲公户',contract_file_path='synthetic-contract.docx')
        self.fee.require_signable(self.db,contract,bound)
        for field,value in [('discount_amount','100'),('service_fee_currency','微信'),('final_fee_amount','2400')]:
            with self.subTest(field=field),self.assertRaises(self.svc.SourceAuthorityError):
                self.fee.require_signable(self.db,dict(contract,**{field:value}),bound)

    def test_contract_rule_absence_never_uses_literal_base(self):
        self.existing()
        self.db.execute('ALTER TABLE revenue_fee_rule_versions RENAME TO unavailable_fee_rules')
        with self.assertRaises(self.svc.SourceAuthorityError):
            self.fee.commercials(self.db,'existing',200,payment_method='微信')

