from pathlib import Path
import sqlite3,unittest
from tests import test_revenue_school_courses as fixture
import revenue.school_courses as service

MIGRATION=Path(__file__).resolve().parents[1]/'migrations/20261125_055_public_language_authority.sql'

class SimplifiedCourseFieldsTests(unittest.TestCase):
    def public(self):
        return dict(course_mode_0='language',course_language_sector_0='public',course_language_start_date_0='2027-02-03',
            course_language_aud_0='9250',course_language_authority_state_0='VIC',course_language_student_number_0='2487510',
            course_language_offer_0='22',course_language_confirmed_0='1')
    def test_public_language_no_school_or_detail_input_is_required(self):
        try:row=service.read_course_fields(self.public(),0)[0]
        except ValueError as e:self.fail('Confirmed public-language form must save without campus/details: '+str(e))
        self.assertIsNone(row['school_id']);self.assertEqual('VIC',row['authority_state']);self.assertEqual(925000,row['cents'])
    def test_private_language_still_requires_actual_school(self):
        form=self.public();form['course_language_sector_0']='private'
        with self.assertRaises(ValueError):service.read_course_fields(form,0)
    def test_main_year_is_derived_from_start_and_grade_remains_required(self):
        form={k.replace('language','main'):v for k,v in self.public().items()};form['course_mode_0']='main';form['course_main_school_0']='16';form['course_main_grade_0']='Year 9'
        try:row=service.read_course_fields(form,0)[0]
        except ValueError as e:self.fail('Main year/details must not require manual input: '+str(e))
        self.assertEqual((2027,'Year 9'),(row['year'],row['grade']))
        form['course_main_grade_0']=''
        with self.assertRaises(ValueError):service.read_course_fields(form,0)

class PublicLanguageAuthorityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MIGRATION.is_file(),'Public language needs a constrained authority-only course identity')
        self.f=fixture.SchoolCourseTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.c=self.f.db
        self.c.commit();self.c.execute('PRAGMA foreign_keys=OFF');self.c.executescript(MIGRATION.read_text());self.c.execute('PRAGMA foreign_keys=ON')
        self.c.execute("UPDATE schools SET sector_key='public',state='VIC' WHERE id=16")
        self.c.execute("UPDATE coe_oshc SET coe_provider='Victorian Department of Education',coe_course='English language course'")
    def test_authority_course_saves_without_inventing_school_or_main_tuition(self):
        entry=service.read_course_fields(SimplifiedCourseFieldsTests().public(),0)[0]
        before=self.c.execute('SELECT count(*) FROM schools').fetchone()[0]
        result=self.f.save([entry]);row=service.course_details(self.c,'TEST-PENINSULA-SCHEDULE')[0]
        self.assertIsNone(row['school_id']);self.assertEqual('VIC',row['authority_state'])
        self.assertIn('VIC',row['school_name']);self.assertEqual(925000,row['tuition_cents'])
        self.assertEqual(before,self.c.execute('SELECT count(*) FROM schools').fetchone()[0])
        self.assertEqual(result,self.f.save([entry]))
        self.assertFalse(self.c.execute('PRAGMA foreign_key_check').fetchall())
        from types import SimpleNamespace
        from revenue.course_commissions import language_components
        workflow=SimpleNamespace(db=self.c,target=('authority','VIC'))
        self.assertIn(row['uid'],[r['course_uid'] for r in language_components(workflow)])
        workflow.target=('authority','NSW')
        self.assertNotIn(row['uid'],[r['course_uid'] for r in language_components(workflow)])
        workflow.target=('school',16)
        self.assertNotIn(row['uid'],[r['course_uid'] for r in language_components(workflow)])
    def test_database_rejects_missing_authority_and_main_without_school(self):
        for kind,state in [('language',None),('main','VIC')]:
            with self.assertRaises(sqlite3.IntegrityError):
                self.c.execute("INSERT INTO revenue_school_courses(uid,commercial_case_id,course_kind,school_id,course_name,authority_state,confirmed_by,confirmed_at) SELECT ?,id,?,NULL,'Unverified',?,1,'test' FROM revenue_commercial_cases LIMIT 1",('invalid-'+kind,kind,state))

class PublicLanguageMigrationPreservationTests(unittest.TestCase):
    def test_nonempty_legacy_course_evidence_and_financial_rows_survive_rebuild(self):
        self.assertTrue(MIGRATION.is_file())
        f=fixture.SchoolCourseTests();f.setUp();self.addCleanup(f.doCleanups);c=f.db
        f.save([f.entry('language',600000)]);c.commit()
        tables=('revenue_school_courses','revenue_school_course_evidence','revenue_tuition_facts','revenue_enrolment_components')
        cols={t:[r[1] for r in c.execute('PRAGMA table_info('+t+')')] for t in tables}
        before={t:[tuple(r) for r in c.execute('SELECT '+','.join(cols[t])+' FROM '+t+' ORDER BY id')] for t in tables}
        c.execute('PRAGMA foreign_keys=OFF');c.executescript(MIGRATION.read_text());c.execute('PRAGMA foreign_keys=ON')
        for t in tables:self.assertEqual(before[t],[tuple(r) for r in c.execute('SELECT '+','.join(cols[t])+' FROM '+t+' ORDER BY id')],t)
        self.assertEqual(1,c.execute('PRAGMA foreign_keys').fetchone()[0]);self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
