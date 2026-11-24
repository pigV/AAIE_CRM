import json,sqlite3,tempfile,unittest
from pathlib import Path

class HistoricalArchiveAccessTests(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
  self.c.executescript('''CREATE TABLE school_plan_reports(id,student_id);
   INSERT INTO school_plan_reports VALUES(1,'S');
   CREATE TABLE school_plan_report_confirmations(id,report_id,student_id,status,confirmed_at);
   INSERT INTO school_plan_report_confirmations VALUES(2,1,'S','confirmed','2026-01-01');
   CREATE TABLE school_plan_report_events(id,report_id,event_type,metadata_json);
   CREATE TABLE student_documents(id,student_id,folder_category,document_type,is_active,document_status,voided_at,file_path,file_size,stored_name);
   CREATE TABLE contracts(id,student_id);
   INSERT INTO contracts VALUES(10,'S');
   CREATE TABLE contract_signatures(id,contract_id,student_id,status,archive_status);
   INSERT INTO contract_signatures VALUES(11,10,'S','signed','archived');
   CREATE TABLE audit_logs(id,action_type,module_name,record_id,student_id,new_value);
  ''')
  self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'STAGING_DATA/tmp');self.root=Path(self.tmp.name)
  self.payload=b'%PDF-1.4\nORIGINAL ARCHIVE\n%%EOF';self.path=self.root/'original.pdf';self.path.write_bytes(self.payload)
  for did,kind in [(3,'ParentSignaturePage'),(4,'FinalConfirmedSchoolPlan'),(12,'ContractSignatureConfirmationPage')]:
   self.c.execute("INSERT INTO student_documents VALUES(?,'S','Applications',?,1,'active',NULL,?,?,?)",(did,kind,str(self.path),len(self.payload),'original.pdf'))
  self.c.execute("INSERT INTO school_plan_report_events VALUES(1,1,'confirmation_archived_to_application',?)",(json.dumps({'confirmation_id':2,'signature_document_id':3,'final_document_id':4}),))
  self.c.execute("INSERT INTO audit_logs VALUES(1,'contract_signature_archived_to_application','contracts','10','S',?)",(json.dumps({'signature_id':11,'confirmation_document_id':12}),));self.c.commit()
 def tearDown(self):self.tmp.cleanup();self.c.close()
 def test_original_confirmation_and_report_use_existing_same_record_archive(self):
  from historical_archive_access import school_archive,archive_bytes
  before=self.c.total_changes
  doc=school_archive(self.c,report_id=1,confirmation_id=2);self.assertEqual(3,doc.document_id)
  self.assertEqual(self.payload,archive_bytes(doc,self.root))
  self.assertEqual(4,school_archive(self.c,report_id=1,kind='report').document_id)
  self.assertEqual(before,self.c.total_changes)
 def test_contract_confirmation_archive_does_not_generate_a_signature(self):
  from historical_archive_access import contract_archive,archive_bytes
  d=contract_archive(self.c,contract_id=10,signature_id=11)
  self.assertEqual(12,d.document_id);self.assertEqual(self.payload,archive_bytes(d,self.root))
 def test_unbound_cross_student_or_wrong_role_never_used(self):
  from historical_archive_access import school_archive
  for sql in ["UPDATE student_documents SET student_id='OTHER' WHERE id=3","UPDATE student_documents SET document_type='Other' WHERE id=3","UPDATE student_documents SET document_status='voided' WHERE id=3","UPDATE school_plan_report_confirmations SET student_id='OTHER'","UPDATE school_plan_report_confirmations SET status='pending'"]:
   with self.subTest(sql=sql):
    self.c.execute('SAVEPOINT invalid');self.c.execute(sql);self.assertIsNone(school_archive(self.c,report_id=1,confirmation_id=2));self.c.execute('ROLLBACK TO invalid');self.c.execute('RELEASE invalid')
  self.assertIsNone(school_archive(self.c,report_id=1,confirmation_id=99))
 def test_escaping_path_size_or_checksum_is_rejected(self):
  from dataclasses import replace
  from historical_archive_access import school_archive,archive_bytes
  d=school_archive(self.c,report_id=1,confirmation_id=2)
  with self.assertRaises(ValueError):archive_bytes(d,self.root/'other')
  with self.assertRaises(ValueError):archive_bytes(replace(d,file_size=1),self.root)
  with self.assertRaises(ValueError):archive_bytes(replace(d,sha256='bad'),self.root)
 def test_latest_exact_binding_does_not_fall_back_to_old_void_version(self):
  from historical_archive_access import school_archive
  self.c.execute("INSERT INTO school_plan_report_events VALUES(2,1,'confirmation_archived_to_application',?)",(json.dumps({'confirmation_id':2,'signature_document_id':999}),))
  self.assertIsNone(school_archive(self.c,report_id=1,confirmation_id=2))

if __name__=='__main__':unittest.main()
