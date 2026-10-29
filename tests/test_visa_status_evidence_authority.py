"""Current confirmed material is authority; classifier proposals are not."""
import json
import sqlite3
import unittest
from pathlib import Path

from tests.test_visa_documents_candidate import SCHEMA, STAMP
from visa_document_lifecycle import case_projection
from visa_document_metadata import edit_document_metadata
import visa_documents_candidate as documents

GRANT = 'IMMI Grant Notification\nDate of Grant: 20 January 2025'
ACK = 'Acknowledgement of application'


class VisaStatusEvidenceAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        for migration in ('20260909_037_visa_document_metadata.sql',
                          '20260909_038_visa_processing_dates.sql',
                          '20260914_039_visa_result_status_authority.sql'):
            self.c.executescript((Path(__file__).parents[1] / 'migrations' / migration).read_text())
        self.c.execute("UPDATE visa_cases SET student_id='S1' WHERE id IN (1,2)")
        self.c.execute('''CREATE TABLE visa_case_document_links(
            visa_case_id INTEGER,student_document_id INTEGER,document_id INTEGER,
            document_category TEXT,document_type TEXT,document_role TEXT,
            used_in_this_case INTEGER,status TEXT)''')
        self.text = {}
        self.actor = dict(actor_user_id=1, occurred_at=STAMP)

    def tearDown(self):
        self.c.close()

    def linked(self, category, kind, text, *, case=1, active=1, used=1, status='Linked', source_type='VisaMaterialChecklist'):
        sid = len(self.text) + 1
        self.text[sid] = text
        self.c.execute('INSERT INTO student_documents VALUES(?,?,?,?,?,?,?,?,?)',
                       (sid, 'S1', f'/synthetic/{sid}.pdf', f'{sid}.pdf', 1, source_type, STAMP, active, 'active'))
        self.c.execute('INSERT INTO visa_case_document_links VALUES(?,?,?,?,?,?,?,?)',
                       (case, sid, sid, category, kind, f'material_item:{sid}', used, status))
        return sid

    def ack(self):
        return self.linked('Visa Result / S56', 'IMMI Acknowledgment Letter', ACK)

    def projection(self):
        return case_projection(self.c, 1, read_text=lambda d: self.text.get(d.get('student_document_id'), GRANT))

    def assert_processing(self):
        p = self.projection()
        self.assertEqual(('Pending', '审理中', ''), tuple(p[k] for k in ('result_status', 'workflow_status', 'grant_date')))

    def test_current_ack_only(self):
        self.ack()
        self.assert_processing()

    def test_ack_plus_historical_draft_grant_text(self):
        self.ack()
        self.linked('Visa Result / S56', 'Draft', GRANT)
        self.assert_processing()

    def test_ack_plus_current_other_grant_text(self):
        self.ack()
        self.linked('Visa Result / S56', 'Other', GRANT)
        self.assert_processing()

    def test_current_confirmed_active_grant(self):
        self.linked('Visa Result / S56', 'IMMI Grant Letter / IMMI Refusal Letter', GRANT, source_type='VisaGrantLetter')
        p = self.projection()
        self.assertEqual(('Granted', '已完成', '2025-01-20'), tuple(p[k] for k in ('result_status', 'workflow_status', 'grant_date')))

    def test_deleted_grant_plus_ack(self):
        self.ack()
        self.linked('Visa Result / S56', 'IMMI Grant Letter', GRANT, active=0)
        self.assert_processing()

    def test_previous_case_grant_plus_current_ack(self):
        self.ack()
        self.linked('Visa Result / S56', 'IMMI Grant Letter', GRANT, case=2)
        self.assert_processing()

    def unknown(self):
        d = documents.register_document(self.c, 1, file_path='/synthetic/unknown.pdf',
            original_name='IMMI Grant Notification.pdf', version=1, sha256='a'*64, **self.actor)
        documents.save_extraction(self.c, 1, d['id'], text=GRANT, **self.actor)
        return d

    def test_unknown_batch_proposal_before_confirmation(self):
        self.ack()
        self.unknown()
        self.assert_processing()
        self.assertEqual('Pending', self.c.execute('SELECT result_status FROM visa_cases WHERE id=1').fetchone()[0])

    def test_upload_auto_category_is_not_staff_confirmation(self):
        self.ack()
        d = documents.register_document(self.c, 1, file_path='/synthetic/upload.pdf',
            original_name='IMMI Grant.pdf', version=1, sha256='c'*64,
            category='grant_letter', category_confirmed=False, **self.actor)
        documents.save_extraction(self.c, 1, d['id'], text=GRANT, **self.actor)
        self.assert_processing()
        self.assertEqual('Pending', self.c.execute('SELECT result_status FROM visa_cases WHERE id=1').fetchone()[0])

    def test_old_ambiguous_selected_category_is_not_confirmation(self):
        d = documents.register_document(self.c, 1, file_path='/synthetic/old.pdf',
            original_name='Grant.pdf', version=1, sha256='d'*64, **self.actor)
        documents._audit(self.c, 1, d['id'], 'document_category_selected', {},
                         {'category':'grant_letter'}, 1, STAMP)
        self.assertEqual('未开始', self.projection()['workflow_status'])

    def test_unknown_after_confirmed_classification(self):
        d = self.unknown()
        edit_document_metadata(self.c, 1, d['id'], category='grant_letter', **self.actor)
        self.assertEqual(('Granted', '已完成', '2025-01-20'), tuple(self.projection()[k] for k in ('result_status', 'workflow_status', 'grant_date')))

    def test_existing_reconciliation_is_read_only_and_never_reads_other(self):
        ack = self.ack()
        self.linked('Visa Result / S56', 'Draft', GRANT)
        self.linked('Other', 'IMMI Grant Letter', GRANT, source_type='VisaGrantLetter')
        self.c.execute("UPDATE visa_cases SET status='Granted',result_status='Granted',grant_date='2025-01-20' WHERE id=1")
        before = self.c.total_changes
        reads = []
        def reader(d):
            reads.append(d['student_document_id'])
            return self.text[d['student_document_id']]
        p = case_projection(self.c, 1, read_text=reader)
        self.assertEqual(('Pending', '审理中', ''), tuple(p[k] for k in ('result_status', 'workflow_status', 'grant_date')))
        self.assertEqual([ack], reads)
        self.assertEqual(before, self.c.total_changes)

    def test_removed_or_unused_material_cannot_drive_status(self):
        for kwargs in ({'used': 0}, {'status': 'Deleted'}, {'status': 'Removed'}):
            with self.subTest(kwargs=kwargs):
                self.c.execute('DELETE FROM visa_case_document_links')
                self.linked('Visa Result / S56', 'IMMI Grant Letter', GRANT, **kwargs)
                self.assertEqual('未开始', self.projection()['workflow_status'])

    def test_generic_attachment_typed_grant_is_not_result_material(self):
        self.ack()
        self.linked('Other', 'IMMI Grant Letter', GRANT, source_type='VisaGrantLetter')
        self.assert_processing()

    def test_removed_link_does_not_reserve_hash_or_single_grant(self):
        from visa_document_lifecycle import check_upload, active_documents
        self.linked('Visa Result / S56', 'IMMI Grant Letter', GRANT,
                    status='Removed', source_type='VisaGrantLetter')
        self.assertEqual([], active_documents(self.c, 1))
        check_upload(self.c, 1, 'a'*64, 'grant_letter')

    def test_unconfirmed_proposal_does_not_reserve_single_grant(self):
        self.unknown()
        d = documents.register_document(self.c, 1, file_path='/synthetic/confirmed.pdf',
            original_name='confirmed.pdf', version=1, sha256='e'*64,
            category='grant_letter', **self.actor)
        self.assertEqual('grant_letter', next(row['authority_category'] for row in
            self.projection()['documents'] if row.get('id') == d['id']))

    def test_explicit_other_overrides_link_and_saved_extraction(self):
        sid = self.linked('Visa Result / S56', 'IMMI Grant Letter', GRANT, source_type='VisaGrantLetter')
        d = documents.register_document(self.c, 1, student_document_id=sid, file_path='/synthetic/1.pdf',
            original_name='1.pdf', version=1, sha256='b'*64, **self.actor)
        edit_document_metadata(self.c, 1, d['id'], category='other', **self.actor)
        self.assertEqual('未开始', self.projection()['workflow_status'])

    def test_refusal_text_is_not_canonical_grant_or_ack(self):
        self.linked('Visa History', 'IMMI Refusal Letter', GRANT)
        self.assertEqual('未开始', self.projection()['workflow_status'])

    def test_stale_manual_result_cannot_override_current_material(self):
        from visa_result_status import result_summary
        self.ack()
        self.c.execute("UPDATE visa_cases SET result_status='Granted',decision_status='Granted',status='Granted' WHERE id=1")
        self.assertEqual('Pending', result_summary(self.c, 1)['effective_result'])

    def test_manual_granted_cannot_bypass_current_ack_only(self):
        from visa_result_status import record_manual_result
        self.ack()
        before = self.c.total_changes
        with self.assertRaisesRegex(ValueError, 'active current Visa Result'):
            record_manual_result(self.c, 1, 'Granted', reason='Unconfirmed guess', **self.actor)
        self.assertEqual(before, self.c.total_changes)
        self.assertEqual('Pending', self.c.execute('SELECT result_status FROM visa_cases WHERE id=1').fetchone()[0])

    def test_stale_granted_result_cannot_authorize_other_letter_review(self):
        self.c.execute("INSERT INTO application_cycles(id,student_id,status) VALUES(1,'S1','active')")
        self.c.execute('UPDATE visa_cases SET application_cycle_id=1 WHERE id=1')
        self.c.execute("UPDATE visa_cases SET status='Granted',result_status='Granted',decision_status='Granted' WHERE id=1")
        d = documents.register_document(self.c, 1, file_path='/synthetic/other.pdf',
            original_name='other.pdf', version=1, sha256='f'*64,
            category='other', **self.actor)
        extraction = documents.save_extraction(self.c, 1, d['id'], text=GRANT, **self.actor)
        documents.confirm_letter(self.c, 1, extraction['id'],
            values=json.loads(extraction['proposed_json']), **self.actor)
        self.assertIsNone(self.c.execute('SELECT grant_date FROM visa_cases WHERE id=1').fetchone()[0])
        self.assertEqual(0, self.c.execute('SELECT COUNT(*) FROM visa_case_confirmed_decisions').fetchone()[0])
        self.assertEqual('Pending', self.projection()['result_status'])
