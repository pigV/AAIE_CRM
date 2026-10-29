from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

import migration_runner
from student_lifecycle.module_dispositions import (
    ModuleDispositionConflict,
    ModuleDispositionPolicyError,
    ModuleDispositionValidationError,
    evaluate_module_policy,
    load_module_dispositions,
    record_module_disposition,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
MIGRATION_030 = (
    MIGRATIONS / "20260824_030_lifecycle_module_dispositions.sql"
)
FORWARD_030 = (
    MIGRATIONS
    / "checks"
    / "20260824_030_lifecycle_module_dispositions_forward_check.sql"
)
NOW = "2026-08-24T10:00:00+10:00"


class LifecycleModuleDispositionTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE users(id INTEGER PRIMARY KEY);
            CREATE TABLE students(student_id TEXT PRIMARY KEY);
            CREATE TABLE contracts(
              id INTEGER PRIMARY KEY,student_id TEXT,
              contract_type TEXT,contract_status TEXT,
              deleted_at TEXT,voided_at TEXT
            );
            CREATE TABLE student_cases(
              id INTEGER PRIMARY KEY,student_id TEXT,case_type TEXT
            );
            CREATE TABLE progression_cases(
              case_id INTEGER PRIMARY KEY,business_type TEXT,
              source_contract_id INTEGER
            );
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              cycle_type TEXT NOT NULL,
              status TEXT NOT NULL,
              education_stream TEXT NOT NULL
            );
            CREATE TABLE offers(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              application_cycle_id INTEGER REFERENCES application_cycles(id)
            );
            CREATE TABLE coe_oshc(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              document_kind TEXT NOT NULL,
              application_cycle_id INTEGER REFERENCES application_cycles(id)
            );
            CREATE TABLE visas(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              application_cycle_id INTEGER REFERENCES application_cycles(id)
            );
            CREATE TABLE visa_cases(
              id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,
              legacy_visa_id INTEGER,application_cycle_id INTEGER
            );
            CREATE TABLE payments(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              application_cycle_id INTEGER REFERENCES application_cycles(id)
            );
            CREATE TABLE student_documents(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              application_cycle_id INTEGER REFERENCES application_cycles(id)
            );
            CREATE TABLE higher_education_applications(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER REFERENCES application_cycles(id)
            );
            INSERT INTO users(id) VALUES(1);
            INSERT INTO students(student_id) VALUES('student-1'),('student-2');
            INSERT INTO application_cycles(
              id,student_id,cycle_type,status,education_stream
            ) VALUES
              (1,'student-1','initial','completed','school_education'),
              (7,'student-1','transfer','active','school_education'),
              (8,'student-1','progression','active','higher_education'),
              (9,'student-2','initial','completed','school_education');
            INSERT INTO offers(id,student_id,application_cycle_id)
              VALUES(21,'student-1',1);
            INSERT INTO coe_oshc(id,student_id,document_kind,application_cycle_id)
              VALUES(22,'student-1','COE',1),(23,'student-1','OSHC',1);
            INSERT INTO visas(id,student_id,application_cycle_id)
              VALUES(31,'student-1',1),(32,'student-2',9),(33,'student-1',8),
                    (34,'student-1',NULL);
            INSERT INTO payments(id,student_id,application_cycle_id)
              VALUES(41,'student-1',1);
            INSERT INTO student_documents(id,student_id,application_cycle_id)
              VALUES(51,'student-1',1);
            INSERT INTO higher_education_applications(id,application_cycle_id)
              VALUES(61,1);
            """
        )
        self.connection.executescript(MIGRATION_030.read_text(encoding="utf-8"))

    def tearDown(self):
        self.connection.close()

    def record(self, **overrides):
        values = {
            "event_uid": "disp-cycle-7-visa-existing",
            "application_cycle_id": 7,
            "module_key": "visa",
            "disposition": "existing",
            "reason": "Current visa remains valid",
            "source_record_type": "visa",
            "source_record_id": 31,
            "actor_user_id": 1,
            "occurred_at": NOW,
        }
        values.update(overrides)
        return record_module_disposition(self.connection, **values)

    def test_migration_package_schema_indexes_and_append_only_triggers(self):
        self.assertTrue(MIGRATION_030.is_file())
        self.assertTrue(FORWARD_030.is_file())
        migration = next(
            item
            for item in migration_runner.discover_migrations(MIGRATIONS)
            if item.version == "20260824_030"
        )
        self.assertEqual(migration.name, "lifecycle_module_dispositions")
        objects = {
            (row[0], row[1])
            for row in self.connection.execute(
                """SELECT type,name FROM sqlite_master
                   WHERE name LIKE 'lifecycle_module_disposition%'"""
            )
        }
        self.assertIn(
            ("table", "lifecycle_module_disposition_events"), objects
        )
        self.assertIn(
            ("trigger", "lifecycle_module_disposition_events_no_update"),
            objects,
        )
        self.assertIn(
            ("trigger", "lifecycle_module_disposition_events_no_delete"),
            objects,
        )

    def test_runner_applies_replays_and_verifies_migration_030(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            database = root / "fixture.db"
            package = root / "migrations"
            checks = package / "checks"
            checks.mkdir(parents=True)
            shutil.copy2(MIGRATION_030, package / MIGRATION_030.name)
            shutil.copy2(FORWARD_030, checks / FORWARD_030.name)
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE users(id INTEGER PRIMARY KEY);
                    CREATE TABLE students(student_id TEXT PRIMARY KEY);
                    CREATE TABLE contracts(
                      id INTEGER PRIMARY KEY,student_id TEXT,
                      contract_type TEXT,contract_status TEXT,
                      deleted_at TEXT,voided_at TEXT
                    );
                    CREATE TABLE student_cases(
                      id INTEGER PRIMARY KEY,student_id TEXT,case_type TEXT
                    );
                    CREATE TABLE progression_cases(
                      case_id INTEGER PRIMARY KEY,business_type TEXT,
                      source_contract_id INTEGER
                    );
                    CREATE TABLE application_cycles(
                      id INTEGER PRIMARY KEY,
                      student_id TEXT NOT NULL REFERENCES students(student_id)
                    );
                    """
                )
            first = migration_runner.apply_migrations(
                database,
                package,
                applied_by="module-disposition-test",
                source_commit="module-disposition-test",
                formal_database_path=None,
            )
            second = migration_runner.apply_migrations(
                database,
                package,
                applied_by="module-disposition-test",
                source_commit="module-disposition-test",
                formal_database_path=None,
            )
            verified = migration_runner.verify_migrations(database, package)
            self.assertEqual(first["applied"], ["20260824_030"])
            self.assertEqual(second["already_applied"], ["20260824_030"])
            self.assertEqual(verified["verified"], ["20260824_030"])

    def test_events_are_append_only_and_latest_projection_is_deterministic(self):
        first = self.record()
        second = self.record(
            event_uid="disp-cycle-7-visa-required",
            disposition="required",
            reason="Reassessment requires a new visa",
            source_record_type=None,
            source_record_id=None,
            occurred_at="2026-08-24T11:00:00+10:00",
        )
        current = load_module_dispositions(self.connection, 7)
        self.assertEqual(first.id + 1, second.id)
        self.assertEqual("required", current["visa"].disposition)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE lifecycle_module_disposition_events SET reason='x' WHERE id=?",
                (first.id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM lifecycle_module_disposition_events WHERE id=?",
                (first.id,),
            )

    def test_event_uid_replay_is_idempotent_but_changed_payload_conflicts(self):
        first = self.record()
        replay = self.record()
        self.assertEqual(first, replay)
        count = self.connection.execute(
            "SELECT count(*) FROM lifecycle_module_disposition_events"
        ).fetchone()[0]
        self.assertEqual(1, count)
        with self.assertRaises(ModuleDispositionConflict):
            self.record(reason="Different payload")

    def test_existing_source_must_match_module_and_cycle_student_without_reownership(self):
        policy = evaluate_module_policy(
            self.connection,
            application_cycle_id=7,
            module_key="visa",
            disposition="existing",
            reason="Current visa remains valid",
            source_record_type="visa",
            source_record_id=31,
        )
        self.assertTrue(policy.allowed)
        self.record()
        owner_cycle = self.connection.execute(
            "SELECT application_cycle_id FROM visas WHERE id=31"
        ).fetchone()[0]
        self.assertEqual(1, owner_cycle)
        with self.assertRaises(ModuleDispositionPolicyError):
            self.record(
                event_uid="wrong-student",
                source_record_id=32,
            )
        with self.assertRaises(ModuleDispositionPolicyError):
            self.record(
                event_uid="wrong-module",
                module_key="offer",
            )
        with self.assertRaises(ModuleDispositionPolicyError) as active_source:
            self.record(
                event_uid="active-source",
                source_record_id=33,
            )
        self.assertEqual(
            "existing_source_not_historical", active_source.exception.code
        )

    def test_existing_visa_uses_production_legacy_link_to_historical_visa_case(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            """
            CREATE TABLE students(student_id TEXT PRIMARY KEY);
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,student_id TEXT,cycle_type TEXT,status TEXT
            );
            CREATE TABLE visas(
              id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,created_at TEXT NOT NULL
            );
            CREATE TABLE visa_cases(
              id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,
              legacy_visa_id INTEGER,application_cycle_id INTEGER
            );
            INSERT INTO students VALUES('student-1');
            INSERT INTO application_cycles VALUES
              (1,'student-1','initial','closed'),
              (7,'student-1','transfer','active');
            INSERT INTO visas VALUES(31,'student-1','2025-01-01');
            INSERT INTO visa_cases VALUES(41,'student-1',31,1);
            """
        )
        try:
            policy = evaluate_module_policy(
                connection,
                application_cycle_id=7,
                module_key="visa",
                disposition="existing",
                reason="Current visa remains valid",
                source_record_type="visa",
                source_record_id=31,
            )
        finally:
            connection.close()

        self.assertTrue(policy.allowed)
        self.assertEqual(1, policy.source_cycle_id)

    def test_explicit_external_existing_visa_evidence_is_current_transfer_source(self):
        recorded = self.record(
            event_uid="external-existing-visa",
            reason="系统登记 Existing Visa：Visa Grant 2026-03-15",
            source_record_type="visa",
            source_record_id=34,
        )

        self.assertEqual("existing", recorded.disposition)
        policy = evaluate_module_policy(
            self.connection,
            application_cycle_id=7,
            module_key="visa",
            disposition="existing",
            reason="系统登记 Existing Visa：Visa Grant 2026-03-15",
            source_record_type="visa",
            source_record_id=34,
        )
        self.assertTrue(policy.allowed)
        self.assertEqual("existing_external_visa_evidence", policy.code)

    def test_external_existing_visa_evidence_never_weakens_new_visa_case_policy(self):
        self.connection.execute(
            "INSERT INTO visa_cases VALUES(90,'student-1',34,7)"
        )
        with self.assertRaises(ModuleDispositionPolicyError) as caught:
            self.record(
                event_uid="not-external-after-case-link",
                reason="系统登记 Existing Visa：Visa Grant 2026-03-15",
                source_record_type="visa",
                source_record_id=34,
            )
        self.assertEqual(
            "existing_visa_evidence_has_visa_case", caught.exception.code
        )

    def test_offer_and_coe_cannot_be_reused_for_a_new_target(self):
        for module_key, source_record_type, source_record_id in (
            ("offer", "offer", 21),
            ("coe_oshc", "coe", 22),
        ):
            with self.subTest(module_key=module_key):
                with self.assertRaises(ModuleDispositionPolicyError):
                    self.record(
                        event_uid=f"forbidden-{module_key}",
                        module_key=module_key,
                        source_record_type=source_record_type,
                        source_record_id=source_record_id,
                )

    def test_higher_education_application_source_derives_student_from_cycle(self):
        existing = self.record(
            event_uid="existing-he-application",
            module_key="applications",
            source_record_type="higher_education_application",
            source_record_id=61,
        )
        self.assertEqual("existing", existing.disposition)

    def test_existing_oshc_is_allowed_but_progression_oshc_na_is_rejected(self):
        existing = self.record(
            event_uid="progression-existing-oshc",
            application_cycle_id=8,
            module_key="coe_oshc",
            source_record_type="oshc",
            source_record_id=23,
        )
        self.assertEqual("existing", existing.disposition)
        with self.assertRaises(ModuleDispositionPolicyError):
            self.record(
                event_uid="progression-oshc-na",
                application_cycle_id=8,
                module_key="coe_oshc",
                disposition="not_applicable",
                reason="No update requested",
                source_record_type=None,
                source_record_id=None,
            )

    def test_not_applicable_requires_reason_and_is_policy_bounded(self):
        allowed = self.record(
            event_uid="transfer-accommodation-na",
            module_key="homestay_airport",
            disposition="not_applicable",
            reason="Student is an adult living independently",
            source_record_type=None,
            source_record_id=None,
        )
        self.assertEqual("not_applicable", allowed.disposition)
        with self.assertRaises(ModuleDispositionValidationError):
            self.record(
                event_uid="missing-reason",
                module_key="homestay_airport",
                disposition="not_applicable",
                reason="  ",
                source_record_type=None,
                source_record_id=None,
            )
        with self.assertRaises(ModuleDispositionPolicyError):
            self.record(
                event_uid="offer-na",
                module_key="offer",
                disposition="not_applicable",
                reason="Not needed",
                source_record_type=None,
                source_record_id=None,
            )


if __name__ == "__main__":
    unittest.main()
