"""Focused regression tests; all identities and files are synthetic."""
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from student_lifecycle.transfer_ownership_repair import RepairError, frozen_digest, verify_scope


class FrozenRepairTests(unittest.TestCase):
    def test_scope_requires_exact_approved_digest(self):
        scope = {'batch_uid':'synthetic', 'targets':[{'student_id':'S2','cycle_id':2,'contract_id':102,'authority_id':2}]}
        self.assertEqual(verify_scope(scope, frozen_digest(scope)), scope)
        with self.assertRaises(RepairError):
            verify_scope(scope, '0'*64)

    def test_scope_rejects_duplicate_students_and_cycles(self):
        target = {'student_id':'S2','cycle_id':2,'contract_id':102,'authority_id':2}
        scope = {'batch_uid':'synthetic', 'targets':[target,target]}
        with self.assertRaises(RepairError):
            verify_scope(scope, frozen_digest(scope))

    def test_digest_covers_nested_original_submission(self):
        scope = {'batch_uid':'synthetic','targets':[{'student_id':'S2','cycle_id':2,'submitted_at':'original'}]}
        digest = frozen_digest(scope)
        scope['targets'][0]['submitted_at'] = 'changed'
        with self.assertRaises(RepairError):
            verify_scope(scope, digest)

from unittest.mock import patch
import json
from business_changes.schema import install
from tests.transfer_v2_test_support import create_transfer_v2_database
from student_lifecycle.transfer_ownership_repair import preview, apply

NOW = '2026-11-22T18:00:00+11:00'
SUBMITTED = '2026-11-17T10:00:00+11:00'


class OwnershipTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.db'
        create_transfer_v2_database(self.path)
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            UPDATE roles SET name='Admin' WHERE id=1;
            CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT);
            INSERT INTO schema_migrations VALUES('20260904_035','2026-08-01T00:00:00+10:00');
            ALTER TABLE student_documents ADD COLUMN is_active INTEGER DEFAULT 1;
            CREATE TABLE school_plan_reports(id INTEGER PRIMARY KEY,student_id TEXT,status TEXT);
            CREATE TABLE school_plan_report_confirmations(id INTEGER PRIMARY KEY,report_id INTEGER,student_id TEXT,status TEXT);
            CREATE TABLE school_plan_confirmed_schools(id INTEGER PRIMARY KEY,student_id TEXT,school_id INTEGER,
                school_name_snapshot TEXT,is_selected INTEGER,submission_status TEXT,submitted_at TEXT,result_status TEXT,
                report_id INTEGER,confirmation_id INTEGER);
            CREATE TABLE public_application_preferences(id INTEGER PRIMARY KEY,public_application_group_id INTEGER,
                confirmed_school_id INTEGER,school_id INTEGER,application_status TEXT);
            INSERT INTO school_plan_reports VALUES(11,'S2','confirmed');
            INSERT INTO school_plan_report_confirmations VALUES(12,11,'S2','confirmed');
        ''')
        # Reuse the actual target-state rebuild and continuity guards, not a
        # relaxed approximation. No production migration is executed.
        migration = (Path(__file__).parents[1]/'migrations/20260904_035_transfer_v2_final_schema.sql').read_text()
        section = migration[migration.index('ALTER TABLE transfer_school_applications ADD COLUMN removed_at'):migration.index('CREATE TABLE transfer_payment_confirmations')]
        self.db.executescript(section)
        self.db.execute('BEGIN')
        install(self.db)
        form = dict(current_school_name='Current College',current_year_level='Year 8',
                    applying_year_level='Year 9',preferred_intake_year='2027',preferred_intake_term='02')
        self.db.execute('UPDATE students SET form_data_json=? WHERE student_id=\'S2\'', (json.dumps(form),))
        self.db.execute('''INSERT INTO application_cycles(id,cycle_uid,student_id,parent_form_token,cycle_type,sequence_no,
            status,opened_at,created_at,updated_at,education_stream) VALUES(200,'original-cycle','S2','S2-transfer-intake',
            'initial',1,'active',?,?,?,'school_education')''', (NOW,NOW,NOW))
        artifact_path = str(Path(self.temp.name)/'signed-contracts/102.pdf')
        self.db.execute('INSERT INTO student_documents(id,student_id,document_type,file_path,uploaded_at) VALUES(1,\'S2\',\'Contract\',?,?)', (artifact_path,NOW))
        self.db.execute("INSERT INTO school_plan_confirmed_schools VALUES(13,'S2',20,'Target Grammar',1,'submitted',?,'pending',11,12)", (SUBMITTED,))
        self.db.execute("INSERT INTO private_school_applications(id,application_cycle_id,student_id,confirmed_school_id,school_id,school_name_snapshot,status,submitted_at,created_at,updated_at) VALUES(14,200,'S2',13,20,'Target Grammar','submitted',?,?,?)",(SUBMITTED,SUBMITTED,SUBMITTED))
        source_id = self.db.execute("SELECT id FROM transfer_source_authorities WHERE transfer_contract_id=102").fetchone()[0]
        self.scope = dict(batch_uid='synthetic-batch',targets=[dict(student_id='S2',cycle_id=200,contract_id=102,
            authority_id=source_id,report_id=11,confirmation_id=12,
            artifact=dict(document_id=1,original_path=artifact_path,sha256=hashlib.sha256(Path(artifact_path).read_bytes()).hexdigest()),
            branches=[dict(confirmed_school_id=13,school_id=20,school_name='Target Grammar',source_kind='private',source_id=14,submitted_at=SUBMITTED)])])
        self.paths = {'102':artifact_path}
        self.digest = frozen_digest(self.scope)
        self.db.commit()
        self.frozen = preview(self.db,self.scope,self.digest,1,NOW,self.paths)
        self.money = patch('student_lifecycle.transfer_ownership_repair.ensure_revenue_case_for_cycle', return_value={'id':1})
        self.money.start()
        self.addCleanup(self.money.stop)

    def run_apply(self):
        return apply(self.db,self.scope,self.digest,self.frozen,1,NOW,self.paths)

    def test_preserves_original_cycle_and_application_and_is_idempotent(self):
        original = tuple(self.db.execute('SELECT * FROM private_school_applications WHERE id=14').fetchone())
        self.db.execute('BEGIN IMMEDIATE')
        result = self.run_apply()
        self.db.commit()
        self.assertFalse(result.replayed)
        cycle = self.db.execute('SELECT * FROM application_cycles WHERE id=200').fetchone()
        self.assertEqual(cycle['cycle_uid'],'original-cycle')
        self.assertEqual(cycle['created_at'],NOW)
        self.assertEqual(cycle['cycle_type'],'transfer')
        self.assertEqual(tuple(self.db.execute('SELECT * FROM private_school_applications WHERE id=14').fetchone()),original)
        state = self.db.execute('SELECT new_state,occurred_at,created_at FROM transfer_target_state_events WHERE application_cycle_id=200').fetchone()
        self.assertEqual(tuple(state),('application_submitted',SUBMITTED,NOW))
        self.db.execute('BEGIN IMMEDIATE')
        self.assertTrue(self.run_apply().replayed)
        self.db.commit()
        self.assertEqual(self.db.execute('SELECT count(*) FROM transfer_school_applications WHERE application_cycle_id=200').fetchone()[0],1)

    def test_stale_preview_rejects_all_changes(self):
        self.db.execute("UPDATE private_school_applications SET updated_at='changed' WHERE id=14")
        self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')
        with self.assertRaisesRegex(RepairError,'preview stale'):
            self.run_apply()
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT cycle_type FROM application_cycles WHERE id=200').fetchone()[0],'initial')

    def test_failure_after_case_creation_rolls_back_entire_command(self):
        self.db.execute('BEGIN IMMEDIATE')
        with patch('student_lifecycle.transfer_ownership_repair.authority.consume_authority',side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError,'interrupted'):
                self.run_apply()
        self.db.commit()  # even a caller commit cannot retain partial repair
        self.assertEqual(self.db.execute('SELECT cycle_type FROM application_cycles WHERE id=200').fetchone()[0],'initial')
        self.assertFalse(self.db.execute("SELECT 1 FROM student_cases WHERE student_id='S2'").fetchone())
        self.assertIsNone(self.db.execute('SELECT application_cycle_id FROM contracts WHERE id=102').fetchone()[0])

    def test_wrong_contract_and_inactive_actor_are_rejected(self):
        bad = json.loads(json.dumps(self.scope));bad['targets'][0]['contract_id']=101
        with self.assertRaises(ValueError):
            preview(self.db,bad,frozen_digest(bad),1,NOW,self.paths)
        self.db.execute('UPDATE users SET is_active=0 WHERE id=1')
        with self.assertRaises(ValueError):
            preview(self.db,self.scope,self.digest,1,NOW,self.paths)

    def test_pdf_changed_after_preview_rejects(self):
        Path(self.paths['102']).write_bytes(b'%PDF-tampered')
        self.db.execute('BEGIN IMMEDIATE')
        with self.assertRaisesRegex(RepairError,'hash changed'):
            self.run_apply()
        self.db.rollback()

    def test_public_submission_uses_existing_preference_and_original_time(self):
        target = self.scope['targets'][0]
        branch = target['branches'][0]
        branch.update(source_kind='public',source_id=21)
        self.db.execute('DELETE FROM private_school_applications WHERE id=14')
        self.db.execute("INSERT INTO public_application_groups(id,application_cycle_id,student_id,report_id,confirmation_id,status,submitted_at,created_at,updated_at) VALUES(21,200,'S2',11,12,'submitted',?,?,?)",(SUBMITTED,SUBMITTED,SUBMITTED))
        self.db.execute("INSERT INTO public_application_preferences VALUES(22,21,13,20,'submitted')")
        self.db.commit()
        self.digest=frozen_digest(self.scope)
        self.frozen=preview(self.db,self.scope,self.digest,1,NOW,self.paths)
        self.db.execute('BEGIN IMMEDIATE')
        self.run_apply();self.db.commit()
        self.assertEqual(self.db.execute('SELECT count(*) FROM public_application_preferences').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT submitted_at FROM public_application_groups WHERE id=21').fetchone()[0],SUBMITTED)
        mapping=json.loads(self.db.execute("SELECT metadata_json FROM case_events WHERE event_type='transfer_ownership_repaired'").fetchone()[0])['source_mappings'][0]
        self.assertEqual(mapping['source_id'],21)
        self.assertEqual(mapping['confirmed_school_id'],13)

    def test_financial_change_causes_rollback(self):
        self.db.execute('CREATE TABLE payments(id INTEGER PRIMARY KEY,student_id TEXT,amount REAL)')
        self.db.execute("INSERT INTO payments VALUES(1,'S2',1200)")
        self.db.commit()
        self.frozen=preview(self.db,self.scope,self.digest,1,NOW,self.paths)
        def accidental_charge(conn,*args,**kwargs):
            conn.execute("INSERT INTO payments VALUES(2,'S2',1200)")
            return {'id':1}
        self.db.execute('BEGIN IMMEDIATE')
        with patch('student_lifecycle.transfer_ownership_repair.ensure_revenue_case_for_cycle',side_effect=accidental_charge):
            with self.assertRaisesRegex(RepairError,'financial facts changed'):
                self.run_apply()
        self.db.commit()
        self.assertEqual(self.db.execute('SELECT count(*) FROM payments').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT cycle_type FROM application_cycles WHERE id=200').fetchone()[0],'initial')

    def test_parent_intake_code_is_month_not_term_number(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.run_apply();self.db.commit()
        row=self.db.execute("SELECT transfer_year,transfer_term FROM transfer_cases WHERE case_id=(SELECT source_student_case_id FROM application_cycles WHERE id=200)").fetchone()
        self.assertEqual(tuple(row),(2027,'Term 1'))

    def test_second_student_failure_rolls_back_first_student_too(self):
        from student_lifecycle.transfer_source_authority import save_authority, consume_authority
        def clone(table, where, changes):
            row=dict(self.db.execute('SELECT * FROM '+table+' WHERE '+where).fetchone())
            row.update(changes)
            self.db.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        clone('students',"student_id='S2'",{'student_id':'S4'})
        clone('contracts','id=102',{'id':103,'student_id':'S4'})
        clone('parent_form_tokens',"token='S2-transfer-intake'",{'token':'S4-transfer-intake','student_id':'S4'})
        clone('application_cycles','id=200',{'id':201,'cycle_uid':'original-second','student_id':'S4','parent_form_token':'S4-transfer-intake'})
        clone('student_documents','id=1',{'id':2,'student_id':'S4'})
        clone('school_plan_reports','id=11',{'id':15,'student_id':'S4'})
        clone('school_plan_report_confirmations','id=12',{'id':16,'report_id':15,'student_id':'S4'})
        clone('school_plan_confirmed_schools','id=13',{'id':17,'report_id':15,'confirmation_id':16,'student_id':'S4'})
        clone('private_school_applications','id=14',{'id':18,'application_cycle_id':201,'student_id':'S4','confirmed_school_id':17})
        save_authority(self.db,'S4',103,parent_form_token='S4-transfer-intake',actor_user_id=1,occurred_at=NOW)
        aid=self.db.execute('SELECT id FROM transfer_source_authorities WHERE transfer_contract_id=103').fetchone()[0]
        target=json.loads(json.dumps(self.scope['targets'][0]));target.update(student_id='S4',cycle_id=201,contract_id=103,authority_id=aid,report_id=15,confirmation_id=16)
        target['artifact']['document_id']=2;target['branches'][0].update(confirmed_school_id=17,source_id=18)
        self.scope['targets'].append(target);self.paths['103']=self.paths['102']
        self.db.commit();self.digest=frozen_digest(self.scope)
        self.frozen=preview(self.db,self.scope,self.digest,1,NOW,self.paths)
        def fail_second(conn,source_id,*args,**kwargs):
            if source_id==aid:raise RuntimeError('second student interrupted')
            return consume_authority(conn,source_id,*args,**kwargs)
        self.db.execute('BEGIN IMMEDIATE')
        with patch('student_lifecycle.transfer_ownership_repair.authority.consume_authority',side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError,'second student'):
                self.run_apply()
        self.db.commit()
        self.assertEqual([r[0] for r in self.db.execute('SELECT cycle_type FROM application_cycles WHERE id IN (200,201)')],['initial','initial'])
        self.assertEqual(self.db.execute("SELECT count(*) FROM student_cases WHERE student_id IN ('S2','S4')").fetchone()[0],0)
