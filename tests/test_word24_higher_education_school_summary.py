"""A reclassified university cycle cannot revive its old K-12 school plan."""
import sqlite3
import unittest
from unittest.mock import patch

from student_detail_summary_cards import _load_school_plan_card_summary


class HigherEducationSchoolSummaryTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE application_cycles(id,student_id,status,sequence_no,education_stream);
            INSERT INTO application_cycles VALUES(80,'S','active',1,'higher_education');
            CREATE TABLE cycle_final_acceptances(id,application_cycle_id,school_name_snapshot,acceptance_status);
            CREATE TABLE school_plan_reports(id,application_cycle_id,cycle_is_current,plan_sector,city_id);
            CREATE INDEX idx_school_plan_reports_cycle ON school_plan_reports(application_cycle_id,plan_sector,city_id,cycle_is_current);
            INSERT INTO school_plan_reports VALUES(1,80,1,'public',1);
            CREATE TABLE school_plan_confirmed_schools(id,report_id,student_id,school_name_snapshot,
                submission_status,result_status,result_offer_id,is_selected);
            INSERT INTO school_plan_confirmed_schools VALUES(1,1,'S','Old K-12 Asquith School','submitted','pending',NULL,1);
            CREATE TABLE higher_education_providers(id,provider_name);
            INSERT INTO higher_education_providers VALUES(1,'University A'),(2,'University B');
            CREATE TABLE higher_education_applications(id,application_cycle_id,provider_id,status,is_current,submitted_at);
            CREATE TABLE higher_education_offers(id,application_cycle_id,application_id,status);
            CREATE TABLE offers(id,student_id,application_cycle_id,school_name,document_type,
                offer_status,offer_file_path,offer_expiry_date);
            CREATE TABLE student_documents(id,student_id,file_path,is_active,document_status,voided_at);
        ''')

    def summary(self):
        with patch('student_detail_summary_cards.is_workflow_v2_active', return_value=True):
            return _load_school_plan_card_summary(self.conn,'S')

    def test_no_current_university_evidence_does_not_show_old_school_or_submission_count(self):
        summary=self.summary()
        self.assertEqual((),summary.names)
        self.assertEqual(0,summary.submitted_count)
        self.assertEqual('暂无当前大学学校方案/申请',summary.empty_label)

    def test_current_owned_offer_school_replaces_old_k12_plan(self):
        self.conn.executescript('''
            INSERT INTO offers VALUES(1,'S',80,'York Business Institute','Offer','received','/synthetic/york.pdf','2099-01-01');
            INSERT INTO offers VALUES(2,'S',99,'Older University','Offer','received','/synthetic/old.pdf','2099-01-01');
            INSERT INTO student_documents VALUES(1,'S','/synthetic/york.pdf',1,'active',NULL);
        ''')
        summary=self.summary()
        self.assertEqual(('York Business Institute',),summary.names)
        self.assertEqual(('York Business Institute',),summary.offer_received_names)
        self.assertEqual(0,summary.submitted_count)

    def test_current_native_applications_have_their_own_submission_counts(self):
        self.conn.executescript('''
            INSERT INTO higher_education_applications VALUES(1,80,1,'Submitted',1,'2026-11-01');
            INSERT INTO higher_education_applications VALUES(2,80,2,'Draft',1,NULL);
        ''')
        summary=self.summary()
        self.assertEqual(('University A','University B'),summary.names)
        self.assertEqual(('University A',),summary.waiting_result_names)
        self.assertEqual(1,summary.submitted_count)

    def test_old_withdrawn_unreviewed_and_void_material_are_not_current_university_schools(self):
        self.conn.executescript('''
            INSERT INTO higher_education_applications VALUES(1,80,1,'Withdrawn',1,'2026-11-01');
            INSERT INTO higher_education_applications VALUES(2,80,2,'Submitted',0,'2026-11-01');
            INSERT INTO offers VALUES(1,'S',80,'Void University','Offer','received','/synthetic/void.pdf','2099-01-01');
            INSERT INTO offers VALUES(2,'S',NULL,'Pending University','Offer','received','/synthetic/pending.pdf','2099-01-01');
            INSERT INTO student_documents VALUES(1,'S','/synthetic/void.pdf',0,'voided','2026-11-01');
        ''')
        self.assertEqual((),self.summary().names)

    def test_legacy_offer_latest_receipt_withdrawal_and_supersession_are_not_revived(self):
        self.conn.executescript('''
            INSERT INTO offers VALUES(1,'S',80,'Withdrawn University','Offer','received','/synthetic/offer.pdf','2099-01-01');
            CREATE TABLE offer_received_events(id,legacy_offer_id,application_cycle_id);
            INSERT INTO offer_received_events VALUES(10,1,80);
            CREATE TABLE offer_received_event_state_events(id,offer_received_event_id,state,occurred_at);
            INSERT INTO offer_received_event_state_events VALUES(1,10,'ACTIVE','2026-11-01');
            INSERT INTO offer_received_event_state_events VALUES(2,10,'WITHDRAWN','2026-11-02');
        ''')
        self.assertEqual((),self.summary().names)
        self.conn.execute("UPDATE offer_received_event_state_events SET state='SUPERSEDED' WHERE id=2")
        self.assertEqual((),self.summary().names)
        self.conn.execute("INSERT INTO offer_received_event_state_events VALUES(3,10,'ACTIVE','2026-11-03')")
        self.assertEqual(('Withdrawn University',),self.summary().names)

    def test_expired_received_offer_is_excluded_but_signed_school_history_remains(self):
        self.conn.executescript('''
            INSERT INTO offers VALUES(1,'S',80,'Expired University','Offer','received','/synthetic/expired.pdf','1999-01-01');
            INSERT INTO offers VALUES(2,'S',80,'Historical Signed University','Offer','signed_received','/synthetic/signed.pdf','1999-01-01');
        ''')
        summary=self.summary()
        self.assertEqual(('Historical Signed University',),summary.names)
        self.assertEqual((),summary.offer_received_names)

    def test_native_offer_withdrawal_keeps_application_but_not_a_current_offer(self):
        self.conn.executescript('''
            INSERT INTO higher_education_applications VALUES(1,80,1,'Submitted',1,'2026-11-01');
            INSERT INTO higher_education_offers VALUES(2,80,1,'Received');
            CREATE TABLE offer_received_events(id,higher_education_offer_id,application_cycle_id);
            INSERT INTO offer_received_events VALUES(10,2,80);
            CREATE TABLE offer_received_event_state_events(id,offer_received_event_id,state,occurred_at);
            INSERT INTO offer_received_event_state_events VALUES(1,10,'WITHDRAWN','2026-11-01');
        ''')
        summary=self.summary()
        self.assertEqual(('University A',),summary.names)
        self.assertEqual((),summary.offer_received_names)
        self.assertEqual(('University A',),summary.waiting_result_names)
