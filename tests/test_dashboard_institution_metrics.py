import sqlite3
import unittest

from dashboard_institution_metrics import load_dashboard_institution_metrics


class DashboardInstitutionMetricsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE students(
              student_id TEXT PRIMARY KEY,
              passport_first_name TEXT,
              passport_last_name TEXT
            );
            CREATE TABLE school_cities(
              id INTEGER PRIMARY KEY,city_name TEXT,state TEXT,is_active INTEGER
            );
            CREATE TABLE schools(
              id INTEGER PRIMARY KEY,school_name TEXT,city_id INTEGER,
              sector_key TEXT,school_type TEXT,state TEXT
            );
            CREATE TABLE offer_received_events(
              id INTEGER PRIMARY KEY,application_cycle_id INTEGER,student_id TEXT,
              source_kind TEXT,school_id INTEGER,school_name_snapshot TEXT,
              higher_education_offer_id INTEGER
            );
            CREATE TABLE cycle_final_acceptances(
              id INTEGER PRIMARY KEY,application_cycle_id INTEGER,student_id TEXT,
              offer_received_event_id INTEGER,school_id INTEGER,
              school_name_snapshot TEXT,selected_at TEXT,
              acceptance_status TEXT
            );
            CREATE TABLE higher_education_providers(
              id INTEGER PRIMARY KEY,provider_name TEXT,provider_type TEXT,
              city TEXT,state TEXT,active_status TEXT
            );
            CREATE TABLE higher_education_applications(
              id INTEGER PRIMARY KEY,provider_id INTEGER,application_cycle_id INTEGER
            );
            CREATE TABLE higher_education_offers(
              id INTEGER PRIMARY KEY,application_id INTEGER,application_cycle_id INTEGER
            );

            INSERT INTO students VALUES('S1','One','Student');
            INSERT INTO students VALUES('S2','Two','Student');
            INSERT INTO students VALUES('S3','Three','Student');
            INSERT INTO school_cities VALUES(1,'Melbourne','VIC',1);
            INSERT INTO school_cities VALUES(2,'Sydney','NSW',1);
            INSERT INTO schools VALUES(10,'Master School',1,'private','K12','VIC');
            INSERT INTO schools VALUES(20,'New School',2,'public','K12','NSW');
            INSERT INTO higher_education_providers
              VALUES(30,'Master University','University','Brisbane','QLD','Active');
            INSERT INTO higher_education_applications VALUES(40,30,2);
            INSERT INTO higher_education_offers VALUES(50,40,2);

            INSERT INTO offer_received_events VALUES
              (100,1,'S1','private',10,'Historical School Name',NULL),
              (200,2,'S2','higher_education',NULL,'Old University Name',50),
              (300,3,'S3','private',NULL,'Legacy College',NULL),
              (400,4,'S1','public',20,'New School',NULL);
            INSERT INTO cycle_final_acceptances VALUES
              (1000,1,'S1',100,10,'Historical School Name','2026-08-01','active'),
              (2000,2,'S2',200,NULL,'Old University Name','2026-08-02','active'),
              (3000,3,'S3',300,NULL,'Legacy College','2026-08-03','active');
            """
        )
        self.fallback = {
            "legacy college": {
                "source_name": "Legacy College",
                "institution_type": "private_language_school",
                "metropolitan_area": "Adelaide",
                "match_method": "reviewed_manual_mapping",
                "reviewed": True,
            }
        }

    def tearDown(self):
        self.conn.close()

    def load(self):
        return load_dashboard_institution_metrics(
            self.conn, fallback_mappings=self.fallback
        )

    def test_master_data_wins_over_snapshot_and_reviewed_json(self):
        data = self.load()

        self.assertEqual(3, data["total_events"])
        self.assertEqual(1, data["category_counts"]["k12_private"])
        self.assertEqual(1, data["category_counts"]["university"])
        self.assertEqual(1, data["category_counts"]["private_language_school"])
        self.assertEqual(1, data["metro_counts"]["Melbourne"])
        self.assertEqual(1, data["metro_counts"]["Brisbane"])
        self.assertEqual(1, data["metro_counts"]["Adelaide"])
        self.assertEqual(2, data["provenance_counts"]["canonical_master"])
        self.assertEqual(1, data["provenance_counts"]["reviewed_json_fallback"])
        self.assertEqual(1, data["fallback_usage_count"])

    def test_master_data_changes_recalculate_without_json_edits(self):
        before = self.load()
        self.conn.execute(
            "UPDATE schools SET sector_key='public',city_id=2 WHERE id=10"
        )
        after = self.load()

        self.assertEqual(1, before["category_counts"]["k12_private"])
        self.assertEqual(1, after["category_counts"]["k12_public"])
        self.assertEqual(0, after["category_counts"]["k12_private"])
        self.assertEqual(1, after["metro_counts"]["Sydney"])
        self.assertEqual(0, after["metro_counts"]["Melbourne"])

    def test_withdrawal_removes_old_structure_and_change_adds_new_master(self):
        self.conn.execute(
            """UPDATE cycle_final_acceptances
               SET acceptance_status='superseded' WHERE id=1000"""
        )
        self.conn.execute(
            """INSERT INTO cycle_final_acceptances VALUES(
                 4000,1,'S1',400,20,'New School','2026-08-10','active'
               )"""
        )

        data = self.load()

        event = next(item for item in data["events"] if item["student_id"] == "S1")
        self.assertEqual("New School", event["school_name"])
        self.assertEqual("k12_public", event["institution_type"])
        self.assertEqual("Sydney", event["metropolitan_area"])
        self.assertEqual(3, data["total_events"])

    def test_unresolved_fallback_is_auditable_not_silent(self):
        data = self.load()

        fallback_event = data["fallback_events"][0]
        self.assertEqual("S3", fallback_event["student_id"])
        self.assertEqual("reviewed_json_fallback", fallback_event["provenance"])
        self.assertEqual("reviewed_manual_mapping", fallback_event["match_method"])


if __name__ == "__main__":
    unittest.main()
