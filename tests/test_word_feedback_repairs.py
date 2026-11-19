"""Operator repair checks; real-record rehearsal uses an explicit isolated fixture.

AAIE_FEEDBACK_REHEARSAL_DB must name the consistent, unmodified task snapshot.
No test opens a production database for writing or accesses attachment files.
"""
import contextlib
import io
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.word_feedback_repairs import RepairConflict, apply_batch, main, plan, protected, rows, state

STAMP = '2026-11-19T08:20:00+11:00'
REASON = 'Reviewed Word feedback: exact record corrections; original files retained'


class RepairArgumentTests(unittest.TestCase):
    def test_missing_reason_and_naive_timestamp_are_rejected_without_writes(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        for reason,stamp in (('',STAMP),(REASON,'2026-11-19T08:20:00')):
            with self.subTest(reason=reason,stamp=stamp):
                with self.assertRaises(RepairConflict):
                    apply_batch(conn,2,reason,stamp)
                self.assertFalse(conn.in_transaction)
                self.assertEqual(conn.total_changes,0)

    def test_nonexistent_dryrun_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'missing.sqlite3'
            with self.assertRaises(sqlite3.OperationalError):
                main(['--database',str(path),'--actor-id','2','--dry-run'])
            self.assertFalse(path.exists())


@unittest.skipUnless(os.environ.get('AAIE_FEEDBACK_REHEARSAL_DB'),
                     'Requires explicitly supplied isolated rehearsal database')
class RealFeedbackRepairTests(unittest.TestCase):
    def setUp(self):
        source=Path(os.environ['AAIE_FEEDBACK_REHEARSAL_DB']).resolve()
        self.assertIn('/worktrees/',str(source))
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'test.sqlite3'
        if os.uname().sysname == 'Darwin':
            subprocess.run(['cp','-c',str(source),str(self.path)],check=True)
        else:
            shutil.copyfile(source,self.path)
        self.conn=sqlite3.connect(self.path)
        self.conn.row_factory=sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(self.conn.close)

    def apply(self):
        return apply_batch(self.conn,2,REASON,STAMP)

    def test_active_admin_wildcard_permissions_are_recognized(self):
        from tools.word_feedback_repairs import actor
        self.assertTrue(actor(self.conn,1)['can_manage_offer'])

    def test_real_apply_retry_preserves_history_dates_and_unrelated_rows(self):
        old_protected=protected(self.conn)
        old_receipts=rows(self.conn,'SELECT * FROM offer_received_events')
        old_docs=rows(self.conn,'SELECT * FROM student_documents WHERE id<>1669')
        old_coe=rows(self.conn,'SELECT * FROM coe_oshc')
        unaffected = {
            table:rows(self.conn,sql) for table,sql in {
                'students':'SELECT * FROM students',
                'cycles':'SELECT * FROM application_cycles',
                'other_offers':'SELECT * FROM offers WHERE id NOT IN (98,106,156)',
                'other_confirmed':'SELECT * FROM school_plan_confirmed_schools WHERE id<>37',
                'other_preferences':'SELECT * FROM public_application_preferences WHERE id<>30',
            }.items()
        }
        result=self.apply()
        self.assertEqual(protected(self.conn),old_protected)
        self.assertEqual(rows(self.conn,'SELECT * FROM offer_received_events'),old_receipts)
        self.assertEqual(rows(self.conn,'SELECT * FROM student_documents WHERE id<>1669'),old_docs)
        self.assertEqual(rows(self.conn,'SELECT * FROM coe_oshc'),old_coe)
        for name,sql in {
            'students':'SELECT * FROM students',
            'cycles':'SELECT * FROM application_cycles',
            'other_offers':'SELECT * FROM offers WHERE id NOT IN (98,106,156)',
            'other_confirmed':'SELECT * FROM school_plan_confirmed_schools WHERE id<>37',
            'other_preferences':'SELECT * FROM public_application_preferences WHERE id<>30',
        }.items():
            self.assertEqual(rows(self.conn,sql),unaffected[name],name)
        for oid,cycle in ((106,48),(156,53)):
            current=state(self.conn,oid)
            self.assertEqual(current['legacy_acceptance'][0]['offer_id'],oid)
            active=[r for r in current['canonical_acceptance'] if r['acceptance_status']=='active']
            self.assertEqual(len(active),1)
            self.assertIsNone(active[0]['signed_offer_completed_at'])
            self.assertIsNone(active[0]['payment_completed_at'])
        invalid=state(self.conn,98)
        self.assertEqual(invalid['offer']['offer_status'],'invalid_record')
        self.assertEqual(invalid['offer_state'][-1]['state'],'INVALID_RECORD')
        self.assertEqual(invalid['confirmed_school'][0]['is_selected'],0)
        self.assertEqual(invalid['preference'][0]['application_status'],'closed')
        self.assertEqual(invalid['documents'][0]['is_active'],0)
        self.assertEqual(invalid['documents'][0]['document_status'],'voided')
        self.assertEqual(state(self.conn,156)['coe_confirmation'][0]['coe_id'],284)
        audited=rows(self.conn,"SELECT * FROM audit_logs WHERE action_type='word_feedback_record_repaired'")
        self.assertEqual(len(audited),3)
        self.assertTrue(all(r['old_value'] and REASON in r['new_value'] for r in audited))
        changes=self.conn.total_changes
        retried=apply_batch(self.conn,2,'Idempotent retry','2026-11-19T08:30:00+11:00')
        self.assertTrue(all(r['unchanged'] for r in retried))
        self.assertEqual(self.conn.total_changes,changes)

    def test_repaired_acceptance_drift_is_rejected_on_retry(self):
        self.apply()
        self.conn.execute("UPDATE offers SET offer_status='invalid_record' WHERE id=106")
        self.conn.commit()
        with self.assertRaisesRegex(RepairConflict, 'accepted status'):
            plan(self.conn,2)
        self.conn.execute("UPDATE offers SET offer_status='accepted' WHERE id=106")
        from workflow_offer_events import record_offer_state
        record_offer_state(self.conn,55,state='VOID',occurred_at='2026-11-19T09:00:00+11:00',
                           actor_user_id=2,reason='Later staff decision')
        self.conn.commit()
        before=self.conn.total_changes
        with self.assertRaisesRegex(RepairConflict, 'validity state'):
            self.apply()
        self.assertEqual(before,self.conn.total_changes)

    def test_mid_batch_service_failure_rolls_back_earlier_acceptances_and_audits(self):
        old=[state(self.conn,i) for i in (106,156,98)]
        with patch('school_application_tracking.set_confirmed_school_result',side_effect=RuntimeError('injected failure')):
            with self.assertRaisesRegex(RuntimeError,'injected failure'):
                self.apply()
        self.assertEqual([state(self.conn,i) for i in (106,156,98)],old)
        self.assertEqual(rows(self.conn,"SELECT id FROM audit_logs WHERE action_type='word_feedback_record_repaired'"),[])
        self.assertFalse(self.conn.in_transaction)

    def test_identity_drift_refuses_entire_batch(self):
        self.conn.execute('UPDATE offers SET school_id=63 WHERE id=106')
        self.conn.commit()
        old=[state(self.conn,i) for i in (106,156,98)]
        with self.assertRaisesRegex(RepairConflict,'identity changed'):
            self.apply()
        self.assertEqual([state(self.conn,i) for i in (106,156,98)],old)

    def test_different_active_accepted_offer_is_never_switched(self):
        columns=[r[1] for r in self.conn.execute('PRAGMA table_info(offers)') if r[1]!='id']
        names=','.join(columns)
        self.conn.execute(f'INSERT INTO offers(id,{names}) SELECT 9999,{names} FROM offers WHERE id=106')
        self.conn.execute('''INSERT INTO student_offer_acceptances(
            student_id,offer_id,status_before_acceptance,accepted_at,accepted_by_name,updated_at)
            VALUES('20260066',9999,'signed',?,'Staff',?)''',(STAMP,STAMP))
        self.conn.commit()
        with self.assertRaisesRegex(RepairConflict,'another legacy acceptance'):
            self.apply()
        self.assertEqual(rows(self.conn,"SELECT offer_id FROM student_offer_acceptances WHERE student_id='20260066'")[0]['offer_id'],9999)

    def test_shared_attachment_refuses_retirement(self):
        path=state(self.conn,98)['offer']['offer_file_path']
        self.conn.execute('UPDATE offers SET signed_offer_file_path=? WHERE id=99',(path,))
        self.conn.commit()
        with self.assertRaisesRegex(RepairConflict,'shared'):
            self.apply()
        self.assertEqual(state(self.conn,98)['offer']['offer_status'],'signed')

    def test_cli_dryrun_is_readonly_and_reports_all_three_records(self):
        old=[state(self.conn,i) for i in (106,156,98)]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            main(['--database',str(self.path),'--actor-id','2'])
        self.assertIn('"mode": "dryrun"',output.getvalue())
        self.assertEqual([state(self.conn,i) for i in (106,156,98)],old)

    def test_real_homepage_uses_new_acceptances_without_invented_signature_completion(self):
        from student_management_summary import load_student_management_summary_map
        self.apply()
        result=load_student_management_summary_map(self.conn,('20260066','20260071','20260105'))
        self.assertEqual(result['20260066'].stage_key,'offer_acceptance_steps')
        self.assertEqual(result['20260071'].stage_key,'10_visa_submission')
        self.assertEqual(result['20260105'].stage_key,'no_counted_stage')
