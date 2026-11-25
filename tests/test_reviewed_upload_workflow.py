import sqlite3
import unittest
from reviewed_upload_workflow import bind_reviewed_offer


class ReviewedOfferOwnerTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.addCleanup(self.c.close)
        self.c.executescript("""CREATE TABLE application_cycles(id INTEGER,student_id TEXT,status TEXT,sequence_no INTEGER,cycle_type TEXT);CREATE TABLE offers(id INTEGER,student_id TEXT,application_cycle_id INTEGER);INSERT INTO application_cycles VALUES(1,'S','active',1,'initial');INSERT INTO offers VALUES(10,'S',NULL);""")
    def owner(self):return self.c.execute('SELECT application_cycle_id FROM offers WHERE id=10').fetchone()[0]
    def test_bind_retry_and_existing_owner_are_idempotent(self):
        self.assertEqual(1,bind_reviewed_offer(self.c,'S',10,{}));self.assertEqual(1,self.owner())
        before=self.c.total_changes
        self.assertEqual(1,bind_reviewed_offer(self.c,'S',10,{}));self.assertEqual(before,self.c.total_changes)
    def test_multiple_current_cycles_require_explicit_selection(self):
        self.c.execute("INSERT INTO application_cycles VALUES(2,'S','active',2,'initial')")
        with self.assertRaisesRegex(ValueError,'多个'):bind_reviewed_offer(self.c,'S',10,{})
        self.assertIsNone(self.owner())
        self.assertEqual(2,bind_reviewed_offer(self.c,'S',10,{'application_cycle_id':'2'}))
    def test_explicit_foreign_or_closed_cycle_rejected(self):
        self.c.execute("INSERT INTO application_cycles VALUES(2,'OTHER','active',1,'initial')")
        with self.assertRaises(ValueError):bind_reviewed_offer(self.c,'S',10,{'application_cycle_id':'2'})
        self.c.execute("UPDATE application_cycles SET status='closed' WHERE id=1")
        with self.assertRaises(ValueError):bind_reviewed_offer(self.c,'S',10,{'application_cycle_id':'1'})
        self.assertIsNone(self.owner())
    def test_foreign_document_or_existing_cycle_ownership_cannot_be_reassigned(self):
        with self.assertRaises(ValueError):bind_reviewed_offer(self.c,'OTHER',10,{'application_cycle_id':'1'})
        self.c.execute('UPDATE offers SET application_cycle_id=99')
        with self.assertRaises(ValueError):bind_reviewed_offer(self.c,'S',10,{})
        self.assertEqual(99,self.owner())
    def test_no_active_cycle_does_not_create_one(self):
        self.c.execute("UPDATE application_cycles SET status='closed'")
        self.assertIsNone(bind_reviewed_offer(self.c,'S',10,{}));self.assertIsNone(self.owner())
    def test_progression_upload_context_keeps_its_native_routing(self):
        self.assertIsNone(bind_reviewed_offer(self.c,'S',10,{'progression_link_after_review':'1','progression_case_id':'8','progression_document_role':'offer','progression_target_application_id':'9'}))
        self.assertIsNone(self.owner())
    def test_non_initial_cycle_never_implicitly_repurposes_an_upload(self):
        self.c.execute("UPDATE application_cycles SET cycle_type='transfer'")
        with self.assertRaisesRegex(ValueError,'对应业务'):bind_reviewed_offer(self.c,'S',10,{})
        self.assertIsNone(self.owner())

    def test_offer_selector_exposes_all_owners_without_automatic_choice(self):
        from reviewed_upload_workflow import render_offer_cycle_selection
        self.c.execute("INSERT INTO application_cycles VALUES(2,'S','active',2,'initial')")
        html=render_offer_cycle_selection(self.c,'S')
        self.assertIn('name="offer_application_cycle_id"',html)
        self.assertIn('不会自动选择',html)
        self.assertNotIn(' selected',html)
        self.assertIn('value="2" selected',render_offer_cycle_selection(self.c,'S',selected='2'))

    def test_offer_selector_does_not_offer_progression_or_transfer_as_intake(self):
        from reviewed_upload_workflow import render_offer_cycle_selection
        self.c.execute("INSERT INTO application_cycles VALUES(2,'S','active',2,'progression')")
        html=render_offer_cycle_selection(self.c,'S')
        self.assertIn('value="1"',html)
        self.assertNotIn('value="2"',html)
        self.assertIn('required',html)
