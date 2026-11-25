"""Regression for signed-off Tong feedback: authority, not status overrides."""
import json
import sqlite3
from tests.test_revenue_client_payment_v1 import RevenueClientPaymentV1Tests
from tests.test_transfer_v2_authoritative_business_records import TransferV2AuthoritativeBusinessRecordTests, NOW
from student_lifecycle.transfer_v2_business_records import bind_authoritative_offer
from student_lifecycle.transfer_v2_projection import load_transfer_v2_projection

class IndependentVisaPaymentTests(RevenueClientPaymentV1Tests):
    def setUp(self):
        super().setUp()
        from pathlib import Path
        self.connection.execute("ALTER TABLE visa_cases ADD COLUMN archived_at TEXT")
        self.connection.execute("ALTER TABLE visa_cases ADD COLUMN student_id TEXT")
        for name in ('20261125_056_visa_payment_authority.sql','20261125_057_visa_allocation_recheck.sql'):
            self.connection.executescript((Path(__file__).resolve().parents[1]/'migrations'/name).read_text())

    def seed_independent(self, *, cycle=100):
        self.connection.execute("ALTER TABLE visa_cases ADD COLUMN visa_subclass TEXT")
        self.connection.execute("INSERT INTO visa_cases(id,application_cycle_id,student_id,visa_subclass) VALUES(901,?,'S-REVENUE','500')",(cycle,))
        item={'visa_case_id':901,'application_cycle_id':100,'milestone_code':'student-visa-renewal','net_cents':240000,'gst_cents':0}
        self.connection.execute("INSERT INTO revenue_rule_snapshots VALUES(3,'visa-snap','fee','visa-rule',?,?,1,'2026-09-01')",(json.dumps({'items':[item]}),'c'*64))
        # Fixture swaps a fee before it has been used; production snapshots remain immutable.
        self.connection.execute("UPDATE revenue_fee_items SET rule_snapshot_id=3,milestone_code='student-visa-renewal' WHERE id=1")
        self.connection.commit()

    def test_visa_fee_can_receive_without_fabricating_school_contract(self):
        self.seed_independent()
        result=self._record(method='au_personal_account',actual_currency='AUD',actual_amount=90000,base=90000,key='visa-payment')
        self.assertEqual(90000,result['allocation']['allocated_base_cents'])
        self.assertIsNone(result['contract_link'])
        self.assertEqual(0,self.connection.execute('SELECT count(*) FROM revenue_fee_item_contract_links').fetchone()[0])

    def test_visa_fee_rejects_cross_cycle_authority(self):
        self.seed_independent(cycle=100)
        self.connection.execute("UPDATE visa_cases SET student_id='OTHER' WHERE id=901")
        self.connection.commit()
        with self.assertRaises(ValueError):
            self._record(method='au_personal_account',actual_currency='AUD',actual_amount=90000,base=90000,key='bad-visa-payment')
        self.assertEqual(0,self.connection.execute('SELECT count(*) FROM revenue_payments').fetchone()[0])

    def test_database_rejects_stale_visa_link_after_authority_changes(self):
        self.seed_independent()
        first=self._record(method='au_personal_account',actual_currency='AUD',actual_amount=10000,base=10000,key='initial-partial')
        self.connection.commit()
        payment=dict(self.connection.execute('SELECT * FROM revenue_payments WHERE uid=?',(first['uid'],)).fetchone())
        payment.pop('id');payment['uid']='payment-for-negative'
        columns=','.join(payment);params=','.join('?' for _ in payment)
        self.connection.execute('INSERT INTO revenue_payments('+columns+') VALUES('+params+')',list(payment.values()))
        pid=self.connection.execute("SELECT id FROM revenue_payments WHERE uid='payment-for-negative'").fetchone()[0]
        allocation=dict(self.connection.execute('SELECT * FROM revenue_client_payment_allocations').fetchone())
        allocation.pop('id');allocation['uid']='allocation-for-negative';allocation['payment_id']=pid
        self.connection.commit()
        for mutation in ("UPDATE visa_cases SET student_id='OTHER' WHERE id=901", "UPDATE visa_cases SET application_cycle_id=NULL WHERE id=901", "UPDATE revenue_fee_items SET net_cents=250000,gross_cents=250000 WHERE id=1"):
            self.connection.execute('SAVEPOINT negative')
            self.connection.execute(mutation)
            with self.assertRaises(sqlite3.IntegrityError):
                self.connection.execute('INSERT INTO revenue_client_payment_allocations('+','.join(allocation)+') VALUES('+','.join('?' for _ in allocation)+')',list(allocation.values()))
            self.connection.execute('ROLLBACK TO negative');self.connection.execute('RELEASE negative')

