"""Real source bytes in a synthetic, isolated workflow; no real student writes.

Sources include an NSW CoE cover letter and a real Avalon language certificate.
Commission rules are deliberately not invented from student materials.
"""
from pathlib import Path
import hashlib
import os
import unittest

import app
from tests import test_school_course_native_review as native

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('AAIE_CRM_NSW_COVER_ORIGINAL', str(ROOT/'reports/word-triage/continued-course-acceptance/b7bc0a2ccb-S-2487510-01 - CoE Cover Letter.pdf')))
AVALON_SOURCE = Path(os.environ.get('AAIE_CRM_AVALON_COE_ORIGINAL', str(ROOT/'STAGING_DATA/tmp/continued-course-sources/Yunxi-TIAN-Avalon-CoE-Original.pdf')))


@unittest.skipUnless(SOURCE.is_file(), 'Private original not supplied: AAIE_CRM_NSW_COVER_ORIGINAL')
class RealCoverLetterNativeTests(unittest.TestCase):
    def setUp(self):
        native.NativeCourseReviewTests.setUp(self)

    def test_real_nsw_cover_letter_upload_save_readback_preserves_course_dates_without_invented_finance(self):
        f = self.fixture
        data = SOURCE.read_bytes()
        self.assertEqual('b7bc0a2ccb', hashlib.sha256(data).hexdigest()[:10])
        upload = app.Upload('files', SOURCE.name, 'application/pdf', data)
        inferred = app.infer_coe_oshc_details(upload, 'LetterOfConfirmation')
        self.assertEqual('2027-02-03', inferred['letter_language_start_date'])
        self.assertEqual('2027-07-02', inferred['letter_language_end_date'])
        self.assertEqual('2027-07-20', inferred['letter_main_start_date'])
        self.assertIn('Marrickville', inferred['letter_language_course_name'])
        self.assertIn('Dulwich', inferred['letter_main_course_name'])
        before = self.db.one('SELECT count(*) FROM revenue_school_courses')[0]
        token = app.create_upload_review(f.sid, 'COE + OSHC', [upload], f.actor,
                                        {'application_cycle_id':str(f.cycle)})
        _, review = app.load_upload_review(token)
        item = review['files'][0]['item_id']
        fields = dict(csrf='synthetic', confirm_item_0='1', document_type_0='LetterOfConfirmation',
                      application_cycle_id=str(f.cycle))
        for key in ('letter_language_course_name','letter_language_start_date','letter_language_end_date',
                    'letter_main_course_name','letter_main_start_date','letter_main_end_date','letter_year_level'):
            fields[f'extract_{key}_0'] = inferred.get(key, '')
        self.h.form = fields
        result = self.h.upload_review_confirm('POST', f.sid, token)
        self.assertEqual('success', result[2], result)
        doc = self.db.one('SELECT * FROM student_documents WHERE original_name=? AND is_active=1', (SOURCE.name,))
        self.assertIsNotNone(doc)
        self.assertEqual(data, Path(doc['file_path']).read_bytes())
        coe = self.db.one('SELECT * FROM coe_oshc WHERE file_path=?', (doc['file_path'],))
        self.assertEqual('LetterOfConfirmation', coe['document_kind'])
        self.assertEqual(inferred['letter_language_course_name'], coe['letter_language_course_name'])
        self.assertEqual('2027-07-20', coe['letter_main_start_date'])
        self.assertEqual(before, self.db.one('SELECT count(*) FROM revenue_school_courses')[0])
        # Multi-year amounts 44,400 and 42,000 must not silently become first-year facts.
        self.assertEqual(0, self.db.one('SELECT count(*) FROM revenue_tuition_facts WHERE tuition_cents IN (4440000,4200000)')[0])
        self.assertEqual('Confirmed', self.db.one('SELECT status FROM upload_review_items WHERE id=?', (item,))[0])


