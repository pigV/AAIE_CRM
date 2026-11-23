"""Native HTTP review dispatch, file rollback and corrected retry for language CoE."""
from pathlib import Path
import os
import unittest
from unittest.mock import patch

import app
from tests import test_business_change_school_chain as chain


class NativeCourseReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture=chain.SchoolMaterialChainTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        secure=self.fixture.root/'secure-tuition'
        secure.mkdir(mode=0o700)
        storage=patch.dict(os.environ,{'REVENUE_SECURE_STORAGE_ROOT':str(secure)})
        storage.start();self.addCleanup(storage.stop)
        # Reuse the independently verified native school chain rather than
        # hand-crafting an accepted school with contradictory workflow records.
        self.fixture.test_plan_application_offer_payment_coe_and_evidence_withdrawal_use_current_cycle()
        self.db=self.fixture.db;self.h=self.fixture.h
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='revenue_school_courses'").fetchone() is None:
                from migration_runner import split_sql_statements
                migration=Path(__file__).resolve().parents[1]/'migrations/20261120_052_school_course_tuition.sql'
                for sql in split_sql_statements(migration.read_text()):conn.execute(sql)

    def test_native_language_fee_rejects_wrong_coe_and_retries_same_review_atomically(self):
        f=self.fixture
        offer=self.db.one('''SELECT o.id,o.school_id FROM offers o
            JOIN offer_received_events e ON e.legacy_offer_id=o.id
            JOIN cycle_final_acceptances a ON a.offer_received_event_id=e.id AND a.acceptance_status='active'
            WHERE o.student_id=?''',(f.sid,))
        data=f.pdf('SYNTHETIC ONLY native language CoE - AUD 6000')
        token=app.create_upload_review(f.sid,'COE + OSHC',[
            app.Upload('files','language-native.pdf','application/pdf',data)],f.actor,
            {'school_name':f.school,'application_cycle_id':str(f.cycle)})
        fields=dict(csrf='synthetic',confirm_item_0='1',document_type_0='COE',
            course_mode_0='language',course_language_school_0=str(offer['school_id']),
            course_language_offer_0=str(offer['id']),course_language_name_0='Intensive English',
            course_language_aud_0='6000',course_language_confirmed_0='1',
            course_language_reference_0='SYNTHETIC CoE page 1 language course',
            extract_coe_provider_0='SYNTHETIC WRONG PROVIDER',extract_school_name_0='SYNTHETIC WRONG PROVIDER',
            extract_coe_course_0='Intensive English',extract_coe_start_date_0='2027-01-25',
            extract_coe_end_date_0='2027-06-30',application_cycle_id=str(f.cycle))
        before=self.db.one('SELECT count(*) FROM coe_oshc')[0]
        financial_before=self.db.one('SELECT count(*) FROM revenue_audit_events')[0]
        cases_before=self.db.one('SELECT count(*) FROM revenue_commercial_cases')[0]
        self.h.form=fields
        with self.assertLogs(level='ERROR') as denied:
            result=self.h.upload_review_confirm('POST',f.sid,token)
        self.assertIn('CoE办学机构与实际课程学校不匹配',' '.join(denied.output))
        self.assertEqual('error',result[2],result)
        self.assertEqual(before,self.db.one('SELECT count(*) FROM coe_oshc')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM revenue_school_courses')[0])
        self.assertEqual(financial_before,self.db.one('SELECT count(*) FROM revenue_audit_events')[0])
        self.assertEqual(cases_before,self.db.one('SELECT count(*) FROM revenue_commercial_cases')[0])
        _,review=app.load_upload_review(token)
        item=review['files'][0]['item_id']
        self.assertEqual('NeedsReview',self.db.one('SELECT status FROM upload_review_items WHERE id=?',(item,))[0])
        self.h.form={**fields,'extract_coe_provider_0':f.school,'extract_school_name_0':f.school}
        result=self.h.upload_review_confirm('POST',f.sid,token)
        self.assertEqual('success',result[2],result)
        self.assertEqual(before+1,self.db.one('SELECT count(*) FROM coe_oshc')[0])
        self.assertEqual('Confirmed',self.db.one('SELECT status FROM upload_review_items WHERE id=?',(item,))[0])
        fact=self.db.one('''SELECT t.tuition_cents,t.fee_component FROM revenue_school_courses c
            JOIN revenue_tuition_facts t ON t.enrolment_component_id=c.enrolment_component_id AND t.status='active' ''')
        self.assertEqual((600000,'language_tuition'),tuple(fact))
        doc=self.db.one("SELECT * FROM student_documents WHERE original_name='language-native.pdf' AND is_active=1")
        self.assertEqual(data,Path(doc['file_path']).read_bytes())
        from revenue.course_commissions import evidence_current
        with self.db.read_snapshot() as conn:
            proof=conn.execute('SELECT course_id,document_id FROM revenue_school_course_evidence').fetchone()
            self.assertTrue(evidence_current(conn,proof['course_id'],proof['document_id']))
        self.db.execute("UPDATE student_documents SET is_active=0,document_status='voided',voided_at=? WHERE id=?",(f.stamp,doc['id']))
        with self.db.read_snapshot() as conn:
            self.assertFalse(evidence_current(conn,proof['course_id'],proof['document_id']))

    def test_native_existing_coe_followup_adds_main_fee_without_reupload(self):
        f=self.fixture
        coe=self.db.one('SELECT * FROM coe_oshc WHERE student_id=? ORDER BY id LIMIT 1',(f.sid,))
        offer=self.db.one('''SELECT o.school_id FROM offers o
            JOIN offer_received_events e ON e.legacy_offer_id=o.id
            JOIN cycle_final_acceptances a ON a.offer_received_event_id=e.id AND a.acceptance_status='active'
            WHERE o.student_id=?''',(f.sid,))
        before=self.db.one('SELECT count(*) FROM coe_oshc')[0]
        docs=self.db.one('SELECT count(*) FROM student_documents')[0]
        original=Path(coe['file_path']).read_bytes()
        self.h.form={'course_mode_0':'main','course_main_school_0':str(offer['school_id']),
            'course_main_name_0':'Secondary Year 10','course_main_year_0':'2027','course_main_grade_0':'Year 10',
            'course_main_aud_0':'12000','course_main_confirmed_0':'1','course_main_reference_0':'TEST CoE page 1 main tuition'}
        result=self.h.student_coe_course_confirmation('POST',f.sid,str(coe['id']))
        self.assertEqual('success',result[2],result)
        again=self.h.student_coe_course_confirmation('POST',f.sid,str(coe['id']))
        self.assertEqual('success',again[2],again)
        self.assertEqual(before,self.db.one('SELECT count(*) FROM coe_oshc')[0])
        self.assertEqual(docs,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(original,Path(coe['file_path']).read_bytes())
        course=self.db.one('''SELECT sc.course_kind,t.tuition_cents FROM revenue_school_courses sc
            JOIN revenue_tuition_facts t ON t.enrolment_component_id=sc.enrolment_component_id AND t.status='active' ''')
        self.assertEqual(('main',1200000),tuple(course))
        from revenue.school_courses import course_details
        with self.db.read_snapshot() as conn:details=course_details(conn,f.sid)[0]
        self.h.form={'tuition_aud':'12500','revision':details['revision'],'idempotency_key':'native-course-correction'}
        corrected=self.h.student_course_tuition_update('POST',f.sid,details['uid'])
        self.assertEqual('success',corrected[2],corrected)
        with self.db.read_snapshot() as conn:self.assertEqual(1250000,course_details(conn,f.sid)[0]['tuition_cents'])
        self.assertEqual(before,self.db.one('SELECT count(*) FROM coe_oshc')[0])
        self.assertEqual(original,Path(coe['file_path']).read_bytes())


if __name__=='__main__':unittest.main()

class CoEMainOwnershipTests(unittest.TestCase):
    setUp=NativeCourseReviewTests.setUp
    def test_main_school_can_be_confirmed_at_coe_after_document_acceptance_without_offer_final_selection(self):
        from application_cycle import close_cycle,create_student_cycle
        from offer_acceptance import accept_offer_document
        f=self.fixture
        with self.db.transaction() as c:
            close_cycle(c,f.cycle,'completed',f.stamp,f.actor['id'])
            f.cycle=create_student_cycle(c,f.sid,'initial',f.stamp,f.actor['id']).id
        self.h.form={}
        document=f.review('Offer','Offer',{'school_name':f.school})
        offer=self.db.one('SELECT * FROM offers WHERE student_id=? AND offer_file_path=?',(f.sid,document['file_path']))
        with self.db.transaction() as c:
            accept_offer_document(c,f.sid,offer['id'],dict(f.actor,can_manage_offer=True))
        coe_doc=f.review('COE + OSHC','COE',{'school_name':f.school},
            {'intake_defer_0':'1','extract_coe_provider_0':f.school,'extract_school_name_0':f.school,
             'extract_coe_course_0':'Secondary Year 10','extract_coe_start_date_0':'2027-01-25','extract_coe_end_date_0':'2030-12-31'})
        coe=self.db.one('SELECT id FROM coe_oshc WHERE file_path=?',(coe_doc['file_path'],))
        self.h.form={'course_mode_0':'main','course_main_school_0':str(offer['school_id']),
            'course_main_offer_0':str(offer['id']),'course_main_name_0':'Secondary Year 10',
            'course_main_year_0':'2027','course_main_grade_0':'Year 10','course_main_aud_0':'12000',
            'course_main_confirmed_0':'1','course_main_reference_0':'TEST CoE page 1'}
        result=self.h.student_coe_course_confirmation('POST',f.sid,str(coe['id']))
        self.assertEqual('success',result[2],result)
        selected=self.db.one("SELECT school_id,signed_offer_completed_at,payment_completed_at FROM cycle_final_acceptances WHERE application_cycle_id=? AND acceptance_status='active'",(f.cycle,))
        self.assertEqual((offer['school_id'],None,None),tuple(selected))

class CourseConfirmationPermissionTests(unittest.TestCase):
    setUp=NativeCourseReviewTests.setUp
    def test_upload_confirmation_cannot_create_financial_course_without_tuition_permission(self):
        f=self.fixture
        token=app.create_upload_review(f.sid,'COE + OSHC',[
            app.Upload('files','permission-coe.pdf','application/pdf',f.pdf('SYNTHETIC course permission'))],f.actor,
            {'school_name':f.school,'application_cycle_id':str(f.cycle)})
        school=self.db.one('SELECT id FROM schools WHERE school_name=?',(f.school,))[0]
        offer=self.db.one('SELECT legacy_offer_id FROM offer_received_events WHERE application_cycle_id=? ORDER BY id LIMIT 1',(f.cycle,))[0]
        self.h.form={'csrf':'synthetic','confirm_item_0':'1','document_type_0':'COE','course_mode_0':'language',
            'course_language_school_0':str(school),'course_language_offer_0':str(offer),'course_language_name_0':'Intensive English',
            'course_language_aud_0':'6000','course_language_confirmed_0':'1','course_language_reference_0':'page 1',
            'extract_coe_provider_0':f.school,'extract_school_name_0':f.school,'extract_coe_course_0':'Intensive English',
            'extract_coe_start_date_0':'2027-01-25','extract_coe_end_date_0':'2030-12-31'}
        with self.assertLogs(level='ERROR') as denied,patch.object(app,'can_perform_revenue_action',return_value=False):
            result=self.h.upload_review_confirm('POST',f.sid,token)
        self.assertEqual('error',result[2],result)
        _,review=app.load_upload_review(token)
        item=self.db.one('SELECT error_message FROM upload_review_items WHERE id=?',(review['files'][0]['item_id'],))
        self.assertTrue(item[0])
        self.assertIn('权限',' '.join(denied.output))
        self.assertEqual(0,self.db.one('SELECT count(*) FROM revenue_school_courses')[0])
