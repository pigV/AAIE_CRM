import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests.test_workflow_application_cycles_migration import (
    apply_workflow_migration,
    build_legacy_fixture,
)
from workflow_backfill import apply_deterministic_backfill, build_backfill_preview, preview_to_json


class WorkflowDataResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.path = Path(self.temp_dir.name) / "resolution.db"
        conn = build_legacy_fixture(self.path)
        conn.executescript(
            """
            ALTER TABLE parent_form_tokens ADD COLUMN created_at TEXT;
            ALTER TABLE parent_form_tokens ADD COLUMN expires_at TEXT;
            ALTER TABLE parent_form_tokens ADD COLUMN student_hint TEXT;
            ALTER TABLE students ADD COLUMN deleted_at TEXT;
            ALTER TABLE students ADD COLUMN passport_first_name TEXT;
            ALTER TABLE students ADD COLUMN passport_last_name TEXT;
            ALTER TABLE students ADD COLUMN english_nickname TEXT;
            ALTER TABLE contracts ADD COLUMN generated_at TEXT;
            ALTER TABLE contracts ADD COLUMN created_at TEXT;
            ALTER TABLE contracts ADD COLUMN deleted_at TEXT;
            ALTER TABLE contracts ADD COLUMN voided_at TEXT;
            ALTER TABLE school_plan_report_confirmations ADD COLUMN status TEXT;
            ALTER TABLE school_plan_report_confirmations ADD COLUMN signature_image_path TEXT;
            ALTER TABLE school_plan_confirmed_schools ADD COLUMN is_selected INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE offers ADD COLUMN document_type TEXT;
            ALTER TABLE offers ADD COLUMN accepted_date TEXT;
            UPDATE parent_form_tokens
               SET created_at='2026-08-10T09:00:00+10:00',
                   expires_at='2026-08-12T09:00:00+10:00';
            CREATE TABLE workflow_status(student_id TEXT PRIMARY KEY,current_stage TEXT);
            INSERT INTO workflow_status VALUES('S1','签证下签或拒签');
            INSERT INTO workflow_status VALUES('S2','建档');
            INSERT INTO student_cases(id,student_id,case_type) VALUES(12,'S2','Initial');
            CREATE TABLE visas(
              id INTEGER PRIMARY KEY,student_id TEXT,visa_status TEXT,
              visa_grant_date TEXT,created_at TEXT
            );
            INSERT INTO visas VALUES(1,'S1','granted','2026-07-20','2026-07-21');
            INSERT INTO contracts(id,student_id,contract_status,signed_at,fully_executed_at)
            VALUES(11,'S2','Sent',NULL,NULL);
            UPDATE contracts
               SET generated_at='2026-08-01T09:00:00+10:00',
                   created_at='2026-08-01T09:00:00+10:00'
             WHERE id=11;
            INSERT INTO students(student_id,current_status) VALUES('S3','New');
            INSERT INTO workflow_status VALUES('S3','建档');
            INSERT INTO student_documents(id,student_id,document_type,uploaded_at,is_active)
            VALUES(99,'S2','VisaGrantLetter','2026-08-09T12:00:00+10:00',1);
            """
        )
        apply_workflow_migration(conn)
        conn.commit()
        conn.close()
        apply_deterministic_backfill(self.path, actor="test")

    def tearDown(self):
        self.temp_dir.cleanup()

    def load_bundle(self):
        from workflow_data_resolution import build_data_resolution_bundle

        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            preview = json.loads(preview_to_json(build_backfill_preview(conn)))
            return build_data_resolution_bundle(conn, preview, today=date(2026, 8, 11))

    def test_active_parity_separates_stage_one_and_missing_cycle_students(self):
        bundle = self.load_bundle()

        self.assertIn("S2", bundle.active_parity["present_in_both"])
        self.assertIn("S3", bundle.active_parity["legacy_only"])
        self.assertEqual(1, len(bundle.active_parity["canonical_workflow_cases"]))
        self.assertNotIn("S1", bundle.active_parity["canonical_student_ids"])
        self.assertEqual([], bundle.stage_parity["duplicate_active_students"])

    def test_conflict_and_missing_fact_classifications_are_auditable(self):
        bundle = self.load_bundle()

        conflict = next(item for item in bundle.business_fact_conflicts if item["student_id"] == "S2")
        self.assertEqual("RESOLUTION_PROPOSED_DETERMINISTIC", conflict["resolution"])
        self.assertEqual("03_contract_signature", conflict["recommended_stage_key"])
        missing = next(item for item in bundle.missing_source_facts if item["source_ids"] == ["S3"])
        self.assertEqual("DASHBOARD_BLOCKING", missing["disposition"])
        self.assertTrue(missing["production_blocker"])
        self.assertIn("grant-event:S2", {item["key"] for item in bundle.manual_decisions})

    def test_terminal_grant_is_closed_without_grant_metric(self):
        bundle = self.load_bundle()

        self.assertEqual("closed", bundle.cycle_outcomes["S1"]["status"])
        self.assertEqual("granted", bundle.cycle_outcomes["S1"]["outcome"])
        self.assertEqual(0, bundle.metric_parity["canonical"]["week_grants"])
        self.assertEqual(0, bundle.metric_parity["canonical"]["month_grants"])

    def test_rendered_report_set_is_complete_and_stable(self):
        from workflow_data_resolution import render_data_resolution_reports

        bundle = self.load_bundle()
        first = render_data_resolution_reports(bundle)
        second = render_data_resolution_reports(bundle)

        self.assertEqual(first, second)
        self.assertEqual(
            {
                "BUSINESS_FACT_CONFLICT_REVIEW.md",
                "MANUAL_DECISION_PACK.md",
                "MISSING_SOURCE_FACT_DISPOSITION.md",
                "ACTIVE_STUDENTS_PARITY.md",
                "STAGE_DISTRIBUTION_PARITY.md",
                "MONTHLY_CONTRACT_PARITY.md",
                "SIGNED_SCHOOL_STRUCTURE_PARITY.md",
                "ORIGINAL_FOUR_STUDENTS_VERIFICATION.md",
                "STAGE_01_VERIFICATION.md",
                "DATA_RESOLUTION_SUMMARY.json",
            },
            set(first),
        )
        self.assertIn("RESOLUTION_PROPOSED_DETERMINISTIC", first["BUSINESS_FACT_CONFLICT_REVIEW.md"])
        self.assertNotIn("/parent-form/", first["STAGE_01_VERIFICATION.md"])
        summary = json.loads(first["DATA_RESOLUTION_SUMMARY.json"])
        self.assertIn("remaining_blocking_count", summary)

    def test_report_cli_requires_immutable_read_only_uri_and_is_repeatable(self):
        from tools.workflow_data_resolution_report import main, open_read_only_database

        with self.assertRaisesRegex(ValueError, "mode=ro"):
            open_read_only_database(str(self.path))
        with self.assertRaisesRegex(ValueError, "immutable=1"):
            open_read_only_database(f"file:{self.path}?mode=ro")

        preview_path = Path(self.temp_dir.name) / "preview.json"
        with sqlite3.connect(self.path) as conn:
            preview_path.write_text(
                preview_to_json(build_backfill_preview(conn)), encoding="utf-8"
            )
        first = Path(self.temp_dir.name) / "reports-one"
        second = Path(self.temp_dir.name) / "reports-two"
        uri = f"file:{self.path}?mode=ro&immutable=1"
        self.assertEqual(0, main([uri, str(preview_path), str(first), "--today", "2026-08-11"]))
        self.assertEqual(0, main([uri, str(preview_path), str(second), "--today", "2026-08-11"]))
        self.assertEqual(
            {path.name: path.read_bytes() for path in first.iterdir()},
            {path.name: path.read_bytes() for path in second.iterdir()},
        )


if __name__ == "__main__":
    unittest.main()
