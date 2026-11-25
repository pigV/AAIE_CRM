import sqlite3,unittest
from tests import test_revenue_school_courses as fixture
import revenue.school_courses as courses
import offer_acceptance

class ReviewRegressionTests(unittest.TestCase):
    def test_canonical_only_withdrawal_preserves_coe_and_payment_history(self):
        from tests import test_business_change_school_chain as chain
        f=chain.SchoolMaterialChainTests();f.setUp();self.addCleanup(f.doCleanups)
        f.test_plan_application_offer_payment_coe_and_evidence_withdrawal_use_current_cycle()
        actor={**f.actor,'can_manage_offer':True}
        with f.db.transaction() as c:
            row=c.execute("SELECT e.legacy_offer_id FROM cycle_final_acceptances a JOIN offer_received_events e ON e.id=a.offer_received_event_id WHERE a.student_id=? AND a.acceptance_status='active'",(f.sid,)).fetchone()
            oid=row[0]
            self.assertFalse(c.execute('SELECT 1 FROM student_offer_acceptances WHERE student_id=?',(f.sid,)).fetchone())
            # Document acceptance is independent from the canonical final choice.
            offer_acceptance.accept_offer_document(c,f.sid,oid,actor)
            originals=[tuple(x) for x in c.execute('SELECT * FROM coe_oshc')]
            payments=[tuple(x) for x in c.execute('SELECT * FROM payments')]
            with self.assertRaises(ValueError):
                offer_acceptance.withdraw_offer_acceptance_and_selection(c,f.sid,oid,actor,reason='Synthetic test')
            offer_acceptance.withdraw_offer_acceptance_and_selection(c,f.sid,oid,actor,reason='Synthetic test',acknowledge_final=True)
            self.assertEqual('withdrawn',c.execute('SELECT acceptance_status FROM cycle_final_acceptances WHERE student_id=?',(f.sid,)).fetchone()[0])
            self.assertEqual(originals,[tuple(x) for x in c.execute('SELECT * FROM coe_oshc')])
            self.assertEqual(payments,[tuple(x) for x in c.execute('SELECT * FROM payments')])
        self.assertNotEqual('10_visa_submission',f.stage())
        with f.db.transaction() as c:
            history=c.execute('SELECT count(*) FROM offer_acceptance_history').fetchone()[0]
            offer_acceptance.withdraw_offer_acceptance_and_selection(c,f.sid,oid,actor,reason='Synthetic repeated request',acknowledge_final=True)
            self.assertEqual(history,c.execute('SELECT count(*) FROM offer_acceptance_history').fetchone()[0])
            offer_acceptance.accept_offer_document(c,f.sid,oid,actor)
            self.assertEqual('accepted',c.execute('SELECT offer_status FROM offers WHERE id=?',(oid,)).fetchone()[0])
            self.assertFalse(c.execute("SELECT 1 FROM cycle_final_acceptances WHERE student_id=? AND acceptance_status='active'",(f.sid,)).fetchone())
            self.assertEqual(payments,[tuple(x) for x in c.execute('SELECT * FROM payments')])

    def test_canonical_only_selected_offer_is_exposed_for_explicit_withdrawal(self):
        c=sqlite3.connect(':memory:');self.addCleanup(c.close)
        c.executescript("""CREATE TABLE cycle_final_acceptances(application_cycle_id,student_id,offer_received_event_id,acceptance_status);
          CREATE TABLE offer_received_events(id,legacy_offer_id,student_id,application_cycle_id);
          INSERT INTO offer_received_events VALUES(1,25,'S1',10),(2,26,'S1',11),(3,27,'S2',12);
          INSERT INTO cycle_final_acceptances VALUES(10,'S1',1,'active'),(11,'S1',2,'withdrawn'),(12,'S2',3,'active');""")
        fn=getattr(offer_acceptance,'active_final_offer_ids',None)
        self.assertTrue(callable(fn),'Canonical-only current choices must supply the withdrawal acknowledgment')
        self.assertEqual({25},fn(c,'S1'))

    def test_simplified_new_version_preserves_existing_named_course_identity(self):
        f=fixture.SchoolCourseTests();f.setUp();self.addCleanup(f.doCleanups);c=f.db
        old=f.save([f.entry('language',600000)])
        c.execute("INSERT INTO coe_oshc(id,application_cycle_id,student_id,coe_provider,coe_course) SELECT 992,125,'TEST-PENINSULA-SCHEDULE',school_name,'English language course' FROM schools WHERE id=16")
        form={'course_mode_0':'language','course_language_school_0':'16','course_language_aud_0':'6000',
              'course_language_offer_0':'22','course_language_confirmed_0':'1'}
        entries=courses.read_course_fields(form,0)
        after=f.save(entries,coe=992)
        self.assertEqual(old['courses'][0]['uid'],after['courses'][0]['uid'],'New UI must not duplicate a named legacy course')
        self.assertEqual(1,c.execute('SELECT count(*) FROM revenue_school_courses').fetchone()[0])
        self.assertEqual('language course',after['courses'][0]['course_name'])
