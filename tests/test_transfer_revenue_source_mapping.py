"""Transfer Revenue consumes exact Source Authority; all DBs are in-memory."""
import json
import sqlite3
import unittest
from pathlib import Path
from revenue.facts import RevenueFactAdapter
from revenue.services import RevenueService
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.repository import RevenueRepository
from revenue.client_fee_rules import FeeRuleAuthorityService, DatabaseClientFeeRuleResolver, ClientFeeRuleFacts, MissingFeeRule
from student_lifecycle.transfer_source_authority import SourceAuthorityError
from tests.test_transfer_source_authority_migration import fixture, MIGRATION

ROOT = Path(__file__).resolve().parents[1]

class TransferRevenueSourceMappingTests(unittest.TestCase):
    def setUp(self):
        self.db = fixture()
        self.addCleanup(self.db.close)
        self.db.executescript(MIGRATION.read_text())
        self.db.executescript("""
            ALTER TABLE application_cycles ADD COLUMN created_at TEXT DEFAULT '2026-09-07';
            ALTER TABLE contracts ADD COLUMN contract_date TEXT;
            ALTER TABLE contracts ADD COLUMN fully_executed_at TEXT;
            CREATE TABLE transfer_school_applications(id INTEGER PRIMARY KEY,application_cycle_id INTEGER,application_status TEXT);
        """)
        self.db.executescript((ROOT/'migrations/20260829_032_revenue_commission_v1.sql').read_text())
        self.db.executescript((ROOT/'migrations/20260901_034_revenue_client_payment_receipt.sql').read_text())
        self.repo = RevenueRepository(self.db)
        self.service = RevenueService(self.repo)
        self.adapter = RevenueFactAdapter(self.db)
        FeeRuleAuthorityService(self.repo).initialize_2026(actor_user_id=1,actor_role='Admin',idempotency_key='rule-init')

    def bind(self, mode='contract', consumed=True):
        student, contract, classification = ('new',100,'new_transfer') if mode=='new' else ('existing',200,'aaie_existing')
        evidence = dict(parent_form_token='new-intake') if mode=='new' else dict(current_enrolment_id=21,ordinary_relationship_cycle_id=20,ordinary_relationship_contract_id=201)
        if mode=='submission':
            self.db.execute("UPDATE contracts SET contract_status='Draft' WHERE id=201")
            self.db.execute("INSERT INTO private_school_applications VALUES(202,'existing',20,'submitted','2026-08-01')")
            evidence = dict(current_enrolment_id=21,ordinary_relationship_cycle_id=20,private_school_application_id=202)
        fields = dict(student_id=student,transfer_contract_id=contract,rule_version='transfer-source-v1',created_by=1,created_at='2026-09-07',**evidence)
        self.db.execute(f"INSERT INTO transfer_source_authorities({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",tuple(fields.values()))
        self.db.execute("""UPDATE transfer_source_authorities
            SET lifecycle_state='verified',source_classification=?,verified_by=1,verified_at='2026-09-07'
            WHERE transfer_contract_id=?""", (classification, contract))
        self.db.execute('INSERT INTO student_cases(id,student_id) VALUES(99,?)',(student,))
        self.db.execute("INSERT INTO application_cycles(id,student_id,cycle_type,education_stream,status,source_student_case_id) VALUES(99,?,'transfer','school_education','active',99)",(student,))
        self.db.execute("UPDATE contracts SET contract_status='FullyExecuted',application_cycle_id=99,fully_executed_at='2026-09-07',contract_date='2026-09-07' WHERE id=?",(contract,))
        self.db.execute('INSERT INTO transfer_cases VALUES(99,?,?)',(contract,classification))
        self.db.execute("INSERT INTO transfer_v2_origin_events(id,application_cycle_id,new_source_label,event_uid) VALUES(99,99,?,'origin99')",(classification,))
        if consumed:
            self.db.execute("UPDATE transfer_source_authorities SET lifecycle_state='consumed',application_cycle_id=99 WHERE transfer_contract_id=?",(contract,))
        return classification

    def fee(self, case):
        return DatabaseClientFeeRuleResolver(self.db).resolve(ClientFeeRuleFacts(service_kind='base',education_stream='school_education',cycle_type='transfer',existing_aaie_k12=bool(case['existing_aaie_k12']),onshore=True),'2026-09-07').net_cents

    def test_contract_only_existing_is_1200_without_generic_submission(self):
        self.bind()
        self.assertFalse(self.adapter.resolve_existing_aaie_k12(99).is_existing)
        case=self.service.open_case(99,1,'case99')
        self.assertEqual(self.fee(case),120000)
        evidence=json.loads(case['classification_evidence_json'])
        self.assertEqual(evidence['definition'],'transfer-source-v1')
        self.assertEqual(evidence['transfer_contract_id'],200)

    def test_submission_only_existing_is_1200_without_generic_contract(self):
        self.bind('submission')
        self.assertFalse(self.adapter.resolve_existing_aaie_k12(99).is_existing)
        self.assertEqual(self.fee(self.service.open_case(99,1,'case99')),120000)

    def test_explicit_new_parent_is_2400(self):
        self.bind('new')
        case=self.service.open_case(99,1,'case99')
        self.assertEqual(self.fee(case),240000)
        self.assertEqual(json.loads(case['classification_evidence_json'])['definition'],'transfer-source-v1')

    def test_missing_source_holds_without_case_or_receipt(self):
        self.db.execute("INSERT INTO application_cycles(id,student_id,cycle_type,education_stream,status) VALUES(99,'other','transfer','school_education','active')")
        with self.assertRaises(SourceAuthorityError): self.service.open_case(99,1,'case99')
        self.assertEqual(self.db.execute('SELECT count(*) FROM revenue_commercial_cases').fetchone()[0],0)
        self.assertIsNone(self.db.execute("SELECT 1 FROM revenue_idempotency_receipts WHERE idempotency_key='case99'").fetchone())

    def test_pending_source_cannot_open_fee_case(self):
        self.bind(consumed=False)
        with self.assertRaises(SourceAuthorityError): self.service.open_case(99,1,'case99')

    def test_missing_source_schema_holds(self):
        self.bind()
        self.db.execute('ALTER TABLE transfer_source_authorities RENAME TO unavailable_source_authorities')
        with self.assertRaises(SourceAuthorityError): self.service.open_case(99,1,'case99')

    def test_historical_case_replay_does_not_reclassify_or_backfill(self):
        self.bind()
        first = self.service.open_case(99,1,'case99')
        self.db.execute('ALTER TABLE transfer_source_authorities RENAME TO unavailable_source_authorities')
        self.assertEqual(self.service.open_case(99,1,'case99'), first)
        self.assertEqual(self.service.open_case(99,1,'read-existing'), first)

    def test_direct_new_fee_write_holds_without_source_and_preserves_existing(self):
        self.bind()
        case = self.service.open_case(99,1,'case99')
        account = self.service.ensure_student_billing_account('existing',1,'account','2026-09-07')
        self.db.execute('ALTER TABLE transfer_source_authorities RENAME TO unavailable_source_authorities')
        before = self.db.total_changes
        with self.assertRaises(SourceAuthorityError):
            self.service.create_fee_items(case['uid'],account['uid'],[dict(net_cents=120000,gst_cents=0,milestone_code='existing-transfer',description='Transfer')],1,'new-fee','fee-rule-version-2026-existing-transfer-v1')
        self.assertEqual(self.db.execute('SELECT count(*) FROM revenue_fee_items').fetchone()[0],0)
        self.assertEqual(self.db.total_changes,before)

    def test_historical_operational_fee_replay_is_unchanged_without_source(self):
        self.bind()
        orchestrator=RevenueOperationalOrchestrator(self.db,1,'Admin')
        first=orchestrator.contract_fully_executed(200)
        self.db.execute('ALTER TABLE transfer_source_authorities RENAME TO unavailable_source_authorities')
        fees=[tuple(row) for row in self.db.execute('SELECT * FROM revenue_fee_items')]
        replay=orchestrator.contract_fully_executed(200)
        self.assertEqual(first,replay)
        self.assertEqual(fees,[tuple(row) for row in self.db.execute('SELECT * FROM revenue_fee_items')])
        # An unrelated lifecycle/commission context event may still reuse the case.
        event=orchestrator.cycle_materialized(99)
        self.assertEqual(event['commercial_case_uid'],first['commercial_case_uid'])

    def test_consumed_history_is_not_reclassified_from_later_enrolment_changes(self):
        self.bind()
        self.db.execute("UPDATE student_school_enrolments SET enrolment_role='previous',status='inactive' WHERE id=21")
        self.assertEqual(self.fee(self.service.open_case(99,1,'case99')),120000)

    def test_operational_event_replay_preserves_original_fee_after_case_drift(self):
        self.bind()
        orchestrator=RevenueOperationalOrchestrator(self.db,1,'Admin')
        first=orchestrator.contract_fully_executed(200)
        self.assertEqual(first['fee_items'][0]['net_cents'],120000)
        self.assertEqual(first['fee_items'][0]['gst_cents'],0)
        self.db.execute('UPDATE revenue_commercial_cases SET existing_aaie_k12=0 WHERE application_cycle_id=99')
        self.assertEqual(first,orchestrator.contract_fully_executed(200))
        self.assertEqual(self.db.execute('SELECT count(*) FROM revenue_fee_items').fetchone()[0],1)

    def test_changed_rule_uid_cannot_create_second_contract_fee_on_replay(self):
        self.bind()
        orchestrator=RevenueOperationalOrchestrator(self.db,1,'Admin')
        first=orchestrator.contract_fully_executed(200)
        self.db.execute("UPDATE revenue_fee_rule_versions SET uid=uid||'-revised' WHERE uid='fee-rule-version-2026-existing-transfer-v1'")
        replay=orchestrator.contract_fully_executed(200)
        self.assertEqual(first,replay)
        self.assertEqual(self.db.execute('SELECT count(*) FROM revenue_fee_items').fetchone()[0],1)

    def test_new_fee_write_still_holds_when_stored_classification_conflicts(self):
        self.bind()
        case=self.service.open_case(99,1,'case99')
        account=self.service.ensure_student_billing_account('existing',1,'account','2026-09-07')
        self.db.execute('UPDATE revenue_commercial_cases SET existing_aaie_k12=0 WHERE application_cycle_id=99')
        with self.assertRaises(SourceAuthorityError):
            self.service.create_fee_items(case['uid'],account['uid'],[dict(net_cents=240000,gst_cents=0,milestone_code='new-onshore-transfer',description='Transfer')],1,'new-fee','fee-rule-version-2026-new-onshore-transfer-v1')
        self.assertEqual(self.db.execute('SELECT count(*) FROM revenue_fee_items').fetchone()[0],0)

    def test_receipt_from_another_contract_is_not_reused(self):
        self.bind()
        orchestrator=RevenueOperationalOrchestrator(self.db,1,'Admin')
        orchestrator.contract_fully_executed(200)
        self.db.execute("INSERT INTO contracts(id,student_id,contract_type,contract_status,application_cycle_id,contract_date,fully_executed_at) VALUES(202,'existing','TransferApplicationContract','FullyExecuted',99,'2026-09-07','2026-09-07')")
        self.db.execute("UPDATE revenue_idempotency_receipts SET idempotency_key=replace(idempotency_key,'contract:200:','contract:202:') WHERE command_type='fee_items_created'")
        with self.assertRaises(SourceAuthorityError): orchestrator.contract_fully_executed(202)
        self.assertEqual(self.db.execute('SELECT count(*) FROM revenue_fee_items').fetchone()[0],1)

    def pending_price(self, existing=True):
        self.assertTrue((ROOT/'revenue/transfer_pricing.py').is_file(), 'Shared pre-Cycle DB fee authority is required')
        from revenue.transfer_pricing import resolve_transfer_base_fee
        student, contract = ('existing',200) if existing else ('new',100)
        evidence = dict(current_enrolment_id=21,ordinary_relationship_cycle_id=20,ordinary_relationship_contract_id=201) if existing else dict(parent_form_token='new-intake')
        values = dict(student_id=student,transfer_contract_id=contract,rule_version='transfer-source-v1',created_by=1,created_at='2026-09-07',**evidence)
        self.db.execute(f"INSERT INTO transfer_source_authorities({','.join(values)}) VALUES({','.join('?' for _ in values)})",tuple(values.values()))
        classification = 'aaie_existing' if existing else 'new_transfer'
        self.db.execute("""UPDATE transfer_source_authorities
            SET lifecycle_state='verified',source_classification=?,verified_by=1,verified_at='2026-09-07'
            WHERE transfer_contract_id=?""", (classification, contract))
        return resolve_transfer_base_fee(self.db,student_id=student,transfer_contract_id=contract,source_engagement_date='2026-09-07')

    def test_pending_existing_contract_price_uses_active_db_rule_without_cycle(self):
        rule=self.pending_price()
        self.assertEqual(rule.net_cents,120000)
        self.assertEqual(rule.rule_uid,'fee-rule-version-2026-existing-transfer-v1')
        self.assertIsNone(self.db.execute('SELECT application_cycle_id FROM contracts WHERE id=200').fetchone()[0])

    def test_pending_new_contract_price_uses_active_db_rule(self):
        self.assertEqual(self.pending_price(existing=False).net_cents,240000)

    def test_pending_contract_missing_active_rule_holds_without_literal_fallback(self):
        self.db.execute("UPDATE revenue_fee_rule_versions SET status='retired'")
        with self.assertRaises(MissingFeeRule): self.pending_price()

    def test_pending_contract_conflicting_db_amount_holds(self):
        self.db.execute("UPDATE revenue_fee_rule_milestones SET fixed_cents=130000 WHERE rule_version_id=(SELECT id FROM revenue_fee_rule_versions WHERE uid='fee-rule-version-2026-existing-transfer-v1')")
        with self.assertRaises(SourceAuthorityError): self.pending_price()

    def test_nontransfer_retains_generic_definition(self):
        case=self.service.open_case(20,1,'case20')
        self.assertEqual(case['existing_aaie_k12'],0)
        self.assertEqual(json.loads(case['classification_evidence_json'])['definition'],'existing_aaie_k12_v1')

if __name__=='__main__': unittest.main()
