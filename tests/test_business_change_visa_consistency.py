import sqlite3
import unittest

from visa_business_state import retained_grant_needs_review
from business_change_sync import BusinessChangeSyncMixin


class ClosedGrantReviewTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.addCleanup(self.conn.close)
        self.conn.executescript('''CREATE TABLE application_cycles(id INTEGER,student_id TEXT,status TEXT,outcome TEXT);
            INSERT INTO application_cycles VALUES(7,'S1','closed','granted');
            CREATE TABLE visa_case_confirmed_decisions(visa_case_id INTEGER,decision_type TEXT,canonical_event_id INTEGER);
            INSERT INTO visa_case_confirmed_decisions VALUES(1,'grant',10),(2,'grant',NULL);''')
        self.case={'id':1,'student_id':'S1','application_cycle_id':7,'visa_type':'500 Student Visa'}

    def test_review_is_readonly_exact_student_cycle_and_canonical_student_decision(self):
        self.conn.execute('PRAGMA query_only=ON')
        self.assertTrue(retained_grant_needs_review(self.conn,self.case,'Pending'))
        for change in ({'student_id':'S2'},{'application_cycle_id':8},{'id':2},{'visa_type':'590 Guardian Visa'}):
            self.assertFalse(retained_grant_needs_review(self.conn,dict(self.case,**change),'Pending'))
        for result in ('Granted','Refused','Withdrawn'):
            self.assertFalse(retained_grant_needs_review(self.conn,self.case,result))
        self.assertEqual(('closed','granted'),self.conn.execute('SELECT status,outcome FROM application_cycles').fetchone())

    def test_no_visa_or_student_rights_reveals_no_issue_or_source_reads(self):
        class Handler(BusinessChangeSyncMixin):
            user={'id':5}
            def business_changes_enabled(self):return True
            def business_change_runtime(self):return self.runtime
        handler=Handler()
        for visa,student in ((False,True),(True,False),(False,False)):
            handler.runtime={'can_access_module':lambda user,module:visa,'allowed':lambda user,permission:student}
            self.assertEqual((),handler.business_change_visa_consistency_issues())
            self.assertEqual('',handler.business_change_unassigned_visa_html(student_id='S1'))

    def test_legacy_granted_label_without_current_evidence_requires_review_not_silent_regression(self):
        self.conn.execute("UPDATE application_cycles SET status='active',outcome=NULL WHERE id=7")
        legacy=dict(self.case,id=2,status='Granted',result_status='Granted')
        self.assertTrue(retained_grant_needs_review(self.conn,legacy,'Pending'))
        self.assertFalse(retained_grant_needs_review(self.conn,dict(legacy,student_id='S2'),'Pending'))
        self.assertFalse(retained_grant_needs_review(self.conn,dict(legacy,visa_type='590 Guardian Visa'),'Pending'))

    def test_prior_explicit_manual_grant_is_visible_as_review_when_no_current_letter_supports_it(self):
        self.conn.executescript('''CREATE TABLE visa_result_status_current(visa_case_id INTEGER,effective_result TEXT,
            authority TEXT,manual_override INTEGER);INSERT INTO visa_result_status_current VALUES(2,'Granted','manual',1);''')
        self.assertTrue(retained_grant_needs_review(self.conn,dict(self.case,id=2),'Pending'))
        for result in ('Granted','Refused','Withdrawn'):
            self.assertFalse(retained_grant_needs_review(self.conn,dict(self.case,id=2),result))
