from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.test_revenue_migration_032 import MIGRATION as MIGRATION_032
from tests.test_revenue_migration_032 import build_legacy_database, fingerprint


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/20260830_033_revenue_claim_eligibility_evidence.sql"
FORWARD_CHECK = ROOT / "migrations/checks/20260830_033_revenue_claim_eligibility_evidence_forward_check.sql"
ROLLBACK = ROOT / "migrations/rollback/20260830_033_revenue_claim_eligibility_evidence_rollback.sql"


class RevenueMigration033Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "revenue-033.sqlite3"
        build_legacy_database(self.database_path)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TABLE student_documents(
                     id INTEGER PRIMARY KEY,
                     student_id TEXT NOT NULL REFERENCES students(student_id),
                     document_type TEXT NOT NULL,
                     file_path TEXT NOT NULL
                   )"""
            )
            connection.executescript(MIGRATION_032.read_text(encoding="utf-8"))
            connection.commit()
            self.core_before = {
                table: fingerprint(connection, table)
                for table in ("users", "students", "application_cycles", "student_documents")
            }
        self.migration_032_sha256 = hashlib.sha256(MIGRATION_032.read_bytes()).hexdigest()

    def apply(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(MIGRATION.read_text(encoding="utf-8"))
            connection.commit()

    def test_package_uses_next_slot_and_never_changes_migration_032(self):
        self.assertTrue(MIGRATION.is_file())
        self.assertTrue(FORWARD_CHECK.is_file())
        self.assertTrue(ROLLBACK.is_file())
        versions = sorted(path.name for path in (ROOT / "migrations").glob("*.sql"))
        self.assertEqual(
            "20260829_032_revenue_commission_v1.sql",
            versions[versions.index(MIGRATION.name) - 1],
        )
        self.assertEqual(self.migration_032_sha256, hashlib.sha256(MIGRATION_032.read_bytes()).hexdigest())

    def test_adds_only_structured_claim_eligibility_and_evidence_authority(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            added = {
                row[0]
                for row in connection.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name IN (
                         'revenue_claim_eligibility_evaluations',
                         'revenue_claim_evidence_links'
                       )"""
                )
            }
            self.assertEqual(
                {"revenue_claim_eligibility_evaluations", "revenue_claim_evidence_links"},
                added,
            )
            evaluation_columns = {
                row[1]: row[2].upper()
                for row in connection.execute(
                    'PRAGMA table_info("revenue_claim_eligibility_evaluations")'
                )
            }
            for name in (
                "claim_id", "commission_line_id", "rule_snapshot_id", "evaluation_version",
                "result_code", "eligible", "evaluator_source", "authoritative_facts_json",
                "authoritative_facts_sha256", "reason_code", "reason_message",
                "evaluated_by", "evaluated_at", "created_at",
            ):
                self.assertIn(name, evaluation_columns)
            evidence_columns = {
                row[1]: row[2].upper()
                for row in connection.execute('PRAGMA table_info("revenue_claim_evidence_links")')
            }
            for name in (
                "claim_id", "claim_document_requirement_id", "revenue_document_id",
                "student_document_id", "source_kind", "source_reference",
                "verification_status", "verified_by", "verified_at", "created_at",
            ):
                self.assertIn(name, evidence_columns)

            evaluation_fks = {row[3] for row in connection.execute(
                'PRAGMA foreign_key_list("revenue_claim_eligibility_evaluations")'
            )}
            self.assertEqual(
                {"claim_id", "commission_line_id", "rule_snapshot_id", "evaluated_by"},
                evaluation_fks,
            )
            evidence_fks = {row[3] for row in connection.execute(
                'PRAGMA foreign_key_list("revenue_claim_evidence_links")'
            )}
            self.assertEqual(
                {
                    "claim_id", "claim_document_requirement_id", "revenue_document_id",
                    "student_document_id", "verified_by",
                },
                evidence_fks,
            )
            self.assertEqual(self.core_before, {
                table: fingerprint(connection, table) for table in self.core_before
            })
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_claim_eligibility_evaluations"
            ).fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_claim_evidence_links"
            ).fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual("ok", connection.execute("PRAGMA quick_check").fetchone()[0])

    def test_claim_evaluations_and_evidence_links_are_append_only(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("INSERT INTO revenue_rule_snapshots VALUES(1,'snapshot-1','commission','rule-1','{}',?,1,'2026-08-30T00:00:00Z')", ("a" * 64,))
            connection.execute("INSERT INTO revenue_entities VALUES(1,'entity-1','school','School',NULL,NULL,'AUD','active',1,'2026-08-30T00:00:00Z')")
            connection.execute("INSERT INTO revenue_commercial_cases VALUES(1,'case-1',100,'S-REVENUE','open',0,'{}',1,'2026-08-30T00:00:00Z',NULL)")
            connection.execute("INSERT INTO offer_received_events VALUES(200,100)")
            connection.execute("INSERT INTO cycle_final_acceptances VALUES(201,100,200)")
            connection.execute("INSERT INTO revenue_enrolment_components VALUES(1,'component-1',1,'school','acceptance',201,201,NULL,NULL,NULL,NULL,'active','2026-08-30T00:00:00Z')")
            connection.execute("INSERT INTO revenue_documents VALUES(1,'document-1','student_id',1,'/existing/id.pdf',?,'application/pdf',100,'imported',1,'2026-08-30T00:00:00Z')", ("b" * 64,))
            connection.execute("INSERT INTO student_documents VALUES(1,'S-REVENUE','StudentID','/existing/id.pdf')")
            connection.execute("INSERT INTO revenue_contract_groups VALUES(1,'group-1',1,'direct_commission',NULL,1,'2026-08-30T00:00:00Z')")
            connection.execute("INSERT INTO revenue_contract_versions VALUES(1,'contract-1',1,1,1,'2026-01-01',NULL,NULL,'active',1,'2026-08-30T00:00:00Z','2026-08-30T00:00:00Z')")
            connection.execute("INSERT INTO revenue_commission_rule_versions VALUES(1,'rule-1',1,1,'2026-01-01',NULL,100,'active',1,1,'2026-08-30T00:00:00Z','2026-08-30T00:00:00Z')")
            connection.execute("INSERT INTO revenue_claim_document_requirements VALUES(1,'requirement-1',1,'student_id',1,'2026-08-30T00:00:00Z')")
            connection.execute("INSERT INTO revenue_commission_lines VALUES(1,'line-1',1,1,1,1,1,10000,10000,1000,11000,'AUD','claimable','2026-08-30T00:00:00Z')")
            connection.execute("INSERT INTO revenue_claims VALUES(1,'claim-1','CLM-2026-000001','direct',1,'2026-08-30',10000,1000,11000,'issued',NULL,1,'2026-08-30T00:00:00Z','2026-08-30T00:00:00Z')")
            connection.execute(
                """INSERT INTO revenue_claim_eligibility_evaluations(
                     uid,claim_id,commission_line_id,rule_snapshot_id,evaluation_version,
                     result_code,eligible,evaluator_source,authoritative_facts_json,
                     authoritative_facts_sha256,evaluated_by,evaluated_at,created_at
                   ) VALUES('evaluation-1',1,1,1,1,'eligible',1,'system','{}',?,1,
                            '2026-08-30T00:00:00Z','2026-08-30T00:00:00Z')""",
                ("c" * 64,),
            )
            connection.execute(
                """INSERT INTO revenue_claim_evidence_links(
                     uid,claim_id,claim_document_requirement_id,revenue_document_id,
                     student_document_id,source_kind,source_reference,verification_status,
                     verified_by,verified_at,created_at
                   ) VALUES('evidence-1',1,1,1,1,'student_document','1','verified',1,
                            '2026-08-30T00:00:00Z','2026-08-30T00:00:00Z')"""
            )
            for table in (
                "revenue_claim_eligibility_evaluations",
                "revenue_claim_evidence_links",
            ):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    connection.execute(f'UPDATE "{table}" SET created_at="changed"')
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    connection.execute(f'DELETE FROM "{table}"')

    def test_disposable_rollback_removes_only_033_objects(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(ROLLBACK.read_text(encoding="utf-8"))
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"
            )}
            self.assertNotIn("revenue_claim_eligibility_evaluations", names)
            self.assertNotIn("revenue_claim_evidence_links", names)
            self.assertIn("revenue_claims", names)
            self.assertIn("revenue_claim_document_requirements", names)
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())


if __name__ == "__main__":
    unittest.main()
