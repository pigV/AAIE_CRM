"""Catch lost parent-school facts and incomplete signed Transfer handoffs."""
from contextlib import closing
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.transfer_v2_test_support import create_transfer_v2_database
from student_lifecycle.transfer_v2_service import TransferV2Service, TransferV2ValidationError
from student_lifecycle.transfer_v2_models import TransferV2CreateInput, TransferV2TargetInput

NOW = '2026-11-23T11:00:00+11:00'

class TransferIntakeHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'handoff.db'
        create_transfer_v2_database(self.path)
        self.c = sqlite3.connect(self.path); self.c.row_factory = sqlite3.Row
        self.c.execute('PRAGMA foreign_keys=ON'); self.addCleanup(self.c.close)
        self.form = dict(current_school_name='CURRENT college', current_school_year_level='Year 7',
                         current_year_level='Year 7', applying_year_level='Year 8',
                         preferred_intake_year='2027', preferred_intake_term='02')
        self.c.execute('UPDATE students SET form_data_json=? WHERE student_id=\'S2\'', (json.dumps(self.form),))
        self.c.execute("""CREATE TRIGGER production_enrolment_event_constraint BEFORE INSERT ON school_enrolment_events
          WHEN NEW.event_type NOT IN ('current_confirmed','upcoming_confirmed','transition_scheduled',
          'transition_applied','upcoming_cancelled','transition_reversed','enrolment_corrected')
          BEGIN SELECT RAISE(ABORT,'invalid enrolment event'); END""")
        self.c.commit()

    def svc(self):
        self.assertIsNotNone(importlib.util.find_spec('student_lifecycle.transfer_intake'),
                             'Shared parent-school confirmation and handoff service is missing')
        from student_lifecycle import transfer_intake
        return transfer_intake

    def confirm(self, **overrides):
        args = dict(student_id='S2', parent_form_token='S2-transfer-intake', school_id=10,
                    school_name='Current College', year_level='Year 7', confirmed=True,
                    actor_user_id=1, occurred_at=NOW)
        args.update(overrides)
        with self.c:
            if not self.c.in_transaction: self.c.execute('BEGIN IMMEDIATE')
            return self.svc().confirm_parent_current(self.c, **args)

    def test_school_preview_matches_case_without_writing_or_inventing_dates(self):
        before=self.c.total_changes
        result=self.svc().parent_school_preview(self.c,'S2','S2-transfer-intake')
        self.assertEqual('CURRENT college',result['parent_school_name'])
        self.assertEqual(10,result['suggested_school_id'])
        self.assertEqual('Year 7',result['year_level'])
        self.assertEqual(before,self.c.total_changes)

    def test_confirmation_registers_one_current_with_parent_provenance(self):
        first=self.confirm(); second=self.confirm()
        self.assertEqual(first,second)
        row=self.c.execute("SELECT * FROM student_school_enrolments WHERE student_id='S2'").fetchone()
        self.assertEqual((10,'Current College','Year 7','current','active'),
                         tuple(row[k] for k in ('school_id','school_name_snapshot','year_level_snapshot','enrolment_role','status')))
        self.assertIsNone(row['start_date'])
        self.assertEqual(1,self.c.execute("SELECT count(*) FROM school_enrolment_events WHERE student_id='S2'").fetchone()[0])
        event=self.c.execute("SELECT * FROM school_enrolment_events WHERE student_id='S2'").fetchone()
        self.assertEqual('current_confirmed',event['event_type'])
        self.assertEqual('parent_transfer_intake',json.loads(event['new_value_json'])['confirmation_source'])
        self.assertIn('S2-transfer-intake',event['new_value_json'])

    def test_confirmation_is_required(self):
        with self.assertRaises(ValueError): self.confirm(confirmed=False)
        self.assertEqual(0,self.c.execute("SELECT count(*) FROM student_school_enrolments WHERE student_id='S2'").fetchone()[0])

    def test_mismatched_school_identifier_is_rejected(self):
        with self.assertRaises(ValueError): self.confirm(school_id=20)

    def test_conflicting_existing_current_is_not_overwritten(self):
        self.confirm()
        self.c.execute("UPDATE student_school_enrolments SET school_id=20,school_name_snapshot='Target Grammar' WHERE student_id='S2'")
        self.c.commit()
        with self.assertRaises(ValueError): self.confirm()
        self.assertEqual(20,self.c.execute("SELECT school_id FROM student_school_enrolments WHERE student_id='S2'").fetchone()[0])

    def test_ambiguous_school_name_is_not_auto_selected(self):
        self.c.execute("INSERT INTO schools VALUES(40,'Current College','public','Government','NSW','Sydney',NULL)")
        result=self.svc().parent_school_preview(self.c,'S2','S2-transfer-intake')
        self.assertIsNone(result['suggested_school_id'])
        self.assertEqual(2,len(result['school_matches']))

    def test_cross_student_and_nontransfer_parent_links_are_rejected(self):
        for student,token in [('S1','S2-transfer-intake'),('S2','missing')]:
            with self.subTest(student=student),self.assertRaises(ValueError):
                self.svc().parent_school_preview(self.c,student,token)
        self.c.execute("INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at,created_at) VALUES('ordinary','S2','new_student','2026-08-20','2026-08-20')")
        with self.assertRaises(ValueError): self.svc().parent_school_preview(self.c,'S2','ordinary')

    def test_inactive_staff_cannot_confirm(self):
        self.c.execute('UPDATE users SET is_active=0 WHERE id=1');self.c.commit()
        with self.assertRaises(ValueError): self.confirm()

    def test_school_form_drift_does_not_block_fee_or_overwrite_current(self):
        self.confirm()
        self.form['current_school_year_level']='Year 9'
        self.c.execute("UPDATE students SET form_data_json=? WHERE student_id='S2'",(json.dumps(self.form),));self.c.commit()
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            result=self.svc().handoff_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)
        self.assertEqual('completed',result['status'])
        self.assertEqual('Year 7',self.c.execute("SELECT year_level_snapshot FROM student_school_enrolments WHERE student_id='S2'").fetchone()[0])

    def test_signed_handoff_creates_empty_target_cycle_and_retries_without_duplicates(self):
        self.confirm()
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            result=self.svc().handoff_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            replay=self.svc().handoff_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)
        self.assertEqual(result['cycle_id'],replay['cycle_id'])
        self.assertEqual(1,self.c.execute("SELECT count(*) FROM application_cycles WHERE student_id='S2' AND cycle_type='transfer'").fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM transfer_school_applications WHERE application_cycle_id=?',(result['cycle_id'],)).fetchone()[0])
        transfer=self.c.execute('SELECT * FROM transfer_cases WHERE case_id=?',(result['case_id'],)).fetchone()
        self.assertEqual('Term 1',transfer['transfer_term'])
        self.assertIsNotNone(transfer['current_school_enrolment_id'])
        overview=TransferV2Service(self.path,clock=lambda:NOW).load_overview(result['cycle_id'])
        self.assertFalse(overview.is_legacy)
        self.assertFalse(overview.branches)

    def test_signed_handoff_does_not_require_school_confirmation(self):
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            result=self.svc().handoff_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)
        self.assertEqual('completed',result['status'])
        self.assertEqual(0,self.c.execute("SELECT count(*) FROM student_school_enrolments WHERE student_id='S2'").fetchone()[0])

    def test_recovery_panel_offers_retry_without_a_school_confirmation_form(self):
        from student_lifecycle.transfer_intake_views import signed_handoff_panels
        self.c.executescript("""CREATE TABLE external_transfer_case_requests(
            contract_id INTEGER,request_status TEXT,last_error_message TEXT,id INTEGER);
            CREATE TABLE revenue_fee_item_contract_links(contract_id INTEGER,fee_item_id INTEGER);
            CREATE TABLE revenue_fee_items(id INTEGER,status TEXT);
            INSERT INTO external_transfer_case_requests VALUES(102,'failed','合同已签妥，待工作人员确认当前学校及年级。',1);""")
        html=signed_handoff_panels(self.c,'S2',csrf='csrf',can_manage=True)
        self.assertIn('/transfer-intake/102/retry',html)
        self.assertNotIn('transfer_current_confirmed',html)
        self.assertNotIn('/transfer-intake/102/confirm',html)
        self.assertNotIn('待工作人员确认当前学校及年级',html)

    def test_caller_rollback_removes_handoff_but_preserves_prior_signature(self):
        self.confirm()
        self.c.execute('BEGIN IMMEDIATE')
        self.svc().handoff_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)
        self.c.rollback()
        self.assertIsNone(self.c.execute('SELECT application_cycle_id FROM contracts WHERE id=102').fetchone()[0])
        self.assertEqual('verified',self.c.execute('SELECT lifecycle_state FROM transfer_source_authorities WHERE transfer_contract_id=102').fetchone()[0])

    def test_targets_can_be_added_after_handoff_without_reopening_cycle(self):
        self.confirm()
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            result=self.svc().handoff_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)
        service=TransferV2Service(self.path,clock=lambda:NOW)
        overview=service.add_target_branch(result['cycle_id'],target=TransferV2TargetInput(20,'Target Grammar'),actor_user_id=1,idempotency_key='later-target')
        self.assertEqual(result['cycle_id'],overview.cycle_id)
        self.assertEqual(1,len(overview.branches))

    def test_current_v2_allows_empty_targets_for_existing_student(self):
        request=TransferV2CreateInput('S1',101,'aaie_existing',10,'Current College','Year 8','Year 9',2027,'Term 1',())
        overview=TransferV2Service(self.path,clock=lambda:NOW).create_cycle(request,actor_user_id=1,idempotency_key='no-targets-existing')
        self.assertFalse(overview.branches)
        self.assertEqual('aaie_existing',overview.source_label)

    def test_failed_handoff_is_visible_and_can_be_retried_after_intake_repair(self):
        self.c.executescript('''CREATE TABLE external_transfer_case_requests(
            id INTEGER PRIMARY KEY,student_id TEXT,contract_id INTEGER,idempotency_key TEXT UNIQUE,
            request_status TEXT,case_id INTEGER,attempt_count INTEGER DEFAULT 0,
            last_error_code TEXT,last_error_message TEXT,next_retry_at TEXT,created_at TEXT,
            updated_at TEXT,completed_at TEXT);''')
        self.form['preferred_intake_term']='invalid'
        self.c.execute("UPDATE students SET form_data_json=? WHERE student_id='S2'",(json.dumps(self.form),));self.c.commit()
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            result=self.svc().attempt_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)
        self.assertEqual('failed',result['status'])
        row=self.c.execute('SELECT * FROM external_transfer_case_requests').fetchone()
        self.assertIn('入学月份',row['last_error_message'])
        self.assertEqual('FullyExecuted',self.c.execute('SELECT contract_status FROM contracts WHERE id=102').fetchone()[0])
        self.form['preferred_intake_term']='02'
        self.c.execute("UPDATE students SET form_data_json=? WHERE student_id='S2'",(json.dumps(self.form),));self.c.commit()
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            result=self.svc().attempt_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)
        self.assertEqual('completed',result['status'])
        row=self.c.execute('SELECT * FROM external_transfer_case_requests').fetchone()
        self.assertEqual(2,row['attempt_count'])
        self.assertIsNone(row['last_error_message'])

    def test_existing_initial_financial_facts_prevent_automatic_takeover(self):
        from student_lifecycle.transfer_state import evaluate_transfer_creation_state
        self.c.executescript('''
          INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at)
            VALUES(991,'initial-with-money','S2','initial',1,'active','2026-11-22','2026-11-22','2026-11-22');
          CREATE TABLE revenue_commercial_cases(id INTEGER PRIMARY KEY,application_cycle_id INTEGER,status TEXT);
          CREATE TABLE revenue_fee_items(id INTEGER PRIMARY KEY,commercial_case_id INTEGER,net_cents INTEGER,status TEXT);
          INSERT INTO revenue_commercial_cases VALUES(1,991,'open');
          INSERT INTO revenue_fee_items VALUES(1,1,240000,'unbilled');
        ''')
        result=evaluate_transfer_creation_state(self.c,'S2')
        self.assertFalse(result.allowed, 'Initial cycle with existing money must not be superseded')

    def test_matching_legacy_current_is_reused_and_explicitly_confirmed(self):
        self.c.execute("""INSERT INTO student_school_enrolments(student_id,school_id,school_name_snapshot,
          year_level_snapshot,enrolment_role,status,created_by,created_at,updated_by,updated_at)
          VALUES('S2',10,'  CURRENT   COLLEGE  ','Year 7','current','active',1,?,1,?)""",(NOW,NOW))
        self.c.commit()
        original_id=self.c.execute("SELECT id FROM student_school_enrolments WHERE student_id='S2'").fetchone()[0]
        self.assertEqual(original_id,self.confirm())
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            result=self.svc().handoff_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)
        self.assertEqual('completed',result['status'])
        row=self.c.execute("SELECT * FROM student_school_enrolments WHERE student_id='S2'").fetchone()
        self.assertEqual('  CURRENT   COLLEGE  ',row['school_name_snapshot'])
        self.assertEqual(1,row['confirmed_by'])

    def test_recovery_reuses_legacy_contract_request_without_replacing_key(self):
        self.c.executescript('''CREATE TABLE external_transfer_case_requests(
            id INTEGER PRIMARY KEY,student_id TEXT,contract_id INTEGER,idempotency_key TEXT UNIQUE,
            request_status TEXT,case_id INTEGER,attempt_count INTEGER DEFAULT 0,last_error_code TEXT,
            last_error_message TEXT,next_retry_at TEXT,created_at TEXT,updated_at TEXT,completed_at TEXT,
            UNIQUE(student_id,contract_id));
            INSERT INTO external_transfer_case_requests(student_id,contract_id,idempotency_key,request_status,
              attempt_count,created_at,updated_at) VALUES('S2',102,'old-external-orchestrator-key','failed',1,'2026-08-22','2026-08-22');''')
        self.confirm()
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            result=self.svc().attempt_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)
        self.assertEqual('completed',result['status'])
        row=self.c.execute('SELECT * FROM external_transfer_case_requests').fetchone()
        self.assertEqual('old-external-orchestrator-key',row['idempotency_key'])
        self.assertEqual(2,row['attempt_count'])
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM external_transfer_case_requests').fetchone()[0])

    def test_manual_v2_creation_also_registers_the_staff_selected_current_school(self):
        request=TransferV2CreateInput('S2',102,'new_transfer',10,'Current College','Year 7','Year 8',2027,'Term 1',())
        overview=TransferV2Service(self.path,clock=lambda:NOW).create_cycle(request,actor_user_id=1,idempotency_key='manual-new-transfer')
        row=self.c.execute("SELECT * FROM student_school_enrolments WHERE student_id='S2' AND enrolment_role='current'").fetchone()
        self.assertIsNotNone(row,'Manual V2 must not create an unbound school snapshot only')
        transfer=self.c.execute('SELECT current_school_enrolment_id FROM transfer_cases WHERE case_id=?',(overview.case_id,)).fetchone()
        self.assertEqual(row['id'],transfer[0])

    def test_existing_source_current_school_cannot_be_replaced_during_v2_creation(self):
        request=TransferV2CreateInput('S1',101,'aaie_existing',20,'Target Grammar','Year 8','Year 9',2027,'Term 1',())
        with self.assertRaises(TransferV2ValidationError):
            TransferV2Service(self.path,clock=lambda:NOW).create_cycle(request,actor_user_id=1,idempotency_key='wrong-current')
        self.assertEqual(10,self.c.execute('SELECT school_id FROM student_school_enrolments WHERE id=80').fetchone()[0])

    def test_confirmation_fills_only_missing_legacy_school_identity_and_year(self):
        self.c.execute("""INSERT INTO student_school_enrolments(student_id,school_name_snapshot,
          enrolment_role,status,created_by,created_at,updated_by,updated_at)
          VALUES('S2','CURRENT COLLEGE','current','active',1,?,1,?)""",(NOW,NOW));self.c.commit()
        self.confirm()
        row=self.c.execute("SELECT school_id,year_level_snapshot,school_name_snapshot FROM student_school_enrolments WHERE student_id='S2'").fetchone()
        self.assertEqual((10,'Year 7','CURRENT COLLEGE'),tuple(row))

    def test_cancelled_request_is_not_resumed_by_a_signing_replay(self):
        self.c.executescript('''CREATE TABLE external_transfer_case_requests(
            id INTEGER PRIMARY KEY,student_id TEXT,contract_id INTEGER,idempotency_key TEXT UNIQUE,
            request_status TEXT,case_id INTEGER,attempt_count INTEGER DEFAULT 0,last_error_code TEXT,
            last_error_message TEXT,next_retry_at TEXT,created_at TEXT,updated_at TEXT,completed_at TEXT,
            UNIQUE(student_id,contract_id));
            INSERT INTO external_transfer_case_requests(student_id,contract_id,idempotency_key,request_status,
              attempt_count,created_at,updated_at) VALUES('S2',102,'cancelled-request','cancelled',1,'2026-08-22','2026-08-22');''')
        self.confirm()
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            result=self.svc().attempt_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)
        self.assertEqual('failed',result['status'])
        self.assertEqual(('cancelled',1),tuple(self.c.execute('SELECT request_status,attempt_count FROM external_transfer_case_requests').fetchone()))
        self.assertIsNone(self.c.execute('SELECT application_cycle_id FROM contracts WHERE id=102').fetchone()[0])

    def test_reconfirming_manual_school_with_library_id_appends_fresh_evidence(self):
        first=self.confirm(school_id=None)
        self.assertEqual(first,self.confirm(school_id=10))
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            result=self.svc().handoff_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)
        self.assertEqual('completed',result['status'])
        evidence=self.c.execute("SELECT new_value_json FROM school_enrolment_events WHERE student_id='S2' AND json_extract(new_value_json,'$.confirmation_source')='parent_transfer_intake' ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(10,json.loads(evidence)['school_id'])

    def test_older_intake_cannot_claim_the_latest_student_profile_as_its_snapshot(self):
        self.c.execute("INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at,created_at) VALUES('newer-intake','S2','new_student_transfer','2026-11-23','2026-11-23')")
        with self.assertRaises(ValueError):self.svc().parent_school_preview(self.c,'S2','S2-transfer-intake')

    def test_concurrent_completion_retries_share_one_request_and_one_cycle(self):
        from concurrent.futures import ThreadPoolExecutor
        self.confirm()
        self.c.executescript('''CREATE TABLE external_transfer_case_requests(
            id INTEGER PRIMARY KEY,student_id TEXT,contract_id INTEGER,idempotency_key TEXT UNIQUE,
            request_status TEXT,case_id INTEGER,attempt_count INTEGER DEFAULT 0,last_error_code TEXT,
            last_error_message TEXT,next_retry_at TEXT,created_at TEXT,updated_at TEXT,completed_at TEXT,
            UNIQUE(student_id,contract_id));''')
        service=self.svc()
        def complete(_):
            with sqlite3.connect(self.path,timeout=20) as connection:
                connection.row_factory=sqlite3.Row;connection.execute('PRAGMA foreign_keys=ON')
                connection.execute('BEGIN IMMEDIATE')
                return service.attempt_signed_transfer(connection,102,actor_user_id=1,occurred_at=NOW)
        with ThreadPoolExecutor(max_workers=2) as workers:results=list(workers.map(complete,range(2)))
        self.assertTrue(all(result['status']=='completed' for result in results))
        self.assertEqual(1,len({result['cycle_id'] for result in results}))
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM external_transfer_case_requests').fetchone()[0])
        self.assertEqual(1,self.c.execute("SELECT count(*) FROM application_cycles WHERE student_id='S2' AND status='active'").fetchone()[0])

    def test_invalidated_newer_submission_still_supersedes_the_old_profile_snapshot(self):
        self.c.execute("INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at,invalidated_at,created_at) VALUES('newer-invalidated','S2','new_student_transfer','2026-11-23','2026-11-23','2026-11-23')")
        with self.assertRaises(ValueError):self.svc().parent_school_preview(self.c,'S2','S2-transfer-intake')

    def test_malformed_downstream_intake_data_cannot_undo_a_completed_signature(self):
        self.c.executescript('''CREATE TABLE external_transfer_case_requests(
            id INTEGER PRIMARY KEY,student_id TEXT,contract_id INTEGER,idempotency_key TEXT UNIQUE,
            request_status TEXT,case_id INTEGER,attempt_count INTEGER DEFAULT 0,last_error_code TEXT,
            last_error_message TEXT,next_retry_at TEXT,created_at TEXT,updated_at TEXT,completed_at TEXT,
            UNIQUE(student_id,contract_id));''')
        self.confirm();self.form['preferred_intake_year']=['malformed']
        self.c.execute("UPDATE students SET form_data_json=? WHERE student_id='S2'",(json.dumps(self.form),));self.c.commit()
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            result=self.svc().attempt_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)
        self.assertEqual('failed',result['status'])
        self.assertEqual('FullyExecuted',self.c.execute('SELECT contract_status FROM contracts WHERE id=102').fetchone()[0])
        self.assertIsNone(self.c.execute('SELECT application_cycle_id FROM contracts WHERE id=102').fetchone()[0])
