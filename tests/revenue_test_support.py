from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile

from tests.test_revenue_migration_032 import MIGRATION, build_legacy_database


MIGRATION_033 = (
    Path(__file__).resolve().parents[1]
    / "migrations/20260830_033_revenue_claim_eligibility_evidence.sql"
)
MIGRATION_034 = (
    Path(__file__).resolve().parents[1]
    / "migrations/20260901_034_revenue_client_payment_receipt.sql"
)


class RevenueDatabaseFixture:
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "revenue.sqlite3"
        build_legacy_database(self.database_path)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(MIGRATION.read_text(encoding="utf-8"))
            connection.execute(
                """CREATE TABLE student_documents(
                     id INTEGER PRIMARY KEY,
                     student_id TEXT NOT NULL REFERENCES students(student_id),
                     folder_category TEXT NOT NULL DEFAULT 'Applications',
                     document_type TEXT NOT NULL,
                     original_name TEXT NOT NULL,
                     stored_name TEXT NOT NULL,
                     file_path TEXT NOT NULL,
                     version INTEGER NOT NULL DEFAULT 1,
                     file_size INTEGER NOT NULL DEFAULT 0,
                     uploaded_by INTEGER REFERENCES users(id),
                     uploaded_at TEXT NOT NULL,
                     preview_path TEXT,
                     is_active INTEGER NOT NULL DEFAULT 1
                   )"""
            )
            connection.executescript(MIGRATION_033.read_text(encoding="utf-8"))
            connection.executescript(MIGRATION_034.read_text(encoding="utf-8"))
            connection.commit()

    def connect(self):
        connection = sqlite3.connect(self.database_path)
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def seed_prior_existing_cycle(self, connection):
        connection.execute(
            "INSERT INTO application_cycles VALUES(90,'cycle-90','S-REVENUE','initial','closed','school_education','2025-01-01')"
        )
        connection.execute(
            "INSERT INTO contracts VALUES(50,'S-REVENUE',90,'FullyExecuted','2025-01-02','2025-01-03T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO private_school_applications VALUES(60,90,'2025-01-04','withdrawn')"
        )

    def seed_component_authorities(self, connection):
        connection.execute(
            "INSERT INTO offer_received_events(id,application_cycle_id) VALUES(200,100)"
        )
        connection.execute(
            """INSERT INTO cycle_final_acceptances(
                 id,application_cycle_id,offer_received_event_id
               ) VALUES(201,100,200)"""
        )
        connection.execute(
            "INSERT INTO coe_oshc(id,application_cycle_id) VALUES(202,100)"
        )
        connection.execute(
            "INSERT INTO visa_cases(id,application_cycle_id) VALUES(203,100)"
        )
        connection.execute(
            """INSERT INTO student_school_enrolments(
                 id,application_cycle_id,school_id
               ) VALUES(204,100,10)"""
        )
