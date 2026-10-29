import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.test_workflow_application_cycles_migration import (
    apply_workflow_migration,
    build_legacy_fixture,
)
from tools.workflow_backfill_preview import open_read_only_database
from workflow_backfill import (
    BackfillClassification,
    build_backfill_preview,
    preview_to_json,
    preview_to_markdown,
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WorkflowBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "backfill.db"
        self.conn = build_legacy_fixture(self.path)
        self.conn.row_factory = sqlite3.Row
        apply_workflow_migration(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_safe_initial_cycle_and_unambiguous_record_links_receive_proposals(self):
        preview = build_backfill_preview(self.conn)

        s1_safe = [
            item
            for item in preview.safe
            if "S1" in item.source_ids and item.rule_id == "LEGACY_INITIAL_CYCLE_EVIDENCE"
        ]
        contract_links = [
            item
            for item in preview.safe
            if item.subject_kind == "contracts" and "10" in item.source_ids
        ]
        self.assertEqual(1, len(s1_safe))
        self.assertTrue(s1_safe[0].proposals)
        self.assertEqual(1, len(contract_links))
        self.assertTrue(contract_links[0].proposals)

    def test_multiple_explicit_cycles_make_legacy_record_ownership_ambiguous(self):
        self.conn.execute(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(101,'S1','Transfer')"
        )

        preview = build_backfill_preview(self.conn)

        ambiguous_contract = next(
            item
            for item in preview.ambiguous
            if item.subject_kind == "contracts" and "10" in item.source_ids
        )
        self.assertEqual("MULTIPLE_PLAUSIBLE_CYCLES", ambiguous_contract.rule_id)
        self.assertEqual((), ambiguous_contract.proposals)

    def test_ambiguous_and_missing_facts_never_receive_automatic_proposals(self):
        self.conn.execute(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(102,'S1','Progression')"
        )

        preview = build_backfill_preview(self.conn)

        self.assertTrue(preview.ambiguous)
        self.assertTrue(preview.missing_source_fact)
        self.assertTrue(all(not item.proposals for item in preview.ambiguous))
        self.assertTrue(all(not item.proposals for item in preview.missing_source_fact))

    def test_missing_final_school_submission_is_not_inferred_from_coe(self):
        preview = build_backfill_preview(self.conn)

        item = next(
            item
            for item in preview.missing_source_fact
            if item.rule_id == "FINAL_SCHOOL_SUBMISSION_NOT_RECORDED"
        )
        self.assertIn("coe_oshc:40", item.evidence)
        self.assertEqual((), item.proposals)

    def test_preview_is_repeatable_and_does_not_change_database(self):
        before = digest(self.path)

        first = preview_to_json(build_backfill_preview(self.conn))
        second = preview_to_json(build_backfill_preview(self.conn))

        self.assertEqual(first, second)
        self.assertNotIn("parent-token-1", first)
        self.assertEqual(before, digest(self.path))
        self.assertIn("SAFE / DETERMINISTIC", preview_to_markdown(build_backfill_preview(self.conn)))

    def test_cli_requires_read_only_immutable_uri(self):
        with self.assertRaisesRegex(ValueError, "mode=ro"):
            open_read_only_database(str(self.path))
        uri = f"file:{self.path}?mode=ro&immutable=1"
        read_only = open_read_only_database(uri)
        try:
            self.assertEqual(1, read_only.execute("SELECT 1").fetchone()[0])
            with self.assertRaises(sqlite3.OperationalError):
                read_only.execute("CREATE TABLE forbidden_write(id INTEGER)")
        finally:
            read_only.close()

    def test_classification_values_are_report_contract(self):
        self.assertEqual("SAFE", BackfillClassification.SAFE.value)
        self.assertEqual("AMBIGUOUS", BackfillClassification.AMBIGUOUS.value)
        self.assertEqual(
            "MISSING_SOURCE_FACT", BackfillClassification.MISSING_SOURCE_FACT.value
        )

    def test_unsubmitted_parent_form_without_student_is_safe_stage_one_cycle(self):
        self.conn.execute("ALTER TABLE parent_form_tokens ADD COLUMN created_at TEXT")
        self.conn.execute("ALTER TABLE parent_form_tokens ADD COLUMN expires_at TEXT")
        self.conn.execute(
            """UPDATE parent_form_tokens
                  SET created_at='2026-08-10T10:00:00+10:00',
                      expires_at='2026-08-12T10:00:00+10:00'
                WHERE token='parent-token-1'"""
        )
        self.conn.execute(
            "INSERT INTO parent_form_tokens(token,created_at,expires_at) VALUES('expired','2026-08-11T10:00:00+10:00','2026-08-11T09:00:00+10:00')"
        )

        preview = build_backfill_preview(self.conn)

        item = next(
            item for item in preview.safe
            if item.rule_id == "UNSUBMITTED_PARENT_FORM_WORKFLOW_CYCLE"
        )
        self.assertEqual("parent-token-1", dict(item.proposals[0].values)["parent_form_token"])
        self.assertEqual(1, sum(item.rule_id == "UNSUBMITTED_PARENT_FORM_WORKFLOW_CYCLE" for item in preview.safe))

    def test_consistent_legacy_grant_proposes_unique_cycle_closure_without_grant_event(self):
        self.conn.executescript(
            """
            CREATE TABLE visas(
              id INTEGER PRIMARY KEY,student_id TEXT,visa_status TEXT,
              visa_grant_date TEXT,created_at TEXT
            );
            INSERT INTO visas VALUES(1,'S1','granted','2026-07-20','2026-07-21');
            INSERT INTO visas VALUES(2,'S1','granted','2026-07-20','2026-07-21');
            """
        )

        preview = build_backfill_preview(self.conn)

        closure = next(
            item for item in preview.safe
            if item.rule_id == "LEGACY_GRANTED_CYCLE_CLOSURE"
        )
        values = dict(closure.proposals[0].values)
        self.assertEqual("closed", values["status"])
        self.assertEqual("granted", values["outcome"])
        self.assertEqual("2026-07-20", values["closed_at"])

    def test_conflicting_legacy_grant_dates_never_propose_cycle_closure(self):
        self.conn.executescript(
            """
            CREATE TABLE visas(
              id INTEGER PRIMARY KEY,student_id TEXT,visa_status TEXT,
              visa_grant_date TEXT,created_at TEXT
            );
            INSERT INTO visas VALUES(1,'S1','granted','2026-07-20','2026-07-21');
            INSERT INTO visas VALUES(2,'S1','granted','2026-08-01','2026-08-02');
            """
        )

        preview = build_backfill_preview(self.conn)

        self.assertFalse(
            any(item.rule_id == "LEGACY_GRANTED_CYCLE_CLOSURE" for item in preview.safe)
        )
        ambiguity = next(
            item for item in preview.ambiguous
            if item.rule_id == "CONFLICTING_LEGACY_TERMINAL_FACTS"
        )
        self.assertEqual((), ambiguity.proposals)


if __name__ == "__main__":
    unittest.main()
