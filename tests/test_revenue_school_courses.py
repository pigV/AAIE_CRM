"""Course-separated CoE tuition uses synthetic, isolated financial facts."""
from pathlib import Path
import unittest

from revenue.school_courses import read_course_fields, save_coe_courses, course_details
from tests import test_revenue_student_intake_tuition as intake

ROOT = Path(__file__).resolve().parents[1]


class SchoolCourseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = intake.StudentIntakeTuitionTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.db = self.fixture.db
        self.db.executescript((ROOT/'migrations/20261120_052_school_course_tuition.sql').read_text())
        self.db.execute('ALTER TABLE coe_oshc ADD COLUMN student_id TEXT')
        self.db.execute('ALTER TABLE coe_oshc ADD COLUMN coe_provider TEXT')
        self.db.execute('ALTER TABLE coe_oshc ADD COLUMN coe_course TEXT')
        self.db.execute("INSERT INTO coe_oshc(id,application_cycle_id,student_id) VALUES(991,125,'TEST-PENINSULA-SCHEDULE')")
        self.db.execute("UPDATE coe_oshc SET coe_provider=(SELECT school_name FROM schools WHERE id=16)")
        self.db.execute("UPDATE offers SET offer_status='accepted' WHERE id=22")
        self.db.execute("INSERT INTO offers VALUES(24,'TEST-PENINSULA-SCHEDULE',13,'accepted','/fixture/language.pdf','','language.pdf','Offer')")
        self.db.execute("INSERT INTO offer_received_events(id,application_cycle_id,legacy_offer_id,school_id,student_id) VALUES(24,125,24,13,'TEST-PENINSULA-SCHEDULE')")

    def entry(self, kind, amount, school=16):
        return {'course_kind': kind, 'school_id': school, 'course_name': kind+' course',
                'cents': amount, 'year': 2028, 'grade': 'Year 10','offer_id':24 if school==13 else 22,
                'confirmed':True,'reference':'TEST CoE page 1 course block'}

    def save(self, entries, coe=991, student='TEST-PENINSULA-SCHEDULE'):
        return save_coe_courses(self.db, student, coe, entries, self.fixture.proof('coe', coe), 1)

    def test_shared_coe_separates_components_and_is_idempotent(self):
        entries = [self.entry('language',600000), self.entry('main',1200000)]
        first = self.save(entries)
        self.assertEqual(first, self.save(entries))
        self.assertEqual(2, len(first['courses']))
        self.assertNotEqual(first['courses'][0]['component_uid'], first['courses'][1]['component_uid'])
        facts = list(self.db.execute("SELECT fee_component,tuition_cents FROM revenue_tuition_facts WHERE status='active' AND enrolment_component_id IN (SELECT enrolment_component_id FROM revenue_school_courses) ORDER BY tuition_cents"))
        self.assertEqual([('language_tuition',600000),('tuition',1200000)], [tuple(r) for r in facts])
        self.assertEqual(120000,self.fixture.candidate(125)['gross_cents'])

    def test_new_coe_version_reuses_language_course_and_nonempty_rollback_refuses(self):
        from revenue.repository import RevenueRepository
        from migration_runner import split_sql_statements
        first=self.save([self.entry('language',600000)])
        self.db.execute("INSERT INTO coe_oshc(id,application_cycle_id,student_id,coe_provider) SELECT 992,125,'TEST-PENINSULA-SCHEDULE',school_name FROM schools WHERE id=16")
        second=self.save([self.entry('language',600000)],coe=992)
        self.assertEqual(first['courses'][0]['uid'],second['courses'][0]['uid'])
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM revenue_school_courses').fetchone()[0])
        self.db.execute("INSERT INTO coe_oshc(id,application_cycle_id,student_id,coe_provider) SELECT 993,125,'TEST-PENINSULA-SCHEDULE',school_name FROM schools WHERE id=16")
        with self.assertRaisesRegex(ValueError,'同一CoE原件'):
            self.save([{**self.entry('language',600000),'course_name':'Slightly renamed same language'}],coe=993)
        self.assertEqual(1,self.db.execute("SELECT count(*) FROM revenue_enrolment_components WHERE source_kind='school_language_course'").fetchone()[0])
        import sqlite3
        rollback=(ROOT/'migrations/rollback/20261120_052_school_course_tuition_rollback.sql').read_text()
        with self.assertRaises(sqlite3.IntegrityError):
            with RevenueRepository(self.db).transaction():
                for statement in split_sql_statements(rollback):self.db.execute(statement)
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM revenue_school_courses').fetchone()[0])
        self.assertEqual(2,self.db.execute('SELECT count(*) FROM revenue_school_course_evidence').fetchone()[0])

    def test_existing_coe_followup_adds_missing_main_without_second_language_component(self):
        from revenue.school_courses import confirm_existing_coe_courses
        language=self.entry('language',600000)
        first=self.save([language])
        result=confirm_existing_coe_courses(self.db,'TEST-PENINSULA-SCHEDULE',991,
            [language,self.entry('main',1200000)],self.fixture.proof('coe',991),1)
        self.assertEqual(first['courses'][0]['uid'],result['courses'][0]['uid'])
        self.assertEqual(2,len(result['courses']))
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM coe_oshc').fetchone()[0])
        self.assertEqual(1,self.db.execute("SELECT count(*) FROM revenue_enrolment_components WHERE source_kind='school_language_course'").fetchone()[0])

    def test_language_only_has_no_grade_or_main_tuition(self):
        entry = self.entry('language',600000,13)
        entry.update(year=None,grade='')
        self.db.execute("UPDATE coe_oshc SET coe_provider=(SELECT school_name FROM schools WHERE id=13)")
        self.save([entry])
        self.assertNotIn('gross_cents',self.fixture.candidate(125))
        details = course_details(self.db,'TEST-PENINSULA-SCHEDULE')
        self.assertEqual(13,details[0]['school_id'])
        self.assertEqual('language',details[0]['course_kind'])
        self.assertEqual(600000,details[0]['tuition_cents'])

    def test_private_language_before_final_main_selection_and_provider_conflict(self):
        self.db.execute("UPDATE cycle_final_acceptances SET acceptance_status='withdrawn' WHERE id=125")
        self.db.execute("UPDATE coe_oshc SET coe_provider=(SELECT school_name FROM schools WHERE id=13),coe_course='Intensive English'")
        result=self.save([self.entry('language',600000,13)])
        self.assertEqual('language',result['courses'][0]['course_kind'])
        self.db.execute("INSERT INTO coe_oshc(id,application_cycle_id,student_id,coe_provider,coe_course) SELECT 992,125,'TEST-PENINSULA-SCHEDULE',school_name,'Secondary Education' FROM schools WHERE id=16")
        with self.assertRaisesRegex(ValueError,'Provider|主课'):
            self.save([self.entry('language',600000,13)],coe=992)

    def test_public_authority_state_mismatch_and_unverified_course_are_rejected(self):
        self.db.execute("UPDATE schools SET sector_key='public',state='VIC' WHERE id=16")
        self.db.execute("UPDATE coe_oshc SET coe_provider='NSW Department of Education'")
        with self.assertRaisesRegex(ValueError,'州别'):
            self.save([self.entry('language',600000)])
        self.db.execute("UPDATE coe_oshc SET coe_provider='Department of Education'")
        with self.assertRaisesRegex(ValueError,'州别'):
            self.save([self.entry('language',600000)])
        self.db.execute("UPDATE coe_oshc SET coe_provider='Victorian Department of Education'")
        entry=self.entry('language',600000)
        with self.assertRaises(ValueError):
            self.save([{**entry,'confirmed':False}])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM revenue_school_courses').fetchone()[0])
        self.save([entry])

    def test_shared_public_authority_coe_can_name_two_actual_same_state_schools(self):
        self.db.execute("UPDATE schools SET sector_key='public',state='VIC' WHERE id IN (13,16)")
        self.db.execute("UPDATE coe_oshc SET coe_provider='Victorian Department of Education',coe_course='Secondary Education'")
        language={**self.entry('language',600000,13),'offer_id':22}
        result=self.save([language,self.entry('main',1200000)])
        self.assertEqual([13,16],[r['school_id'] for r in result['courses']])
        self.assertNotEqual(result['courses'][0]['component_uid'],result['courses'][1]['component_uid'])

    def test_normal_contract_editor_records_reviewed_language_rule(self):
        from revenue.commission_terms_editor import CommissionTermsEditor
        workflow=self.fixture.workflow
        catalog={'confirmed':[(None,None,workflow.uid)],'name':'TEST school','active':[]}
        editor=CommissionTermsEditor(workflow.repo,workflow.pilot,catalog,workflow.target,workflow.uid,1)
        html=editor.render('csrf','test-language-form')
        self.assertIn('name="commission_action" value="language_rule"',html)
        terms=editor.save({'commission_action':'language_rule','revision':self.fixture.terms['revision'],
            'rate':'15','period':'whole_course','conditions':'TEST actual attendance',
            'reference':'TEST contract page 2 clause 3','reviewed':'1'},'language-editor-save')
        self.assertEqual(1500,terms['course_rules']['language']['rates']['standard'])
        self.assertEqual(self.fixture.terms['rates'],terms['rates'])
        self.assertEqual(workflow.document['sha256'],terms['course_rules']['language']['pdf_sha256'])

    def test_language_estimate_view_is_separate_from_main_nsw_iec_fields(self):
        from unittest.mock import patch
        from revenue import vic_rcti
        from revenue.institution_commission_draft import NSW_CLAIMS_ONLY
        from revenue.institution_commission_draft_views import _language_estimate_panel,render_workflow
        row={'component_uid':'test-language','student_id':'SYNTHETIC','student_name':'Synthetic',
             'course_kind':'language','school_name':'TEST language school','course':'Intensive English',
             'gross_cents':90000,'net_cents':90000,'gst_cents':0,'needs_evidence':True}
        terms={'revision':'test-terms','course_rules':{'language':{'conditions':'TEST language attendance'}}}
        html=_language_estimate_panel([row],terms,'/test',lambda _: '')
        self.assertIn('AUD 900.00',html)
        self.assertIn('name="tier" value="standard"',html)
        self.assertNotIn('name="iec"',html)
        self.assertNotIn('name="visa"',html)
        for mode in (NSW_CLAIMS_ONLY,vic_rcti.PAYMENT):
            with patch.object(self.fixture.workflow,'candidates',return_value=iter([row])):
                rendered=render_workflow(self.fixture.workflow,'csrf','token',
                    terms={**self.fixture.terms,**terms,'payment_mode':mode})
            self.assertIn('AUD 900.00',rendered)
            self.assertIn('独立语言课程佣金',rendered)

    def test_main_only_has_one_course_and_rejects_other_school(self):
        self.save([self.entry('main',1200000)])
        self.db.execute("INSERT INTO coe_oshc(id,application_cycle_id,student_id) VALUES(992,125,'TEST-PENINSULA-SCHEDULE')")
        self.db.execute("UPDATE coe_oshc SET coe_provider=(SELECT school_name FROM schools WHERE id=16) WHERE id=992")
        with self.assertRaises(ValueError):
            self.save([self.entry('main',1100000,13)],coe=992)
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM revenue_school_courses').fetchone()[0])

    def test_wrong_cycle_student_rolls_back_entire_batch(self):
        with self.assertRaises(ValueError):
            self.save([self.entry('language',600000)],student='TEST-VIC-PRIVATE')
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM revenue_school_courses').fetchone()[0])
        with self.assertRaises(ValueError):
            self.save([self.entry('language',600000),self.entry('main',1200000,13)])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM revenue_school_courses').fetchone()[0])

    def test_form_language_only_and_both_require_only_their_own_amounts(self):
        form={'course_mode_0':'language','course_language_school_0':'13',
              'course_language_name_0':'ELICOS','course_language_aud_0':'6000','course_language_offer_0':'24'}
        rows=read_course_fields(form,0)
        self.assertEqual(1,len(rows))
        self.assertEqual('',rows[0]['grade'])
        self.assertEqual(600000,rows[0]['cents'])
        with self.assertRaises(ValueError):
            read_course_fields({**form,'course_mode_0':'both'},0)

    def test_invalid_offer_status_never_revives_from_stale_active_final(self):
        from revenue.school_courses import accepted_course_offers
        for status in ('void','voided','canceled','declined','invalid_record',' VOIDED '):
            self.db.execute('UPDATE offers SET offer_status=? WHERE id=22',(status,))
            self.assertNotIn(22,[r['id'] for r in accepted_course_offers(self.db,'TEST-PENINSULA-SCHEDULE',125)],status)
        self.db.execute("UPDATE offers SET offer_status='accepted' WHERE id=22")
        self.assertIn(22,[r['id'] for r in accepted_course_offers(self.db,'TEST-PENINSULA-SCHEDULE',125)])

    def test_language_commission_requires_own_confirmed_rule_and_commencement(self):
        from revenue.course_commissions import confirm_language_rule
        from revenue.services import RevenueService
        result=self.save([self.entry('language',600000),self.entry('main',1200000)])
        component=result['courses'][0]['component_uid']
        missing=next(r for r in self.fixture.workflow.candidates(self.fixture.terms) if r['component_uid']==component)
        self.assertIn('语言佣金条款',missing['reason'])
        rules=confirm_language_rule(self.fixture.workflow,{'revision':self.fixture.terms['revision'],
            'rate':'15','period':'whole_course','conditions':'TEST language attendance verified',
            'reference':'TEST contract language clause 2','reviewed':'1'},'language-rule-test')
        workflow=self.fixture.workflow
        workflow.confirm_student_evidence({'component':component,'tier':'standard','reference':'TEST attendance','eligible':'1'},rules,'language-evidence')
        missing=next(r for r in workflow.candidates(rules) if r['component_uid']==component)
        self.assertIn('开学',missing['reason'])
        RevenueService(workflow.repo).confirm_commencement(component,'2026-09-01',1,'language-start')
        candidate=next(r for r in workflow.candidates(rules) if r['component_uid']==component)
        self.assertEqual(90000,candidate['gross_cents'])
        self.assertEqual(1500,candidate['rate_basis_points'])
        main=next(r for r in workflow.candidates(rules) if r['component_uid']=='state-component-125')
        self.assertEqual(120000,main['gross_cents'])
        from revenue.course_commissions import language_candidates
        legacy_facts=workflow._facts(rules)
        legacy_facts['state-component-125']={**legacy_facts['state-component-125'],'iec_tuition_cents':600000}
        duplicate=next(language_candidates(workflow,rules,legacy_facts))
        self.assertNotIn('gross_cents',duplicate)
        self.assertIn('重复计佣',duplicate['reason'])
        self.db.execute("UPDATE offers SET offer_status='withdrawn' WHERE id=22")
        blocked=next(r for r in workflow.candidates(rules) if r['component_uid']==component)
        self.assertNotIn('gross_cents',blocked)
        self.db.execute("UPDATE offers SET offer_status='accepted' WHERE id=22")
        resumed=next(r for r in workflow.candidates(rules) if r['component_uid']==component)
        self.assertEqual(90000,resumed['gross_cents'])
        self.db.execute("UPDATE offers SET school_id=13 WHERE id=22")
        self.db.execute("UPDATE offer_received_events SET school_id=13 WHERE legacy_offer_id=22")
        moved=next(r for r in workflow.candidates(rules) if r['component_uid']==component)
        self.assertNotIn('gross_cents',moved)
        self.db.execute("UPDATE offers SET school_id=16 WHERE id=22")
        self.db.execute("UPDATE offer_received_events SET school_id=16 WHERE legacy_offer_id=22")
        self.db.execute('CREATE TABLE offer_received_event_state_events(id INTEGER PRIMARY KEY,offer_received_event_id INTEGER,state TEXT,occurred_at TEXT)')
        self.db.execute("INSERT INTO offer_received_event_state_events VALUES(1,22,'WITHDRAWN','2026-11-20')")
        withdrawn=next(r for r in workflow.candidates(rules) if r['component_uid']==component)
        self.assertNotIn('gross_cents',withdrawn)
        self.db.execute("UPDATE offer_received_event_state_events SET state='ACTIVE'")
        active=next(r for r in workflow.candidates(rules) if r['component_uid']==component)
        self.assertEqual(90000,active['gross_cents'])
        self.db.execute("UPDATE coe_oshc SET student_id='TEST-VIC-PRIVATE' WHERE id=991")
        stale=next(r for r in workflow.candidates(rules) if r['component_uid']==component)
        self.assertNotIn('gross_cents',stale)

    def test_course_amount_correction_preserves_evidence_and_shared_versions(self):
        from business_changes.schema import install
        from business_changes.contracts import Subject
        from business_changes.versions import version_of
        from revenue.school_courses import correct_course_tuition
        self.save([self.entry('language',600000)])
        old=course_details(self.db,'TEST-PENINSULA-SCHEDULE')[0]
        self.db.commit()
        self.db.execute('BEGIN')
        install(self.db)
        self.db.execute('CREATE TABLE IF NOT EXISTS system_settings(key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute("INSERT OR REPLACE INTO system_settings VALUES('business_change_sync_v1','enabled')")
        self.db.commit()
        form={'tuition_aud':'6500','revision':old['revision'],'idempotency_key':'course-correction-test'}
        result=correct_course_tuition(self.db,'TEST-PENINSULA-SCHEDULE',old['uid'],form,1)
        self.assertEqual(result,correct_course_tuition(self.db,'TEST-PENINSULA-SCHEDULE',old['uid'],form,1))
        new=course_details(self.db,'TEST-PENINSULA-SCHEDULE')[0]
        self.assertEqual(650000,new['tuition_cents'])
        self.assertEqual(old['evidence_document_id'],new['evidence_document_id'])
        for scope in (Subject('student','TEST-PENINSULA-SCHEDULE'),Subject('application_cycle','125'),
                      Subject('dashboard','workflow'),Subject('dashboard','overview'),Subject('module','students')):
            self.assertEqual(1,version_of(self.db,scope),str(scope))
        with self.assertRaises(ValueError):
            correct_course_tuition(self.db,'TEST-PENINSULA-SCHEDULE',old['uid'],{**form,'idempotency_key':'stale'},1)
        from unittest.mock import patch
        before=self.db.execute('SELECT count(*) FROM revenue_audit_events').fetchone()[0]
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('test-domain-publish-failure')):
            with self.assertRaises(RuntimeError):
                correct_course_tuition(self.db,'TEST-PENINSULA-SCHEDULE',old['uid'],
                    {'tuition_aud':'6600','revision':new['revision'],'idempotency_key':'publish-fails'},1)
        self.assertEqual(650000,course_details(self.db,'TEST-PENINSULA-SCHEDULE')[0]['tuition_cents'])
        self.assertEqual(before,self.db.execute('SELECT count(*) FROM revenue_audit_events').fetchone()[0])

    def test_main_course_correction_retains_intake_metadata_and_source(self):
        from revenue.school_courses import correct_course_tuition
        from revenue.student_intake_tuition import student_tuition_details,source_record
        self.save([self.entry('main',1200000)])
        old=course_details(self.db,'TEST-PENINSULA-SCHEDULE')[0]
        source=source_record(self.db,'coe',991,'TEST-PENINSULA-SCHEDULE')
        correct_course_tuition(self.db,'TEST-PENINSULA-SCHEDULE',old['uid'],
            {'tuition_aud':'12500','revision':old['revision'],'idempotency_key':'main-course-correction'},1)
        current=student_tuition_details(self.db,'TEST-PENINSULA-SCHEDULE')['record']
        self.assertEqual((2028,'Year 10'),(current['intake_year'],current['intake_grade']))
        self.assertEqual(1250000,current['tuition_cents'])
        self.assertEqual(125000,self.fixture.candidate(125)['gross_cents'])
        self.assertEqual(source,source_record(self.db,'coe',991,'TEST-PENINSULA-SCHEDULE'))


if __name__=='__main__':
    unittest.main()
