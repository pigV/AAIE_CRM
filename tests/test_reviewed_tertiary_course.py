import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

class ReviewedTertiaryCourseTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'STAGING_DATA/tmp')
  self.signed=Path(self.temp.name)/'signed.pdf';self.signed.write_bytes(b'original signed foundation')
  self.coe=Path(self.temp.name)/'coe.pdf';self.coe.write_bytes(b'original foundation coe')
  self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
  self.c.executescript('''CREATE TABLE students(student_id,deleted_at);INSERT INTO students VALUES('S',NULL);
  CREATE TABLE roles(id,name);INSERT INTO roles VALUES(1,'Admin');
  CREATE TABLE users(id,is_active,role_id,deleted_at);INSERT INTO users VALUES(1,1,1,NULL);
  CREATE TABLE application_cycles(id,student_id,status,cycle_type,education_stream);INSERT INTO application_cycles VALUES(10,'S','active','initial','higher_education');
  CREATE TABLE offers(id,student_id,application_cycle_id,school_name,offer_status,document_type,offer_file_path,signed_offer_file_path,offer_date,offer_expiry_date,course_start_date,accepted_date);
  CREATE TABLE coe_oshc(id,student_id,application_cycle_id,document_kind,file_path,coe_provider,school_name,coe_course,coe_start_date,coe_end_date);
  CREATE TABLE student_documents(id,student_id,file_path,document_type,is_active,document_status,voided_at,document_date);
  CREATE TABLE offer_acceptance_history(id,new_offer_id,student_id,previous_offer_restored_status);
  INSERT INTO offer_acceptance_history VALUES(1,20,'S','__document_acceptance__');
  CREATE TABLE cycle_final_acceptances(application_cycle_id,school_name_snapshot,acceptance_status,offer_received_event_id);
  CREATE TABLE offer_received_events(id,legacy_offer_id,application_cycle_id,transfer_branch_id);
  CREATE TABLE offer_received_event_state_events(id,offer_received_event_id,state,occurred_at);
  CREATE TABLE higher_education_coe_links(coe_oshc_id);
  CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id,action_type,module_name,module_key,record_id,student_id,old_value,new_value,created_at);
  ''')
  self.c.execute("INSERT INTO offers VALUES(20,'S',10,'Example University','accepted','SignedOffer',?,?,'2026-09-16',NULL,'2027-02-01','2026-11-25')",(str(self.signed),str(self.signed)))
  self.c.execute("INSERT INTO coe_oshc VALUES(30,'S',10,'COE',?,'Example University [00026A]','Example University','Foundation Course [022310D]','2027-02-01','2028-01-28')",(str(self.coe),))
  self.c.execute("INSERT INTO student_documents VALUES(40,'S',?,'SignedOffer',1,'active',NULL,'2026-09-17')",(str(self.signed),))
  self.c.execute("INSERT INTO student_documents VALUES(41,'S',?,'COE',1,'active',NULL,NULL)",(str(self.coe),));self.c.commit()
 def tearDown(self):self.c.close();self.temp.cleanup()
 def confirm(self,**kw):
  from reviewed_tertiary_course import confirm_course_link
  args=dict(student_id='S',cycle_id=10,offer_id=20,coe_id=30,course_name='Foundation Course',course_cricos='022310D',provider_cricos='00026A',course_start_date='2027-02-01',course_end_date='2028-01-28',signed_date='2026-09-17',actor_id=1,reason='User verified exact original course',stamp='2026-11-25T00:00:00+11:00');args.update(kw)
  return confirm_course_link(self.c,**args)
 def load(self):
  from reviewed_tertiary_course import load_course_links
  return load_course_links(self.c,[10])
 def test_confirm_read_retry_and_only_audit_write(self):
  self.c.execute('BEGIN');before=self.c.total_changes;self.assertTrue(self.confirm());self.assertEqual(before+1,self.c.total_changes)
  n=self.c.total_changes;self.assertFalse(self.confirm());self.assertEqual(n,self.c.total_changes)
  row=self.load()[10][0];self.assertEqual(30,row['coe_id']);self.assertEqual('2026-09-17',row['signed_date']);self.assertFalse(row['principal_bachelor_accepted'])
 def test_read_fails_closed_on_stale_or_wrong_identity(self):
  changes=["UPDATE offers SET offer_status='withdrawn'", "UPDATE offers SET student_id='OTHER'", "UPDATE coe_oshc SET application_cycle_id=99", "UPDATE coe_oshc SET coe_course='Bachelor Course [115421A]'", "UPDATE coe_oshc SET coe_provider='Other [99999X]'", "UPDATE coe_oshc SET coe_end_date='2029-01-01'", "UPDATE student_documents SET document_date='2026-09-18' WHERE id=40", "UPDATE student_documents SET is_active=0 WHERE id=41", "UPDATE application_cycles SET status='closed'", "UPDATE students SET deleted_at='2026-11-25'"]
  self.c.execute('BEGIN');self.confirm()
  for sql in changes:
   with self.subTest(sql=sql):
    self.c.execute('SAVEPOINT bad');self.c.execute(sql);self.assertEqual({},self.load());self.c.execute('ROLLBACK TO bad');self.c.execute('RELEASE bad')
 def test_file_hash_change_or_missing_disables_read(self):
  self.c.execute('BEGIN');self.confirm();self.signed.write_bytes(b'changed');self.assertEqual({},self.load())
 def test_canonical_conflicts_and_native_coe_owner_rejected(self):
  self.c.execute('BEGIN');self.confirm()
  for sql in ["INSERT INTO cycle_final_acceptances VALUES(10,'Other University','active',NULL)","INSERT INTO cycle_final_acceptances VALUES(10,'Example University','withdrawn',NULL)","INSERT INTO higher_education_coe_links VALUES(30)","INSERT INTO offer_received_events VALUES(1,20,10,NULL); INSERT INTO offer_received_event_state_events VALUES(1,1,'WITHDRAWN','2026-11-25')"]:
   with self.subTest(sql=sql):
    self.c.execute('SAVEPOINT bad')
    for statement in sql.split(';'):self.c.execute(statement)
    self.assertEqual({},self.load());self.c.execute('ROLLBACK TO bad');self.c.execute('RELEASE bad')
  self.c.execute("INSERT INTO cycle_final_acceptances VALUES(10,'Unrelated Old School','withdrawn',NULL)");self.assertIn(10,self.load())
 def test_wrong_inputs_no_audit_and_role_enforced(self):
  self.c.execute('BEGIN')
  for args in [{'coe_id':99},{'course_cricos':'115421A'},{'signed_date':'2027-03-01'},{'student_id':'OTHER'},{'actor_id':99}]:
   with self.subTest(args=args),self.assertRaises((ValueError,PermissionError)):self.confirm(**args)
  self.assertEqual(0,self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
 def test_explicit_wrong_document_cycle_rejected(self):
  self.c.execute('ALTER TABLE student_documents ADD COLUMN application_cycle_id')
  self.c.commit();self.c.execute('BEGIN');self.confirm()
  self.c.execute('UPDATE student_documents SET application_cycle_id=999 WHERE id=40');self.assertEqual({},self.load())
 def test_duplicate_active_document_rejected(self):
  self.c.execute('BEGIN');self.confirm()
  self.c.execute('INSERT INTO student_documents SELECT 42,student_id,file_path,document_type,is_active,document_status,voided_at,document_date FROM student_documents WHERE id=41');self.assertEqual({},self.load())
 def test_missing_file_and_changed_coe_bytes_rejected(self):
  self.c.execute('BEGIN');self.confirm();self.coe.write_bytes(b'changed CoE');self.assertEqual({},self.load())
  self.coe.unlink();self.assertEqual({},self.load())
 def test_same_offer_with_wrong_school_snapshot_withdrawn_still_blocks(self):
  self.c.execute('BEGIN');self.confirm();self.c.execute('INSERT INTO offer_received_events VALUES(7,20,10,NULL)')
  self.c.execute("INSERT INTO cycle_final_acceptances VALUES(10,'Wrong Historical Label','withdrawn',7)");self.assertEqual({},self.load())
 def test_expired_signature_and_nontransaction_cannot_confirm(self):
  with self.assertRaises(ValueError):self.confirm()
  self.c.execute('BEGIN');self.c.execute("UPDATE offers SET offer_expiry_date='2026-09-16'")
  with self.assertRaises(ValueError):self.confirm()
 def test_document_and_provider_identity_changes_not_hidden_by_audit(self):
  self.c.execute('BEGIN');self.confirm();self.c.execute("UPDATE offers SET school_name='Different University'");self.assertEqual({},self.load())
 def test_latest_invalid_decision_cannot_fall_back(self):
  self.c.execute('BEGIN');self.confirm();self.c.execute("UPDATE audit_logs SET new_value='{}'");self.assertEqual({},self.load())

if __name__=='__main__':unittest.main()
