from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from business_change_sync import BusinessChangeSyncMixin
from business_changes.schema import install


class MaterialUploadBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.addCleanup(self.c.close)
        self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.executescript('''CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT);
          INSERT INTO system_settings VALUES('business_change_sync_v1','enabled');
          CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT);INSERT INTO application_cycles VALUES(7,'S1');
          CREATE TABLE visa_cases(id INTEGER PRIMARY KEY,student_id TEXT,visa_type TEXT,application_cycle_id INTEGER,archived_at TEXT);
          INSERT INTO visa_cases VALUES(1,'S1','500 Student Visa',7,NULL);
          CREATE TABLE student_documents(id INTEGER PRIMARY KEY,student_id TEXT,folder_category TEXT,document_type TEXT,
            original_name TEXT,stored_name TEXT,file_path TEXT,version INTEGER,file_size INTEGER,uploaded_by INTEGER,
            uploaded_at TEXT,is_active INTEGER,document_status TEXT);
          CREATE TABLE visa_case_document_links(id INTEGER PRIMARY KEY,visa_case_id INTEGER,student_document_id INTEGER);
          CREATE TABLE visa_checklist_items(id INTEGER PRIMARY KEY,visa_case_id INTEGER,status TEXT,updated_at TEXT);INSERT INTO visa_checklist_items VALUES(8,1,'missing',NULL);''')
        parent=self
        class DB:
            @contextmanager
            def transaction(db):
                parent.c.execute('BEGIN IMMEDIATE')
                try:yield parent.c;parent.c.commit()
                except BaseException:parent.c.rollback();raise
        self.g={'db':DB(),'can_workflow_module_save':lambda *a:True,'next_version_path':lambda folder,name:(folder/name,1),'now_iso':lambda:'2026-11-19T10:00:00+10:00'}
        self.h=object.__new__(BusinessChangeSyncMixin);self.h.business_change_runtime=lambda:self.g;self.h.user={'id':1}
        self.h.business_change_prepare_upload_review=lambda *args:{'visa_bytes':self.upload.data,'visa_text':''}
        self.h.visa_material_saved_item_category=lambda *a:'Academic'
        self.h.visa_lodgement_document_type=lambda t:'VisaGrantLetter'
        self.h.visa_material_insert_document_link=lambda case,item,did,connection:connection.execute('INSERT INTO visa_case_document_links(visa_case_id,student_document_id) VALUES(?,?)',(1,did))
        self.h.visa_material_mark_item_uploaded_if_missing=lambda cid,iid,connection:connection.execute("UPDATE visa_checklist_items SET status='uploaded' WHERE id=?",(iid,))
        self.h._visa_register_uploaded_document=lambda *a,**kw:None
        self.case=dict(self.c.execute('SELECT * FROM visa_cases').fetchone());self.item={'id':8}
        self.upload=SimpleNamespace(filename='synthetic.pdf',data=b'synthetic fixture')

    def save(self):return self.h.business_change_store_visa_material(self.case,self.item,self.upload,self.root,'other')

    def test_file_source_link_checklist_and_event_are_saved_together(self):
        did=self.save();self.assertEqual(b'synthetic fixture',(self.root/'synthetic.pdf').read_bytes())
        self.assertEqual(did,self.c.execute('SELECT student_document_id FROM visa_case_document_links').fetchone()[0])
        self.assertEqual('uploaded',self.c.execute('SELECT status FROM visa_checklist_items').fetchone()[0])
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_registration_failure_leaves_no_success_rows_or_orphan_file(self):
        def broken(*a,**kw):raise ValueError('synthetic registration failure')
        self.h._visa_register_uploaded_document=broken
        with self.assertRaises(ValueError):self.save()
        self.assertFalse((self.root/'synthetic.pdf').exists())
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM student_documents').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM visa_case_document_links').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.assertEqual('missing',self.c.execute('SELECT status FROM visa_checklist_items').fetchone()[0])

    def test_view_only_user_cannot_write_file(self):
        self.g['can_workflow_module_save']=lambda *a:False
        with self.assertRaises(PermissionError):self.save()
        self.assertFalse((self.root/'synthetic.pdf').exists())
