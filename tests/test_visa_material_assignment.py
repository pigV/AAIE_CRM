"""Final Item and Current Category share a case-scoped material assignment."""
import json
import sqlite3
import unittest
from tests import test_visa_status_evidence_authority as fixture
import visa_documents_candidate as documents
from visa_document_metadata import effective_metadata, edit_document_metadata
from visa_document_processing_ui import document_edit_html

class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.VisaStatusEvidenceAuthorityTests();self.f.setUp();self.addCleanup(self.f.tearDown);self.c=self.f.c;self.kw=self.f.actor
        self.c.executescript('''CREATE TABLE visa_checklist_items(id INTEGER PRIMARY KEY,visa_case_id INTEGER,category TEXT,material_name TEXT,document_type TEXT,status TEXT,item_status TEXT,document_status TEXT);
        INSERT INTO visa_checklist_items VALUES(10,1,'Statement','Genuine Student Statement','Genuine Student Statement','uploaded','uploaded','uploaded');
        INSERT INTO visa_checklist_items VALUES(11,1,'Financial','Evidence of Funds','Evidence of Funds','missing','missing','missing');
        INSERT INTO visa_checklist_items VALUES(12,2,'Other','Other Case Item','Other Case Item','missing','missing','missing');''')
        sid=self.f.linked('Statement','Genuine Student Statement','Synthetic statement');self.sid=sid
        self.c.execute("UPDATE visa_case_document_links SET document_role='material_item:10'")
        self.c.execute("UPDATE student_documents SET original_name='Statement.pdf' WHERE id=?",(sid,))
        self.doc=documents.register_document(self.c,1,student_document_id=sid,file_path='/synthetic/1.pdf',original_name='Statement.pdf',version=1,sha256='a'*64,category='other',**self.kw)
    def test_current_category_uses_confirmed_final_item_without_get_writes(self):
        before=self.c.total_changes;meta=effective_metadata(self.c,1,self.doc['id'])
        self.assertEqual(10,meta['material_item_id']);self.assertEqual('Genuine Student Statement',meta['material_item_label'])
        self.assertEqual(before,self.c.total_changes)
        html=document_edit_html(1,'csrf',dict(self.doc,**meta))
        self.assertIn('name="material_item_id"',html);self.assertIn('value="10" selected',html)
        self.assertNotIn('Other Case Item',html)
    def test_staff_reassignment_updates_position_counts_and_retains_physical_identity(self):
        before=tuple(self.c.execute('SELECT * FROM visa_case_documents').fetchone())
        result=edit_document_metadata(self.c,1,self.doc['id'],display_name='Statement renamed.pdf',material_item_id=11,**self.kw)
        self.assertEqual(11,result['material_item_id']);self.assertEqual('material_item:11',self.c.execute('SELECT document_role FROM visa_case_document_links').fetchone()[0])
        self.assertEqual([('missing','missing'),('uploaded','uploaded')],[tuple(r) for r in self.c.execute('SELECT status,item_status FROM visa_checklist_items WHERE visa_case_id=1 ORDER BY id')])
        self.assertEqual(before,tuple(self.c.execute('SELECT * FROM visa_case_documents').fetchone()))
        self.assertEqual(1,self.c.execute("SELECT count(*) FROM visa_document_audit WHERE action='material_item_assigned'").fetchone()[0])
        self.assertEqual('Pending',self.f.projection()['result_status'])
    def test_cross_case_and_invalid_target_rejected_atomically(self):
        before=self.c.iterdump();before='\n'.join(before)
        for target in [12,999,'bad',True]:
            with self.assertRaises(ValueError):edit_document_metadata(self.c,1,self.doc['id'],material_item_id=target,**self.kw)
            self.assertEqual(before,'\n'.join(self.c.iterdump()))
    def test_audit_failure_rolls_back_assignment_and_name(self):
        self.c.executescript("CREATE TRIGGER deny_assignment BEFORE INSERT ON visa_document_audit WHEN NEW.action='material_item_assigned' BEGIN SELECT RAISE(ABORT,'deny'); END;")
        with self.assertRaises(sqlite3.IntegrityError):edit_document_metadata(self.c,1,self.doc['id'],material_item_id=11,display_name='renamed.pdf',**self.kw)
        self.assertEqual(10,effective_metadata(self.c,1,self.doc['id'])['material_item_id'])
        self.assertEqual('Statement.pdf',effective_metadata(self.c,1,self.doc['id'])['display_name'])
    def test_removed_link_is_not_resurrected(self):
        self.c.execute("UPDATE visa_case_document_links SET status='Removed'")
        with self.assertRaises(ValueError):edit_document_metadata(self.c,1,self.doc['id'],material_item_id=11,**self.kw)
    def test_repeated_save_is_noop_for_assignment(self):
        edit_document_metadata(self.c,1,self.doc['id'],material_item_id=11,**self.kw)
        n=self.c.execute("SELECT count(*) FROM visa_document_audit WHERE action='material_item_assigned'").fetchone()[0]
        edit_document_metadata(self.c,1,self.doc['id'],material_item_id=11,**self.kw)
        self.assertEqual(n,self.c.execute("SELECT count(*) FROM visa_document_audit WHERE action='material_item_assigned'").fetchone()[0])
    def test_result_slot_does_not_invent_grant_type(self):
        self.c.execute("INSERT INTO visa_checklist_items VALUES(20,1,'Visa Result / S56','IMMI Grant Letter / IMMI Refusal Letter','IMMI Grant Letter / IMMI Refusal Letter','missing','missing','missing')")
        with self.assertRaisesRegex(ValueError,'核实'):
            edit_document_metadata(self.c,1,self.doc['id'],material_item_id=20,**self.kw)
        self.assertEqual(10,effective_metadata(self.c,1,self.doc['id'])['material_item_id'])
        self.assertEqual('Pending',self.f.projection()['result_status'])

    def test_independent_assignment_keeps_original_upload_binding(self):
        self.c.execute('UPDATE visa_cases SET student_id=NULL WHERE id=1');self.c.execute('UPDATE visa_case_documents SET student_document_id=NULL')
        self.c.execute('CREATE TABLE visa_only_material_uploads(visa_case_id INTEGER,item_id INTEGER,document_id INTEGER)')
        self.c.execute('INSERT INTO visa_only_material_uploads VALUES(1,10,?)',(self.doc['id'],))
        edit_document_metadata(self.c,1,self.doc['id'],material_item_id=11,**self.kw)
        from visa_material_reclassification import independent_material_rows
        self.assertEqual(10,self.c.execute('SELECT item_id FROM visa_only_material_uploads').fetchone()[0])
        self.assertEqual([],independent_material_rows(self.c,1,10));self.assertEqual([self.doc['id']],[r['id'] for r in independent_material_rows(self.c,1,11)])

    def test_unplaced_documents_keep_legacy_editor(self):
        self.c.execute('DELETE FROM visa_case_document_links')
        html=document_edit_html(1,'csrf',dict(self.doc,**effective_metadata(self.c,1,self.doc['id'])))
        self.assertNotIn('name="material_item_id"',html)
        self.assertIn('name="category"',html)

    def test_sync_events_cover_assignment_and_original_student_ownership(self):
        from business_changes.schema import install
        self.c.commit();self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
        self.c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
        edit_document_metadata(self.c,1,self.doc['id'],material_item_id=11,**self.kw)
        self.assertEqual(11,effective_metadata(self.c,1,self.doc['id'])['material_item_id'])
        self.assertEqual(1,self.c.execute("SELECT count(*) FROM business_change_events WHERE event_type='visa.document_metadata_changed'").fetchone()[0])

    def test_auto_detected_grant_is_not_staff_confirmation_for_result_placement(self):
        self.c.execute("INSERT INTO visa_checklist_items VALUES(20,1,'Visa Result / S56','IMMI Grant Letter / IMMI Refusal Letter','IMMI Grant Letter / IMMI Refusal Letter','missing','missing','missing')")
        self.c.execute("DELETE FROM visa_document_audit WHERE action='document_category_selected'")
        self.c.execute("UPDATE visa_document_metadata SET auto_category='grant_letter',manual_category=NULL WHERE document_id=?",(self.doc['id'],))
        self.assertEqual('Pending',self.f.projection()['result_status'])
        with self.assertRaisesRegex(ValueError,'核实'):
            edit_document_metadata(self.c,1,self.doc['id'],material_item_id=20,**self.kw)
        self.assertEqual(10,effective_metadata(self.c,1,self.doc['id'])['material_item_id'])
        self.assertEqual('Pending',self.f.projection()['result_status'])

    def test_historical_target_link_can_be_explicitly_selected_without_unique_collision(self):
        self.c.execute('CREATE UNIQUE INDEX real_link_identity ON visa_case_document_links(visa_case_id,student_document_id,document_role)')
        self.c.execute("INSERT INTO visa_case_document_links SELECT visa_case_id,student_document_id,document_id,'Financial','Evidence of Funds','material_item:11',0,'Removed' FROM visa_case_document_links")
        edit_document_metadata(self.c,1,self.doc['id'],material_item_id=11,**self.kw)
        self.assertEqual(11,effective_metadata(self.c,1,self.doc['id'])['material_item_id'])
        rows=self.c.execute('SELECT document_role,used_in_this_case,status FROM visa_case_document_links ORDER BY document_role').fetchall()
        self.assertEqual([('material_item:10',0,'Removed'),('material_item:11',1,'Linked')],[tuple(r) for r in rows])
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM visa_case_document_links').fetchone()[0])