@unittest.skipUnless(AVALON_SOURCE.is_file(), 'Private original not supplied: AAIE_CRM_AVALON_COE_ORIGINAL')
class RealAvalonCoENativeTests(unittest.TestCase):
    def setUp(self):
        native.NativeCourseReviewTests.setUp(self)

    def test_real_private_language_coe_retries_wrong_school_and_records_only_actual_tuition(self):
        from business_changes.contracts import Subject
        from business_changes.versions import version_of
        from revenue.course_commissions import evidence_current
        f = self.fixture
        source = AVALON_SOURCE
        data = source.read_bytes()
        self.assertEqual('f0a815f15bb0b8a81a0d26ad9f9354de0c1ae76948183a50230e0e33e4e5b5e2',
                         hashlib.sha256(data).hexdigest())
        # Accepted Offer and student remain explicitly synthetic fixtures.
        # The school identity, dates and fee are transcribed from rendered source page 1.
        offer = self.db.one('''SELECT o.id,o.school_id FROM offers o
            JOIN offer_received_events e ON e.legacy_offer_id=o.id
            JOIN cycle_final_acceptances a ON a.offer_received_event_id=e.id AND a.acceptance_status='active'
            WHERE o.student_id=? AND a.application_cycle_id=?''', (f.sid,f.cycle))
        self.db.execute("UPDATE schools SET school_name='Avalon College',sector_key='private',state='VIC' WHERE id=?", (offer['school_id'],))
        other = self.db.execute("INSERT INTO schools(school_name,sector_key,school_type,state,created_at,updated_at) VALUES('SYNTHETIC OTHER MAIN SCHOOL','private','Secondary','VIC',?,?)", (f.stamp,f.stamp))
        upload = app.Upload('files',source.name,'application/pdf',data)
        token = app.create_upload_review(f.sid,'COE + OSHC',[upload],f.actor,
                                        {'application_cycle_id':str(f.cycle)})
        _, review = app.load_upload_review(token)
        item = review['files'][0]['item_id']
        fields = dict(csrf='synthetic', confirm_item_0='1', document_type_0='COE',
            application_cycle_id=str(f.cycle),course_mode_0='language',
            course_language_school_0=str(other),course_language_offer_0=str(offer['id']),
            course_language_name_0='English Language Preparation for Secondary Schools - Pre-Intermediate',
            course_language_aud_0='14040',course_language_confirmed_0='1',
            course_language_reference_0='Original Avalon CoE 1128D3111 page 1 B, Total Tuition Fee AUD 14,040; non-tuition 1,160 excluded',
            extract_coe_provider_0='Avalon College Pty Ltd [04076C]',
            extract_school_name_0='Avalon College',
            extract_coe_course_0='English Language Preparation for Secondary Schools - Pre-Intermediate [111514F]',
            extract_coe_start_date_0='2026-10-09',extract_coe_end_date_0='2026-12-06')
        before_coe = self.db.one('SELECT count(*) FROM coe_oshc')[0]
        before_lines = self.db.one('SELECT count(*) FROM revenue_commission_lines')[0]
        before_docs = self.db.one('SELECT count(*) FROM student_documents')[0]
        before_finance_docs = self.db.one('SELECT count(*) FROM revenue_documents')[0]
        before_finance_audit = self.db.one('SELECT count(*) FROM revenue_audit_events')[0]
        before_tuition = self.db.one('SELECT count(*) FROM revenue_tuition_facts')[0]
        before_files = {str(p.relative_to(f.student_root)) for p in f.student_root.rglob('*') if p.is_file()}
        scopes = [Subject('student',f.sid),Subject('application_cycle',str(f.cycle)),Subject('dashboard','workflow')]
        with self.db.read_snapshot() as conn:
            before_versions = [version_of(conn,s) for s in scopes]
        self.h.form = fields
        with self.assertLogs(level='ERROR') as denied:
            wrong = self.h.upload_review_confirm('POST',f.sid,token)
        self.assertEqual('error',wrong[2],wrong)
        self.assertIn('CoE', ' '.join(denied.output))
        self.assertEqual(before_coe,self.db.one('SELECT count(*) FROM coe_oshc')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM revenue_school_courses')[0])
        self.assertEqual('NeedsReview',self.db.one('SELECT status FROM upload_review_items WHERE id=?',(item,))[0])
        self.assertEqual(before_lines,self.db.one('SELECT count(*) FROM revenue_commission_lines')[0])
        self.assertEqual(before_docs,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(before_finance_docs,self.db.one('SELECT count(*) FROM revenue_documents')[0])
        self.assertEqual(before_finance_audit,self.db.one('SELECT count(*) FROM revenue_audit_events')[0])
        self.assertEqual(before_tuition,self.db.one('SELECT count(*) FROM revenue_tuition_facts')[0])
        self.assertEqual(before_files,{str(p.relative_to(f.student_root)) for p in f.student_root.rglob('*') if p.is_file()})
        with self.db.read_snapshot() as conn:
            # The normal review lifecycle may publish its own status/audit change
            # even while the invalid financial write is rolled back.
            after_rejection = [version_of(conn,s) for s in scopes]
            self.assertTrue(all(v>=b for v,b in zip(after_rejection,before_versions)))
        self.h.form = {**fields,'course_language_school_0':str(offer['school_id'])}
        result = self.h.upload_review_confirm('POST',f.sid,token)
        self.assertEqual('success',result[2],result)
        doc = self.db.one('SELECT * FROM student_documents WHERE original_name=? AND is_active=1',(source.name,))
        self.assertEqual(data,Path(doc['file_path']).read_bytes())
        self.assertEqual(before_coe+1,self.db.one('SELECT count(*) FROM coe_oshc')[0])
        self.assertEqual('Confirmed',self.db.one('SELECT status FROM upload_review_items WHERE id=?',(item,))[0])
        course = self.db.one("""SELECT sc.course_kind,sc.school_id,t.academic_period,t.fee_component,t.tuition_cents,
            e.course_id,e.document_id,d.sha256 FROM revenue_school_courses sc
            JOIN revenue_tuition_facts t ON t.enrolment_component_id=sc.enrolment_component_id AND t.status='active'
            JOIN revenue_school_course_evidence e ON e.course_id=sc.id
            JOIN revenue_documents d ON d.id=e.document_id""")
        self.assertEqual(('language',offer['school_id'],'whole_course','language_tuition',1404000),tuple(course)[:5])
        self.assertEqual(hashlib.sha256(data).hexdigest(),course['sha256'])
        self.assertEqual(0,self.db.one("SELECT count(*) FROM revenue_school_courses WHERE course_kind='main'")[0])
        self.assertEqual(0,self.db.one("SELECT count(*) FROM revenue_tuition_facts WHERE tuition_cents IN (116000,1520000)")[0])
        self.assertEqual(before_lines,self.db.one('SELECT count(*) FROM revenue_commission_lines')[0])
        with self.db.read_snapshot() as conn:
            self.assertTrue(evidence_current(conn,course['course_id'],course['document_id']))
            self.assertTrue(all(version_of(conn,s)>v for s,v in zip(scopes,after_rejection)))


if __name__ == '__main__':
    unittest.main()
