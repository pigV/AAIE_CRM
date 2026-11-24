"""Verified independent tertiary evidence must survive unrelated K12 history."""
import sqlite3
import unittest
from unittest.mock import patch

from staging_environment import ACTIVE
from dashboard_confirmed_stages import load_dashboard_stages


class LegacyTertiaryWithdrawalDashboardTests(unittest.TestCase):
    def setUp(self):
        # Schema only, no real student rows or application secrets copied.
        source = sqlite3.connect('file:' + str(ACTIVE.db) + '?mode=ro', uri=True)
        ddl = [r[0] for r in source.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END,name")]
        source.close()
        self.c = sqlite3.connect(':memory:'); self.c.row_factory = sqlite3.Row
        self.addCleanup(self.c.close)
        self.c.executescript(';\n'.join(ddl) + ';')
        self.c.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-01-01')")
        self.c.execute("INSERT INTO students(student_id,passport_first_name,passport_last_name,current_status,created_date,updated_at,form_data_json) VALUES('SYNTHETIC','Synthetic','Tertiary','Active','2026-01-01','2026-01-01','{}')")
        self.c.execute("INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at,education_stream) VALUES(1,'synthetic','SYNTHETIC','initial',1,'active','2026-01-01','2026-01-01','2026-01-01','higher_education')")
        self.c.execute("INSERT INTO offers(id,student_id,school_name,offer_status,offer_file_path,signed_offer_file_path,document_type,created_at,application_cycle_id) VALUES(1,'SYNTHETIC','Independent Tertiary Provider','accepted','/synthetic/signed.pdf','/synthetic/signed.pdf','SignedOffer','2026-01-01',1)")
        self.c.execute("INSERT INTO coe_oshc(id,student_id,document_kind,file_path,created_at,application_cycle_id,coe_provider,coe_course,coe_start_date,coe_end_date) VALUES(1,'SYNTHETIC','COE','/synthetic/coe.pdf','2026-01-01',1,'Independent Tertiary Provider','Bachelor of Business','2027-01-01','2099-01-01')")
        for i, kind, path in [(1,'SignedOffer','/synthetic/signed.pdf'),(2,'COE','/synthetic/coe.pdf')]:
            self.c.execute("INSERT INTO student_documents(id,student_id,folder_category,document_type,original_name,stored_name,file_path,file_size,uploaded_at,document_date) VALUES(?,'SYNTHETIC','Offer',?,'synthetic.pdf','synthetic.pdf',?,1,'2026-11-24','2026-07-09')",(i,kind,path))
        self.c.execute("INSERT INTO public_application_groups(id,application_cycle_id,student_id,city_id,status,created_at,updated_at) VALUES(1,1,'SYNTHETIC',1,'draft','2026-01-01','2026-01-01')")
        self.c.execute("INSERT INTO offer_received_events(id,application_cycle_id,student_id,source_kind,public_application_group_id,school_name_snapshot,external_offer_key,idempotency_key,received_at,created_at) VALUES(1,1,'SYNTHETIC','public',1,'Old Secondary School','synthetic-old','synthetic-old','2026-01-01','2026-01-01')")
        self.c.execute("INSERT INTO cycle_final_acceptances(id,application_cycle_id,student_id,offer_received_event_id,school_name_snapshot,selected_at,created_at,updated_at,acceptance_status,ended_at) VALUES(1,1,'SYNTHETIC',1,'Old Secondary School','2026-01-01','2026-01-01','2026-01-01','withdrawn','2026-02-01')")

    def stage(self):
        with patch('reviewed_application_stage.load_reviewed_stages', return_value={}):
            return load_dashboard_stages(self.c, (1,))[1]

    def test_independent_signed_tertiary_offer_and_coe_pass_old_school_hold(self):
        result = self.stage()
        self.assertIsNotNone(result)
        self.assertEqual('10_visa_submission', result[0])

    def test_same_provider_withdrawal_keeps_hold(self):
        self.c.execute("UPDATE offers SET school_name='Old Secondary School'")
        self.c.execute("UPDATE coe_oshc SET coe_provider='Old Secondary School'")
        self.assertIsNone(self.stage())

    def test_active_invalid_canonical_selection_keeps_hold(self):
        self.c.execute("UPDATE cycle_final_acceptances SET acceptance_status='active',ended_at=NULL")
        self.c.execute("INSERT INTO offer_received_event_state_events(offer_received_event_id,state,reason,occurred_at,created_at,idempotency_key) VALUES(1,'WITHDRAWN','Synthetic invalid source','2026-11-01','2026-11-01','synthetic-invalid')")
        self.assertIsNone(self.stage())

    def test_unverified_signature_cannot_release_old_hold(self):
        self.c.execute("UPDATE student_documents SET is_active=0,document_status='voided' WHERE id=1")
        self.assertIsNone(self.stage())
