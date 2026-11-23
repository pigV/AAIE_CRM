import sqlite3,unittest
from dashboard_confirmed_stages import legacy_he_offer_cycles
class HigherEducationOfferValidityTests(unittest.TestCase):
    def test_formal_withdrawal_is_not_reactivated_by_unclassified_offer_fallback(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;self.addCleanup(c.close)
        c.executescript('''CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,education_stream TEXT);
          INSERT INTO application_cycles VALUES(7,'S1','higher_education');
          CREATE TABLE offers(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER,document_type TEXT,offer_status TEXT,offer_file_path TEXT,offer_expiry_date TEXT);
          INSERT INTO offers VALUES(11,'S1',7,'Offer','received','/source.pdf','2027-01-01');
          CREATE TABLE student_documents(student_id TEXT,file_path TEXT,is_active INTEGER,document_status TEXT);
          CREATE TABLE offer_received_events(id INTEGER PRIMARY KEY,legacy_offer_id INTEGER,application_cycle_id INTEGER);
          INSERT INTO offer_received_events VALUES(13,11,7);
          CREATE TABLE offer_received_event_state_events(id INTEGER PRIMARY KEY,offer_received_event_id INTEGER,state TEXT,occurred_at TEXT);
          INSERT INTO offer_received_event_state_events VALUES(1,13,'ACTIVE','2026-11-20');''')
        self.assertIn(7,legacy_he_offer_cycles(c,[7],'2026-11-23'))
        c.execute("INSERT INTO offer_received_event_state_events VALUES(2,13,'WITHDRAWN','2026-11-23')")
        self.assertEqual({},legacy_he_offer_cycles(c,[7],'2026-11-23'))