class UploadedTransferOfferTests(TransferV2AuthoritativeBusinessRecordTests):
    def test_saved_offer_is_actionable_without_separate_hidden_state_action(self):
        self._mark_cycle_as_new()
        with sqlite3.connect(self.database_path) as c:
            c.row_factory=sqlite3.Row
            c.execute("INSERT INTO offers(id,student_id,offer_status) VALUES(501,'S1','received')")
            first=bind_authoritative_offer(c,student_id='S1',branch_id=self.branch_id,offer_id=501,document_id=None,actor_user_id=1,occurred_at=NOW)
            bind_authoritative_offer(c,student_id='S1',branch_id=self.branch_id,offer_id=501,document_id=None,actor_user_id=1,occurred_at=NOW)
            self.assertEqual('ACTIVE',c.execute('SELECT state FROM offer_received_event_state_events WHERE offer_received_event_id=? ORDER BY id DESC LIMIT 1',(first.offer_event_id,)).fetchone()[0])
            self.assertEqual(1,c.execute('SELECT count(*) FROM offer_received_event_state_events WHERE offer_received_event_id=?',(first.offer_event_id,)).fetchone()[0])

class VisitorPolicyTests(__import__('unittest').TestCase):
    def test_visitor_has_distinct_kind_and_fixed_approved_rule(self):
        from revenue.visa_fee_policy import visa_fee_facts, visa_subclass
        visa={'visa_type':'600 Visitor Visa','application_type':'New Application'}
        self.assertEqual('600',visa_subclass(visa))
        self.assertEqual('visitor_600',visa_fee_facts(visa,education_stream='school_education',cycle_type='initial',existing_aaie=False).service_kind)

from tests.test_revenue_controlled_onboarding import RevenueControlledOnboardingTests
class FamilyCaseFeeTests(RevenueControlledOnboardingTests):
    def test_visitor_rule_activation_and_separate_parent_fees_are_idempotent(self):
        from revenue.visitor_fee_activation import activate_visitor_600
        from revenue.operational_orchestrator import RevenueOperationalOrchestrator
        with self.connect() as c:
            c.execute('ALTER TABLE visa_cases ADD COLUMN student_id TEXT')
            c.execute('ALTER TABLE visa_cases ADD COLUMN applicant_name TEXT')
            c.execute("UPDATE visa_cases SET student_id='S-REVENUE',applicant_name='Mother Test' WHERE id=230")
            c.execute("INSERT INTO visa_cases(id,application_cycle_id,student_id,visa_subclass,case_type,application_type,applicant_name) VALUES(901,100,'S-REVENUE','600','600 Visitor Visa','New Application','Father Test')")
            activate_visitor_600(c,actor_user_id=1,actor_role='Admin',effective_from='2026-01-01',idempotency_key='activate-600')
            activate_visitor_600(c,actor_user_id=1,actor_role='Admin',effective_from='2026-01-01',idempotency_key='activate-600')
            o=RevenueOperationalOrchestrator(c,1,'Admin')
            a=o.visa_case_changed(230);b=o.visa_case_changed(901);o.visa_case_changed(901)
            self.assertEqual((90000,90000),(a['visa_fee_cents'],b['visa_fee_cents']))
            fees=c.execute("SELECT description,net_cents FROM revenue_fee_items WHERE milestone_code IN ('guardian-590','visitor-600')").fetchall()
            self.assertEqual(2,len(fees))
            self.assertTrue(any('Father Test' in r[0] and '600' in r[0] for r in fees))
            self.assertTrue(any('Mother Test' in r[0] and '590' in r[0] for r in fees))

    def test_new_parent_visa_uses_own_date_not_old_school_contract(self):
        from revenue.visitor_fee_activation import activate_visitor_600
        from revenue.operational_orchestrator import RevenueOperationalOrchestrator
        with self.connect() as c:
            c.execute('ALTER TABLE visa_cases ADD COLUMN student_id TEXT')
            c.execute('ALTER TABLE visa_cases ADD COLUMN updated_at TEXT')
            c.execute('ALTER TABLE visa_cases ADD COLUMN applicant_name TEXT')
            c.execute("INSERT INTO visa_cases(id,application_cycle_id,student_id,visa_subclass,case_type,application_type,updated_at,applicant_name) VALUES(901,100,'S-REVENUE','600','600 Visitor Visa','New Application','2026-11-25T10:00:00+11:00','Father Test')")
            activate_visitor_600(c,actor_user_id=1,actor_role='Admin',effective_from='2026-11-25',idempotency_key='activate-current-parent-rule')
            result=RevenueOperationalOrchestrator(c,1,'Admin').visa_case_changed(901)
            self.assertEqual(90000,result['visa_fee_cents'])
            self.assertEqual('2026-11-25',result['visa_fee_policy']['source_engagement_date'])
