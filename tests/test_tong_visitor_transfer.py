"""Visitor fees use the same authority in initial, legacy and canonical Transfer writes."""
import unittest
from tests import test_tong_word_20261125 as legacy
from tests import test_transfer_revenue_source_mapping as canonical
from revenue.visitor_fee_activation import activate_visitor_600
from revenue.operational_orchestrator import RevenueOperationalOrchestrator

class VisitorTransferTests(unittest.TestCase):
    def seed_visa(self,c,student,cycle):
        c.execute('CREATE TABLE IF NOT EXISTS visa_cases(id INTEGER PRIMARY KEY,application_cycle_id INTEGER)')
        cols={r[1] for r in c.execute('PRAGMA table_info(visa_cases)')}
        for name in ['student_id','visa_subclass','case_type','application_type','updated_at','applicant_name']:
            if name not in cols:c.execute('ALTER TABLE visa_cases ADD COLUMN '+name+' TEXT')
        c.execute("INSERT INTO visa_cases(id,application_cycle_id,student_id,visa_subclass,case_type,application_type,updated_at,applicant_name) VALUES(901,?,?,'600','600 Visitor Visa','New Application','2026-11-25T10:00:00+11:00','Father Test')",(cycle,student))
        activate_visitor_600(c,actor_user_id=1,actor_role='Admin',effective_from='2026-11-25',idempotency_key='current-visitor-policy')

    def verify(self,c):
        service=RevenueOperationalOrchestrator(c,1,'Admin')
        result=service.visa_case_changed(901)
        self.assertEqual(90000,result['visa_fee_cents'])
        self.assertEqual('2026-11-25',result['visa_fee_policy']['source_engagement_date'])
        service.visa_case_changed(901)
        self.assertEqual(1,c.execute("SELECT count(*) FROM revenue_fee_items WHERE milestone_code='visitor-600'").fetchone()[0])

    def test_legacy_transfer_visitor_ignores_old_school_contract_date(self):
        f=legacy.FamilyCaseFeeTests('test_visitor_rule_activation_and_separate_parent_fees_are_idempotent');f.setUp();self.addCleanup(f.doCleanups)
        with f.connect() as c:
            self.seed_visa(c,'S-REVENUE',101)
            self.verify(c)

    def test_canonical_transfer_visitor_retains_source_authority_and_uses_case_date(self):
        f=canonical.TransferRevenueSourceMappingTests('test_explicit_new_parent_is_2400');f.setUp();self.addCleanup(f.doCleanups)
        f.bind('new');self.seed_visa(f.db,'new',99);self.verify(f.db)

    def test_controlled_preview_keeps_visitor_independent_of_school_contract(self):
        from revenue.controlled_onboarding import ControlledOnboardingService
        from revenue.repository import RevenueRepository
        import json
        f=legacy.FamilyCaseFeeTests('test_visitor_rule_activation_and_separate_parent_fees_are_idempotent');f.setUp();self.addCleanup(f.doCleanups)
        with f.connect() as c:
            self.seed_visa(c,'S-REVENUE',100)
            service=ControlledOnboardingService(RevenueRepository(c))
            proposal=f._preview(c)
            cycle=next(x for x in proposal['cycles'] if x['application_cycle_id']==100)
            fee=next(x for x in cycle['additional_fees'] if x['visa_case_id']==901)
            self.assertEqual('proposed',fee['status'])
            self.assertIsNone(fee['contract_id'])
            self.assertEqual('2026-11-25',fee['source_engagement_date'])
            service.apply(proposal,actor_user_id=1,actor_role='Admin',idempotency_key='onboard-independent-visitor')
            item=c.execute("SELECT s.canonical_json FROM revenue_fee_items f JOIN revenue_rule_snapshots s ON s.id=f.rule_snapshot_id WHERE f.milestone_code='visitor-600'").fetchone()
            data=json.loads(item[0])['items'][0]
            self.assertIsNone(data.get('contract_id'));self.assertEqual(901,data['visa_case_id'])
