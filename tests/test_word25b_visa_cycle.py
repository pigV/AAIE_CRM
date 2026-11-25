import json
import unittest
from tests import test_visa_documents_candidate as fixture


class LegacyVisaCycleConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.VisaDocumentsCandidateTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.c=self.f.conn;self.f.student_cycle()
        self.c.execute('UPDATE visa_cases SET application_cycle_id=NULL WHERE id=1')

    def test_unique_legacy_student_case_is_bound_atomically_and_grant_is_confirmed(self):
        extraction=self.f.extract()
        try:
            result=self.f.service.confirm_letter(self.c,1,extraction['id'],values=json.loads(extraction['proposed_json']),**self.f.actor)
        except (ValueError,RuntimeError) as e:self.fail('Unique current legacy case must confirm: '+str(e))
        self.assertEqual(1,self.c.execute('SELECT application_cycle_id FROM visa_cases WHERE id=1').fetchone()[0])
        self.assertEqual(('grant','2026-09-05'),tuple(self.c.execute('SELECT decision_type,decision_date FROM visa_decision_events').fetchone()))
        self.assertEqual('Granted',self.c.execute('SELECT result_status FROM visa_cases WHERE id=1').fetchone()[0])
        self.assertIsNotNone(result['canonical_event_id'])
        self.f.service.confirm_letter(self.c,1,extraction['id'],values=json.loads(extraction['proposed_json']),**self.f.actor)
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM visa_decision_events').fetchone()[0])

    def test_ambiguous_legacy_case_does_not_guess_or_save_partial_review(self):
        self.c.execute("INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status) VALUES(2,'C2','S1','initial',2,'active')")
        extraction=self.f.extract()
        with self.assertRaises(ValueError):self.f.service.confirm_letter(self.c,1,extraction['id'],values=json.loads(extraction['proposed_json']),**self.f.actor)
        self.assertIsNone(self.c.execute('SELECT application_cycle_id FROM visa_cases WHERE id=1').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM visa_letter_reviews').fetchone()[0])

    def test_failed_official_confirmation_rolls_back_new_cycle_binding(self):
        extraction=self.f.extract()
        from visa_result_status import record_manual_result
        record_manual_result(self.c,1,'Refused',reason='Verified conflicting refusal',**self.f.actor)
        with self.assertRaisesRegex(ValueError,'冲突|conflict'):
            self.f.service.confirm_letter(self.c,1,extraction['id'],values=json.loads(extraction['proposed_json']),**self.f.actor)
        self.assertIsNone(self.c.execute('SELECT application_cycle_id FROM visa_cases WHERE id=1').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM visa_letter_reviews').fetchone()[0])

    def test_guardian_confirmation_cannot_close_or_decide_student_cycle(self):
        self.c.execute('ALTER TABLE visa_cases ADD COLUMN case_type TEXT')
        self.c.execute("UPDATE visa_cases SET case_type='590 Guardian Visa' WHERE id=1")
        extraction=self.f.extract()
        self.f.service.confirm_letter(self.c,1,extraction['id'],values=json.loads(extraction['proposed_json']),**self.f.actor)
        self.assertIsNone(self.c.execute('SELECT application_cycle_id FROM visa_cases WHERE id=1').fetchone()[0])
        self.assertEqual('active',self.c.execute('SELECT status FROM application_cycles WHERE id=1').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM visa_decision_events').fetchone()[0])
