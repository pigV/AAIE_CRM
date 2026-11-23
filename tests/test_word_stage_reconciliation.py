import sqlite3,unittest
import reviewed_application_stage as stages

class ReviewedStageTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.addCleanup(self.c.close)
        self.c.executescript('''CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,status TEXT,education_stream TEXT);
          INSERT INTO application_cycles VALUES(7,'S1','active','higher_education');
          INSERT INTO application_cycles VALUES(8,'S1','active','school_education');
          CREATE TABLE offers(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER,offer_status TEXT);
          INSERT INTO offers VALUES(1,'S1',7,'received');
          CREATE TABLE users(id INTEGER PRIMARY KEY,is_active INTEGER);INSERT INTO users VALUES(1,1);
          CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id INTEGER,action_type TEXT,module_name TEXT,module_key TEXT,
            record_id TEXT,student_id TEXT,old_value TEXT,new_value TEXT,created_at TEXT,status TEXT DEFAULT 'success');''')
        self.c.execute('BEGIN')
    def confirm(self,cycle=7):
        stages.confirm_reviewed_stage(self.c,student_id='S1',cycle_id=cycle,stage_key='08_offer_acceptance',
            actor_id=1,reason='Word feedback confirmed by owner',stamp='2026-11-23T22:00:00+11:00')
    def test_review_is_exact_cycle_and_creates_no_offer_or_financial_fact(self):
        self.confirm()
        result=stages.load_reviewed_stages(self.c,[7,8])
        self.assertEqual({7:('08_offer_acceptance','人工核对：Word feedback confirmed by owner')},result)
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM offers').fetchone()[0])
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
    def test_new_business_evidence_invalidates_review_instead_of_freezing_stage(self):
        self.confirm();self.c.execute("UPDATE offers SET offer_status='accepted' WHERE id=1")
        self.assertEqual({},stages.load_reviewed_stages(self.c,[7]))
    def test_other_cycle_change_does_not_invalidate_current_review(self):
        self.confirm();self.c.execute("INSERT INTO offers VALUES(2,'S1',8,'received')")
        self.assertIn(7,stages.load_reviewed_stages(self.c,[7]))
    def test_foreign_cycle_and_unreasoned_review_are_rejected(self):
        self.c.execute("UPDATE application_cycles SET student_id='S2' WHERE id=8")
        with self.assertRaises(ValueError):self.confirm(8)
        with self.assertRaises(ValueError):
            stages.confirm_reviewed_stage(self.c,student_id='S1',cycle_id=7,stage_key='10_visa_submission',actor_id=1,reason='',stamp='2026-11-23T22:00:00+11:00')
    def test_indirect_formal_offer_state_change_invalidates_review(self):
        self.c.execute('CREATE TABLE offer_received_events(id INTEGER PRIMARY KEY,application_cycle_id INTEGER)')
        self.c.execute('INSERT INTO offer_received_events VALUES(11,7)')
        self.c.execute('CREATE TABLE offer_received_event_state_events(id INTEGER PRIMARY KEY,offer_received_event_id INTEGER,state TEXT)')
        self.c.execute("INSERT INTO offer_received_event_state_events VALUES(1,11,'ACTIVE')")
        self.confirm();self.c.execute("UPDATE offer_received_event_state_events SET state='WITHDRAWN'")
        self.assertEqual({},stages.load_reviewed_stages(self.c,[7]))
    def test_indirect_public_school_preference_change_invalidates_review(self):
        self.c.execute('CREATE TABLE public_application_groups(id INTEGER PRIMARY KEY,application_cycle_id INTEGER)')
        self.c.execute('INSERT INTO public_application_groups VALUES(1,7)')
        self.c.execute('CREATE TABLE public_application_preferences(id INTEGER PRIMARY KEY,public_application_group_id INTEGER,application_status TEXT)')
        self.c.execute("INSERT INTO public_application_preferences VALUES(1,1,'submitted')")
        self.confirm();self.c.execute("UPDATE public_application_preferences SET application_status='rejected'")
        self.assertEqual({},stages.load_reviewed_stages(self.c,[7]))
