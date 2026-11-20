import sqlite3
import unittest


class ExistingVisaReviewContextTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.addCleanup(self.conn.close)
        self.conn.executescript("""CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,cycle_type TEXT,status TEXT);
            INSERT INTO application_cycles VALUES(7,'S1','transfer','active'),(8,'S2','progression','active');""")

    def freeze(self,**fields):
        from business_changes.visa_review_context import freeze_context
        return freeze_context(self.conn,'S1',dict(visa_record_purpose='existing_evidence',**fields))

    def test_creation_owns_exact_cycle_and_does_not_trust_forged_frozen_fields(self):
        result=self.freeze(business_existing_visa_cycle_id='8')
        self.assertEqual('7',result['business_existing_visa_cycle_id'])
        with self.assertRaises(ValueError):self.freeze(existing_visa_cycle_id='8')
        with self.assertRaises(ValueError):self.freeze(visa_case_id='99')

    def test_ambiguous_cycles_require_explicit_selection(self):
        self.conn.execute("INSERT INTO application_cycles VALUES(9,'S1','progression','active')")
        with self.assertRaises(ValueError):self.freeze()
        self.assertEqual('9',self.freeze(existing_visa_cycle_id='9')['business_existing_visa_cycle_id'])

    def test_confirmation_rejects_missing_closed_reassigned_or_changed_kind(self):
        from business_changes.visa_review_context import validate_context
        context=self.freeze()
        self.assertEqual(7,validate_context(self.conn,'S1',context)['id'])
        with self.assertRaises(ValueError):validate_context(self.conn,'S1',{'visa_record_purpose':'existing_evidence'})
        for field,value in (('status','closed'),('student_id','S2'),('cycle_type','initial')):
            with self.subTest(field=field):
                self.conn.execute('SAVEPOINT fixture')
                self.conn.execute('UPDATE application_cycles SET '+field+'=? WHERE id=7',(value,))
                with self.assertRaises(ValueError):validate_context(self.conn,'S1',context)
                self.conn.execute('ROLLBACK TO fixture');self.conn.execute('RELEASE fixture')

    def test_regular_application_cannot_keep_forged_existing_evidence_binding(self):
        from business_changes.visa_review_context import freeze_context
        result=freeze_context(self.conn,'S1',{'visa_record_purpose':'current_application','business_existing_visa_cycle_id':'7'})
        self.assertNotIn('business_existing_visa_cycle_id',result)
