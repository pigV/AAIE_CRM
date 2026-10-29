import sqlite3
import unittest

from pathlib import Path

from student_lifecycle.module_dispositions import record_module_disposition


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_030 = (
    ROOT / "migrations" / "20260824_030_lifecycle_module_dispositions.sql"
)
NOW = "2026-08-24T12:00:00+10:00"


class LifecycleModuleProgressTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE users(id INTEGER PRIMARY KEY);
            CREATE TABLE students(student_id TEXT PRIMARY KEY);
            CREATE TABLE student_cases(
              id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,case_type TEXT NOT NULL
            );
            CREATE TABLE contracts(
              id INTEGER PRIMARY KEY,student_id TEXT,
              contract_type TEXT,contract_status TEXT,
              deleted_at TEXT,voided_at TEXT
            );
            CREATE TABLE progression_cases(
              case_id INTEGER PRIMARY KEY,business_type TEXT,
              source_contract_id INTEGER
            );
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,
              cycle_type TEXT NOT NULL,source_student_case_id INTEGER,
              status TEXT NOT NULL
            );
            CREATE TABLE coe_oshc(
              id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,
              document_kind TEXT NOT NULL,application_cycle_id INTEGER
            );
            CREATE TABLE visas(
              id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,
              application_cycle_id INTEGER
            );
            CREATE TABLE progression_target_applications(
              id INTEGER PRIMARY KEY,case_id INTEGER NOT NULL,
              application_status TEXT NOT NULL,is_final INTEGER NOT NULL
            );
            CREATE TABLE progression_case_offers(
              id INTEGER PRIMARY KEY,case_id INTEGER NOT NULL,
              target_application_id INTEGER,offer_status TEXT NOT NULL
            );
            CREATE TABLE progression_document_links(
              id INTEGER PRIMARY KEY,case_id INTEGER NOT NULL,
              target_application_id INTEGER,document_role TEXT NOT NULL,
              status TEXT NOT NULL,is_current INTEGER NOT NULL
            );
            CREATE TABLE progression_case_arrangements(
              id INTEGER PRIMARY KEY,case_id INTEGER NOT NULL,
              arrangement_type TEXT NOT NULL,status TEXT NOT NULL
            );

            INSERT INTO users VALUES(1);
            INSERT INTO students VALUES('student-1');
            INSERT INTO student_cases VALUES(80,'student-1','progression');
            INSERT INTO application_cycles VALUES
              (1,'student-1','initial',NULL,'closed'),
              (8,'student-1','progression',80,'active');
            INSERT INTO coe_oshc VALUES
              (11,'student-1','COE',1),
              (12,'student-1','OSHC',1);
            INSERT INTO visas VALUES(13,'student-1',1);
            INSERT INTO progression_target_applications VALUES
              (801,80,'submitted',1);
            INSERT INTO progression_document_links VALUES
              (901,80,801,'coe','confirmed',1);
            """
        )
        self.connection.executescript(
            MIGRATION_030.read_text(encoding="utf-8")
        )

    def tearDown(self):
        self.connection.close()

    def disposition(self, module_key, disposition, **overrides):
        values = {
            "event_uid": f"cycle-8-{module_key}-{disposition}",
            "application_cycle_id": 8,
            "module_key": module_key,
            "disposition": disposition,
            "reason": None,
            "source_record_type": None,
            "source_record_id": None,
            "actor_user_id": 1,
            "occurred_at": NOW,
        }
        values.update(overrides)
        return record_module_disposition(self.connection, **values)

    def progress(self):
        from student_lifecycle.module_progress import load_cycle_module_progress

        return {
            item.module_key: item
            for item in load_cycle_module_progress(self.connection, 8)
        }

    def test_required_modules_use_only_current_cycle_canonical_facts(self):
        progress = self.progress()

        self.assertEqual("required_satisfied", progress["applications"].state)
        self.assertTrue(progress["applications"].satisfied)
        self.assertEqual("required_pending", progress["offer"].state)
        self.assertFalse(progress["offer"].satisfied)
        self.assertEqual("required_pending", progress["coe_oshc"].state)
        self.assertFalse(progress["coe_oshc"].satisfied)

        # Historical Initial COE/OSHC exist, but they cannot satisfy Cycle 8.
        self.assertEqual(("coe",), progress["coe_oshc"].satisfied_components)

    def test_existing_source_is_revalidated_without_ownership_transfer(self):
        self.disposition(
            "visa",
            "existing",
            reason="Current visa remains valid",
            source_record_type="visa",
            source_record_id=13,
        )

        progress = self.progress()["visa"]

        self.assertEqual("existing_validated", progress.state)
        self.assertTrue(progress.satisfied)
        self.assertEqual(
            1,
            self.connection.execute(
                "SELECT application_cycle_id FROM visas WHERE id=13"
            ).fetchone()[0],
        )

    def test_not_applicable_is_audited_only_where_policy_allows_it(self):
        self.disposition(
            "payments",
            "not_applicable",
            reason="Scholarship covers all fees",
        )

        progress = self.progress()["payments"]

        self.assertEqual("not_applicable_audited", progress.state)
        self.assertTrue(progress.satisfied)
        self.assertEqual("Scholarship covers all fees", progress.reason)

    def test_existing_oshc_needs_valid_history_and_current_cycle_coe(self):
        self.disposition(
            "coe_oshc",
            "existing",
            reason="Existing cover remains valid for the new course",
            source_record_type="oshc",
            source_record_id=12,
        )

        accepted = self.progress()["coe_oshc"]
        self.assertEqual("existing_validated", accepted.state)
        self.assertTrue(accepted.satisfied)
        self.assertEqual(("coe", "oshc"), accepted.satisfied_components)
        self.assertEqual(
            1,
            self.connection.execute(
                "SELECT application_cycle_id FROM coe_oshc WHERE id=12"
            ).fetchone()[0],
        )

        self.connection.execute("DELETE FROM progression_document_links")
        missing_new_coe = self.progress()["coe_oshc"]
        self.assertEqual("existing_validated", missing_new_coe.state)
        self.assertFalse(missing_new_coe.satisfied)
        self.assertEqual(("oshc",), missing_new_coe.satisfied_components)

    def test_progress_is_not_a_completed_modules_over_six_rule(self):
        self.disposition(
            "homestay_airport",
            "not_applicable",
            reason="Student lives independently",
        )
        progress = self.progress()

        self.assertEqual(6, len(progress))
        self.assertTrue(progress["homestay_airport"].satisfied)
        self.assertFalse(progress["offer"].satisfied)
        self.assertNotIn("completed_count", vars(progress["offer"]))

    def test_existing_visa_and_na_accommodation_allow_applicable_work_to_finish(self):
        self.connection.execute(
            """INSERT INTO progression_case_offers(
                 id,case_id,target_application_id,offer_status
               ) VALUES(701,80,801,'accepted')"""
        )
        self.disposition(
            "coe_oshc",
            "existing",
            reason="Existing OSHC remains valid; current COE is confirmed",
            source_record_type="oshc",
            source_record_id=12,
        )
        self.disposition(
            "visa",
            "existing",
            reason="Current visa remains valid",
            source_record_type="visa",
            source_record_id=13,
        )
        self.disposition(
            "payments",
            "not_applicable",
            reason="Scholarship covers all fees",
        )
        self.disposition(
            "homestay_airport",
            "not_applicable",
            reason="Student lives independently",
        )

        progress = self.progress()

        self.assertTrue(all(item.satisfied for item in progress.values()))
        self.assertEqual("existing_validated", progress["visa"].state)
        self.assertEqual(
            "not_applicable_audited", progress["homestay_airport"].state
        )
        self.assertEqual(
            1,
            self.connection.execute(
                "SELECT application_cycle_id FROM visas WHERE id=13"
            ).fetchone()[0],
        )


if __name__ == "__main__":
    unittest.main()
