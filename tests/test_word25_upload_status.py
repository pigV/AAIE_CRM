"""Confirmed Word cases use real Review persistence and canonical projections."""
import io
import unittest
from tests import test_business_change_school_chain as fixture
import app


class Word25UploadStatusTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SchoolMaterialChainTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.h=self.f.h;self.sid=self.f.sid;self.cycle=self.f.cycle

    def test_new_higher_education_offer_is_bound_to_current_cycle_and_advances(self):
        self.db.execute("UPDATE students SET application_stream='higher_education' WHERE student_id=?",(self.sid,))
        self.db.execute("UPDATE application_cycles SET education_stream='higher_education' WHERE id=?",(self.cycle,))
        self.f.review('Offer','Offer',{'school_name':'Synthetic University'})
        row=self.db.one('SELECT application_cycle_id FROM offers WHERE student_id=?',(self.sid,))
        self.assertEqual(self.cycle,row[0])
        self.assertEqual('08_offer_acceptance',self.f.stage())

    def coe_chain(self,given='Match',family='Student',can_select=True,signed_kind='SignedOffer',before_coe=None,identity=True):
        from reportlab.pdfgen import canvas
        self.db.execute("UPDATE students SET passport_first_name='Match',passport_last_name='Student' WHERE student_id=?",(self.sid,))
        city=self.db.execute("INSERT INTO school_cities(city_name,state) VALUES('Word25 City','VIC')")
        school='Synthetic Word25 School'
        self.db.execute("INSERT INTO schools(school_name,city_id,sector_key,school_type,state,created_at,updated_at) VALUES(?,?,'public','Secondary','VIC',?,?)",(school,city,self.f.stamp,self.f.stamp))
        self.f.review('Offer',signed_kind,{'school_name':school},extra={'extract_offer_date_0':'2026-11-01','extract_offer_expiry_date_0':'2027-12-31'})
        if before_coe:before_coe()
        protected=[r[0] for r in self.db.all("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'revenue_%' OR name IN ('payments','contracts'))")]
        before={t:tuple(tuple(r) for r in self.db.all('SELECT * FROM '+t+' ORDER BY rowid')) for t in protected}
        out=io.BytesIO();pdf=canvas.Canvas(out)
        for i,text in enumerate(['Overseas Student Confirmation-of-Enrolment (CoE)','B. COURSE DETAILS','Provider: Department of Education [00861K]','Course: Secondary (7-12) [019048F]','Course Start Date: 27/01/2027','Course End Date: 20/12/2030','C. STUDENT DETAILS','Family Name: '+family,'Given Names: '+given,'D. NOTES']):pdf.drawString(30,790-24*i,text)
        pdf.save();data=out.getvalue() if identity else self.f.pdf('SYNTHETIC COE - no readable identity')
        token=app.create_upload_review(self.sid,'COE + OSHC',[app.Upload('files','synthetic-coe.pdf','application/pdf',data)],self.f.actor,{'document_kind':'COE'})
        self.h.form={'csrf':'synthetic','confirm_item_0':'1','document_type_0':'COE','intake_defer_0':'1','extract_coe_provider_0':'Department of Education [00861K]','extract_coe_course_0':'Secondary (7-12) [019048F]','extract_coe_start_date_0':'2027-01-27','extract_coe_end_date_0':'2030-12-20'}
        if can_select:
            result=self.h.upload_review_confirm('POST',self.sid,token)
        else:
            from unittest.mock import patch
            with patch.object(app,'can_workflow_module_save',side_effect=lambda user,module:module!='Offer'):
                result=self.h.upload_review_confirm('POST',self.sid,token)
        self.assertEqual('success',result[2],result)
        for table,rows in before.items():self.assertEqual(rows,tuple(tuple(r) for r in self.db.all('SELECT * FROM '+table+' ORDER BY rowid')),table)
        return token

    def test_matching_reviewed_coe_advances_without_inventing_payment(self):
        self.coe_chain()
        self.assertEqual('10_visa_submission',self.f.stage())
        self.assertEqual(1,self.db.one('SELECT count(*) FROM final_school_coe_confirmations')[0])
        self.assertIsNone(self.db.one('SELECT payment_completed_at FROM cycle_final_acceptances')[0])

    def test_wrong_student_coe_never_advances_or_creates_acceptance(self):
        self.coe_chain(given='Someone',family='Else')
        self.assertNotEqual('10_visa_submission',self.f.stage())
        self.assertEqual(0,self.db.one('SELECT count(*) FROM final_school_coe_confirmations')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM cycle_final_acceptances')[0])

    def test_coe_only_permission_saves_material_without_selecting_final_offer(self):
        self.coe_chain(can_select=False)
        self.assertNotEqual('10_visa_submission',self.f.stage())
        self.assertEqual(0,self.db.one('SELECT count(*) FROM cycle_final_acceptances')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM coe_oshc')[0])

    def test_offer_acceptance_document_and_matching_coe_complete_the_same_chain(self):
        self.coe_chain(signed_kind='OfferAcceptance')
        self.assertEqual('10_visa_submission',self.f.stage())
        self.assertEqual(1,self.db.one('SELECT count(*) FROM final_school_coe_confirmations')[0])

    def test_two_signed_offers_require_explicit_final_selection(self):
        self.coe_chain(before_coe=lambda:self.f.review('Offer','SignedOffer',{'school_name':'Synthetic Word25 School'}))
        self.assertEqual(0,self.db.one('SELECT count(*) FROM cycle_final_acceptances')[0])

    def test_expired_offer_or_inactive_signed_attachment_never_auto_selects(self):
        self.coe_chain(before_coe=lambda:self.db.execute("UPDATE offers SET offer_expiry_date='2000-01-01' WHERE student_id=?",(self.sid,)))
        self.assertEqual(0,self.db.one('SELECT count(*) FROM cycle_final_acceptances')[0])

    def test_unreadable_identity_is_retained_for_staff_review_without_advancing(self):
        self.coe_chain(identity=False)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM cycle_final_acceptances')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM coe_oshc')[0])

    def test_repeat_coe_confirmation_and_withdrawn_acceptance_do_not_create_or_restore_selection(self):
        from reviewed_upload_workflow import prepare_coe_identity,confirm_matching_school_coe
        from workflow_offer_events import withdraw_final_acceptance
        from pathlib import Path
        self.coe_chain()
        coe=self.db.one('SELECT id,file_path FROM coe_oshc')
        identity=prepare_coe_identity(Path(coe['file_path']).read_bytes())
        with self.db.transaction() as c:
            self.assertTrue(confirm_matching_school_coe(c,self.sid,coe['id'],identity,self.f.actor,self.f.stamp,can_select_offer=True))
        self.assertEqual(1,self.db.one('SELECT count(*) FROM cycle_final_acceptances')[0])
        with self.db.transaction() as c:
            withdraw_final_acceptance(c,self.cycle,occurred_at=self.f.stamp,actor_user_id=self.f.actor['id'],reason='synthetic withdrawal')
            self.assertFalse(confirm_matching_school_coe(c,self.sid,coe['id'],identity,self.f.actor,self.f.stamp,can_select_offer=True))
        self.assertEqual(0,self.db.one("SELECT count(*) FROM cycle_final_acceptances WHERE acceptance_status='active'")[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM cycle_final_acceptances')[0])

    def test_explicit_cycle_selection_survives_real_upload_review_request(self):
        row=dict(self.db.one('SELECT * FROM application_cycles WHERE id=?',(self.cycle,)))
        row.pop('id');row['cycle_uid']='word25-second';row['sequence_no']+=1;row['education_stream']='higher_education'
        second=self.db.execute('INSERT INTO application_cycles('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        self.f.review('Offer','Offer',{'school_name':'Synthetic University'},extra={'offer_application_cycle_id':str(second)})
        self.assertEqual(second,self.db.one('SELECT application_cycle_id FROM offers')[0])

    def test_invalid_posted_cycle_rolls_back_review_material_and_offer(self):
        token=app.create_upload_review(self.sid,'Offer',[app.Upload('files','synthetic-offer.pdf','application/pdf',self.f.pdf('SYNTHETIC ONLY'))],self.f.actor,{'school_name':'Synthetic University'})
        self.h.form={'csrf':'synthetic','confirm_item_0':'1','document_type_0':'Offer','offer_application_cycle_id':'999999'}
        result=self.h.upload_review_confirm('POST',self.sid,token)
        self.assertEqual('error',result[2])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM offers')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])

    def test_inactive_signed_document_is_not_acceptance_evidence(self):
        self.coe_chain(before_coe=lambda:self.db.execute("UPDATE student_documents SET is_active=0 WHERE student_id=? AND document_type='SignedOffer'",(self.sid,)))
        self.assertEqual(0,self.db.one('SELECT count(*) FROM cycle_final_acceptances')[0])

    def test_withdrawn_offer_event_never_auto_creates_final_selection(self):
        from offer_acceptance import ensure_canonical_offer_event
        from workflow_offer_events import record_offer_state
        def withdraw():
            oid=self.db.one('SELECT id FROM offers')[0]
            with self.db.transaction() as c:
                event=ensure_canonical_offer_event(c,oid,self.f.actor)
                record_offer_state(c,event,state='withdrawn',occurred_at=self.f.stamp,actor_user_id=self.f.actor['id'],reason='synthetic invalidated receipt')
        self.coe_chain(before_coe=withdraw)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM cycle_final_acceptances')[0])

    def test_ordinary_review_cannot_silently_rebind_explicit_progression_to_initial(self):
        row=dict(self.db.one('SELECT * FROM application_cycles WHERE id=?',(self.cycle,)))
        row.pop('id');row['cycle_uid']='word25-progression';row['sequence_no']+=1;row['education_stream']='higher_education';row['cycle_type']='progression'
        row['source_student_case_id']=self.db.execute("INSERT INTO student_cases(student_id,case_type,sequence_no,status,active_slot,created_by,created_at,updated_by,updated_at) VALUES(?,'progression',1,'in_progress',1,?,?,?,?)",(self.sid,self.f.actor['id'],self.f.stamp,self.f.actor['id'],self.f.stamp))
        second=self.db.execute('INSERT INTO application_cycles('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        city=self.db.execute("INSERT INTO school_cities(city_name,state) VALUES('Select City','VIC')")
        self.db.execute("INSERT INTO schools(school_name,city_id,sector_key,school_type,state,created_at,updated_at) VALUES('Synthetic Selection School',?,'public','Secondary','VIC',?,?)",(city,self.f.stamp,self.f.stamp))
        token=app.create_upload_review(self.sid,'Offer',[app.Upload('files','synthetic-offer.pdf','application/pdf',self.f.pdf('SYNTHETIC ONLY'))],self.f.actor,{'school_name':'Synthetic Selection School'})
        self.h.form={'csrf':'synthetic','confirm_item_0':'1','document_type_0':'Offer','offer_application_cycle_id':str(second)}
        result=self.h.upload_review_confirm('POST',self.sid,token)
        self.assertEqual('error',result[2])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM offers')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
