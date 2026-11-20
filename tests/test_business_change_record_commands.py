from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from database import Database
from business_changes.schema import install
from business_changes.contracts import Subject
from business_changes.versions import version_of


class OwnedRecordCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Database(Path(self.temp.name)/'record.db');self.db.initialize()
        with self.db.transaction() as conn:
            install(conn)
            conn.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19')")
            for sid in ('S1','S2'):
                conn.execute("INSERT INTO students(student_id,passport_first_name,passport_last_name,created_date,updated_at,form_data_json) VALUES(?,'Synthetic','Student','2026-11-19','2026-11-19','{}')",(sid,))
        self.allowed=True

    def save(self, *, nonce='synthetic-create', expected=0, record_id=None, after=None):
        from business_changes.record_commands import save_record
        def authorize(view):
            if not self.allowed:raise PermissionError('synthetic revoked permission')
        def operation(view):
            if record_id is None:
                rid=int(view.execute("INSERT INTO offers(student_id,school_name,document_type,offer_status,created_at) VALUES('S1','Synthetic','Offer','received','2026-11-19')").lastrowid)
            else:
                rid=record_id
                view.execute("UPDATE offers SET school_name='Updated' WHERE id=? AND student_id='S1'",(rid,))
            if after:after(view,rid)
            return rid
        with self.db.transaction() as conn:
            return save_record(conn,module_key='Offer',student_id='S1',record_id=record_id,actor={'id':1},
                nonce=nonce,expected_version=expected,payload={'school_name':'Synthetic'},
                occurred_at='2026-11-19T00:00:00+00:00',authorize=authorize,operation=operation)

    def test_create_retry_once_and_revoked_authority_still_checked(self):
        first=self.save();again=self.save()
        self.assertFalse(first.replayed);self.assertTrue(again.replayed)
        self.assertEqual(1,self.db.one('SELECT count(*) FROM offers')[0])
        self.assertIsNotNone(self.db.one("SELECT 1 FROM business_change_impacts WHERE subject_kind='student' AND subject_id='S1'"))
        self.allowed=False
        with self.assertRaises(PermissionError):self.save()

    def test_conflicting_create_or_stale_edit_cannot_overwrite_committed_fact(self):
        first=self.save();rid=first.result['record_id']
        with self.assertRaises(ValueError):self.save(nonce='synthetic-other-create',expected=0)
        with self.assertRaises(ValueError):self.save(record_id=rid,nonce='synthetic-edit',expected=0)
        self.save(record_id=rid,nonce='synthetic-edit',expected=1)
        self.assertEqual('Updated',self.db.one('SELECT school_name FROM offers')[0])

    def test_wrong_owner_or_publishing_failure_roll_back_source_and_audit(self):
        foreign=self.db.execute("INSERT INTO offers(student_id,school_name,document_type,offer_status,created_at) VALUES('S2','Foreign','Offer','received','2026-11-19')")
        with self.assertRaises(ValueError):self.save(record_id=foreign)
        with patch('business_changes.commands._append',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.save()
        self.assertEqual(1,self.db.one('SELECT count(*) FROM offers')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_failure_caught_by_outer_owner_cannot_commit_only_the_source(self):
        from business_changes.record_commands import save_record
        with self.db.transaction() as conn:
            with patch('business_changes.commands._append',side_effect=RuntimeError('synthetic')):
                with self.assertRaises(RuntimeError):
                    save_record(conn,module_key='Offer',student_id='S1',record_id=None,actor={'id':1},
                        nonce='synthetic-caught',expected_version=0,payload={},occurred_at='2026-11-19T00:00:00+00:00',
                        authorize=lambda view:None,operation=lambda view:int(view.execute("INSERT INTO offers(student_id,school_name,document_type,offer_status,created_at) VALUES('S1','Synthetic','Offer','received','2026-11-19')").lastrowid))
        self.assertEqual(0,self.db.one('SELECT count(*) FROM offers')[0])

    def test_creation_encloses_nested_classification_revision_and_retains_replay(self):
        from business_changes.commands import Mutation
        from business_changes.domain import run_domain_write
        def classify(view,rid):
            run_domain_write(view,event_type='offer.school_changed',actor_kind='user',actor_ref='1',
                capture=lambda c:c.execute('SELECT school_name FROM offers WHERE id=?',(rid,)).fetchone()[0],
                operation=lambda c:c.execute("UPDATE offers SET school_name='Nested classification' WHERE id=?",(rid,)),
                describe=lambda c,result,before,after:Mutation('offer.school_changed',Subject('offer',str(rid)),(Subject('student','S1'),)))
        result=self.save(after=classify)
        self.assertEqual(2,result.result['version'])
        self.assertTrue(self.save(after=classify).replayed)

    def test_coe_and_payment_records_use_explicit_owned_modules(self):
        from business_changes.record_commands import save_record
        for module,sql,kind in (
            ('COE + OSHC',"INSERT INTO coe_oshc(student_id,document_kind,file_path,created_at) VALUES('S1','OSHC','','2026-11-19')",'coe'),
            ('Payments',"INSERT INTO payments(student_id,payment_category,amount,currency,created_at) VALUES('S1','Synthetic',88,'AUD','2026-11-19')",'payment'),
            ('Visa',"INSERT INTO visas(student_id,file_path,visa_type,visa_status,created_at) VALUES('S1','','500 Student Visa','pending','2026-11-19')",'visa_record')):
            with self.subTest(module=module):
                with self.db.transaction() as conn:
                    current=version_of(conn,Subject('student','S1'))
                    result=save_record(conn,module_key=module,student_id='S1',record_id=None,actor={'id':1},
                        nonce='synthetic-'+kind,expected_version=current,payload={},occurred_at='2026-11-19T00:00:00+00:00',
                        authorize=lambda view:None,operation=lambda view:int(view.execute(sql).lastrowid))
                self.assertIsNotNone(self.db.one('SELECT 1 FROM business_change_impacts WHERE subject_kind=? AND subject_id=?',
                    (kind,str(result.result['record_id']))))

    def test_contract_updates_own_linked_or_standalone_source_without_crossing_student(self):
        from business_changes.record_commands import save_record
        for owner in ('S1',None):
            cid=self.db.execute("INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,contract_title,contract_status,service_scope_json,generated_at,updated_at) VALUES(?,?,'Study','StudyAbroadContract','Synthetic','Sent','[]','2026-11-19','2026-11-19')",(owner,'SYNTHETIC-'+str(owner)))
            def save(student_id,nonce):
                with self.db.transaction() as conn:
                    def operation(view):
                        view.execute("UPDATE contracts SET contract_status='ParentSigned' WHERE id=?",(cid,));return cid
                    return save_record(conn,module_key='Contracts',student_id=student_id,record_id=cid,actor={'id':1},
                        nonce=nonce,expected_version=0,payload={'contract_status':'ParentSigned'},occurred_at='2026-11-19T00:00:00+00:00',
                        authorize=lambda view:None,operation=operation)
            with self.subTest(owner=owner):
                with self.assertRaises(ValueError):save('S2','foreign-contract-'+str(cid))
                first=save(owner,'owned-contract-'+str(cid));second=save(owner,'owned-contract-'+str(cid))
                self.assertFalse(first.replayed);self.assertTrue(second.replayed)
                self.assertEqual('ParentSigned',self.db.one('SELECT contract_status FROM contracts WHERE id=?',(cid,))[0])
