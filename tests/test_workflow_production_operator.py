import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.test_workflow_application_cycles_migration import (
    apply_workflow_migration,
    build_legacy_fixture,
)


class WorkflowProductionOperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "aaie_crm.db"
        conn = build_legacy_fixture(self.database)
        apply_workflow_migration(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def _identity(self, **changes):
        from workflow_production_operator import (
            APPROVED_HUMAN_DECISION_SHA256,
            MIGRATION_017_PACKAGE_SHA256,
            OPERATOR_IMPLEMENTATION_VERSION,
            database_sha256,
        )

        values = {
            "database_path": self.database,
            "formal_database_path": self.database,
            "expected_database_sha256": database_sha256(self.database),
            "approved_candidate_commit": "a" * 40,
            "actual_candidate_commit": "a" * 40,
            "worktree_clean": True,
            "expected_operator_version": OPERATOR_IMPLEMENTATION_VERSION,
            "expected_operator_checksum": "b" * 64,
            "actual_operator_checksum": "b" * 64,
            "expected_environment": "production",
            "actual_environment": "production",
            "expected_migration_checksum": MIGRATION_017_PACKAGE_SHA256,
            "actual_migration_checksum": MIGRATION_017_PACKAGE_SHA256,
            "expected_human_decision_checksum": APPROVED_HUMAN_DECISION_SHA256,
            "actual_human_decision_checksum": APPROVED_HUMAN_DECISION_SHA256,
        }
        values.update(changes)
        return values

    def test_wrong_database_identity_fingerprint_commit_and_cleanliness_fail_closed(self):
        from workflow_production_operator import OperatorGateError, validate_execution_identity

        cases = (
            {"formal_database_path": self.root / "copied.db"},
            {"expected_database_sha256": "0" * 64},
            {"actual_candidate_commit": "c" * 40},
            {"worktree_clean": False},
            {"actual_operator_checksum": "0" * 64},
            {"actual_migration_checksum": "0" * 64},
            {"actual_human_decision_checksum": "0" * 64},
            {"actual_environment": "development"},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(OperatorGateError):
                validate_execution_identity(**self._identity(**changes))

    def test_validated_capability_cannot_be_forged(self):
        from workflow_production_operator import ValidatedProductionExecutionContext, validate_execution_identity

        with self.assertRaises(TypeError):
            ValidatedProductionExecutionContext()

        context = validate_execution_identity(**self._identity())
        self.assertEqual(self.database.resolve(), context.database_path)

    def test_confirmation_phrases_are_bound_to_actual_fingerprint(self):
        from workflow_production_operator import OperatorGateError, validate_confirmations

        fingerprint = "1234567890ab" + "c" * 52
        validate_confirmations(
            "backfill-apply",
            fingerprint,
            "CONFIRM_PRODUCTION_TARGET_1234567890ab",
            "APPLY_PRODUCTION_BACKFILL_1234567890ab",
        )
        for first, second in (("yes", "yes"), ("CONFIRM_PRODUCTION_TARGET_deadbeefdead", "APPLY_PRODUCTION_BACKFILL_deadbeefdead")):
            with self.assertRaises(OperatorGateError):
                validate_confirmations("backfill-apply", fingerprint, first, second)

    def test_artifact_checksum_excludes_timestamp_but_detects_state_change(self):
        from workflow_production_operator import seal_artifact

        first = seal_artifact({"artifact_version": 1, "timestamp": "one", "database_sha256": "a"})
        second = seal_artifact({"artifact_version": 1, "timestamp": "two", "database_sha256": "a"})
        changed = seal_artifact({"artifact_version": 1, "timestamp": "two", "database_sha256": "b"})
        self.assertEqual(first["artifact_checksum"], second["artifact_checksum"])
        self.assertNotEqual(first["artifact_checksum"], changed["artifact_checksum"])

    def test_operator_checksum_covers_the_declared_implementation_manifest(self):
        from workflow_production_operator import (
            OPERATOR_IMPLEMENTATION_FILES,
            PROJECT_ROOT,
            implementation_checksum,
        )

        digest = hashlib.sha256()
        for relative in OPERATOR_IMPLEMENTATION_FILES:
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update((PROJECT_ROOT / relative).read_bytes())
            digest.update(b"\0")
        self.assertEqual(digest.hexdigest(), implementation_checksum())

    def test_integrity_validation_rejects_quick_check_and_foreign_key_failures(self):
        from workflow_production_operator import OperatorGateError, validate_integrity_snapshot

        validate_integrity_snapshot({"quick_check": "ok", "foreign_key_violations": 0})
        for snapshot in (
            {"quick_check": "corrupt", "foreign_key_violations": 0},
            {"quick_check": "ok", "foreign_key_violations": 1},
        ):
            with self.subTest(snapshot=snapshot), self.assertRaises(OperatorGateError):
                validate_integrity_snapshot(snapshot)

    def test_stale_artifact_is_rejected_when_database_or_readiness_changes(self):
        from workflow_production_operator import OperatorGateError, validate_artifact_binding

        artifact = {
            "database_sha256": "a" * 64,
            "state_fingerprint": "b" * 64,
            "operator_checksum": "c" * 64,
            "human_decision_checksum": "d" * 64,
            "migration_checksum": "e" * 64,
            "approved_candidate_commit": "f" * 40,
            "operator_version": "v1",
        }
        current = dict(artifact)
        validate_artifact_binding(artifact, current)
        for field in artifact:
            if field == "database_sha256":
                continue
            with self.subTest(field=field), self.assertRaises(OperatorGateError):
                validate_artifact_binding(artifact, {**current, field: "0" * 64})

    def test_artifact_staleness_uses_business_state_not_raw_file_sha(self):
        from workflow_production_operator import OperatorGateError, validate_artifact_binding

        artifact = {
            "database_sha256": "a" * 64,
            "state_fingerprint": "b" * 64,
            "operator_checksum": "c" * 64,
            "human_decision_checksum": "d" * 64,
            "migration_checksum": "e" * 64,
            "approved_candidate_commit": "f" * 40,
            "operator_version": "v2",
        }
        current = dict(artifact)
        current["database_sha256"] = "0" * 64
        validate_artifact_binding(artifact, current)
        with self.assertRaisesRegex(OperatorGateError, "stale_dry_run_artifact"):
            validate_artifact_binding(
                artifact,
                {**current, "state_fingerprint": "1" * 64},
            )

    def test_operator_state_fingerprint_ignores_runtime_rows_and_tracks_business_rows(self):
        from workflow_production_operator import database_state_fingerprint

        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE backup_records(id INTEGER PRIMARY KEY,created_at TEXT)"
            )
        before = database_state_fingerprint(self.database)
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO backup_records VALUES(1,'runtime')")
        self.assertEqual(before, database_state_fingerprint(self.database))
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE students SET current_status='Changed' WHERE student_id='S1'"
            )
        self.assertNotEqual(before, database_state_fingerprint(self.database))

    def test_validated_context_binds_confirmations_to_business_state(self):
        from workflow_production_operator import (
            database_state_fingerprint,
            validate_execution_identity,
        )

        context = validate_execution_identity(**self._identity())
        self.assertEqual(
            database_state_fingerprint(self.database),
            context.state_fingerprint,
        )

    def test_status_is_read_only_and_exposes_explicit_readiness_contract(self):
        from datetime import date
        from workflow_production_operator import database_sha256, production_status

        before = database_sha256(self.database)
        result = production_status(
            self.database,
            candidate_commit="a" * 40,
            worktree_clean=True,
            today=date(2026, 8, 11),
        )
        self.assertEqual(before, database_sha256(self.database))
        self.assertEqual("rehearsal", result["execution_mode"])
        for key in (
            "backfill_ready", "reconciliation_ready", "p0", "p1_blockers",
            "v2_active", "metrics", "quick_check", "foreign_key_violations",
        ):
            self.assertIn(key, result)

    def test_file_audit_is_append_only_and_redacts_secrets(self):
        from workflow_production_operator import append_operator_audit

        path = self.root / "audit.jsonl"
        append_operator_audit(path, {"action": "status", "result": "success", "token": "SECRET", "password": "SECRET2"})
        append_operator_audit(path, {"action": "status", "result": "rejected", "failure_code": "wrong_commit"})
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(2, len(lines))
        self.assertNotIn("SECRET", path.read_text(encoding="utf-8"))
        self.assertEqual("status", json.loads(lines[0])["action"])

    def test_activation_rejects_p0_or_p1_blocking_artifact(self):
        from workflow_production_operator import (
            OperatorGateError,
            activate_production_v2,
            seal_artifact,
            validate_execution_identity,
        )

        context = validate_execution_identity(**self._identity())
        for blocker in ("p0", "p1_blockers"):
            artifact = seal_artifact({
                "artifact_kind": "v2-activation",
                "ready": False,
                blocker: 1,
            })
            with self.subTest(blocker=blocker), self.assertRaises(OperatorGateError):
                activate_production_v2(
                    context,
                    artifact,
                    artifact_checksum=artifact["artifact_checksum"],
                    target_confirmation="unused",
                    operation_confirmation="unused",
                    actor="test",
                    audit_path=self.root / "audit.jsonl",
                    today=__import__("datetime").date(2026, 8, 11),
                )

    def test_disable_file_audit_failure_rolls_back_v2_state_and_database_audit(self):
        from workflow_cutover import activate_workflow_v2, resolve_workflow_read_mode
        from workflow_production_operator import disable_production_v2, validate_execution_identity
        from tests.test_workflow_cutover import READY

        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                "CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_by INTEGER,updated_at TEXT NOT NULL)"
            )
            connection.execute(
                """CREATE TABLE audit_logs(
                     id INTEGER PRIMARY KEY,user_id INTEGER,username TEXT,action_type TEXT NOT NULL,
                     module_name TEXT NOT NULL,module_key TEXT,record_id TEXT,new_value TEXT,
                     device_info TEXT,status TEXT,created_at TEXT NOT NULL
                   )"""
            )
            activate_workflow_v2(connection, evidence=READY, actor="test")
            connection.commit()
        context = validate_execution_identity(**self._identity(
            expected_database_sha256=__import__("workflow_production_operator").database_sha256(self.database)
        ))
        short = context.state_fingerprint[:12]
        with self.assertRaises(OSError):
            disable_production_v2(
                context,
                target_confirmation=f"CONFIRM_PRODUCTION_TARGET_{short}",
                operation_confirmation=f"DISABLE_WORKFLOW_V2_{short}",
                actor="test",
                audit_path=self.root,
            )
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual("canonical_v2", resolve_workflow_read_mode(connection).value)
            count = connection.execute(
                "SELECT count(*) FROM audit_logs WHERE module_name='workflow_production_operator'"
            ).fetchone()[0]
            self.assertEqual(0, count)

    def test_disable_database_audit_failure_rolls_back_v2_state(self):
        from workflow_cutover import activate_workflow_v2, resolve_workflow_read_mode
        from workflow_production_operator import disable_production_v2, validate_execution_identity
        from tests.test_workflow_cutover import READY

        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                "CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_by INTEGER,updated_at TEXT NOT NULL)"
            )
            activate_workflow_v2(connection, evidence=READY, actor="test")
            connection.commit()
        context = validate_execution_identity(**self._identity(
            expected_database_sha256=__import__("workflow_production_operator").database_sha256(self.database)
        ))
        short = context.state_fingerprint[:12]
        with self.assertRaises(sqlite3.OperationalError):
            disable_production_v2(
                context,
                target_confirmation=f"CONFIRM_PRODUCTION_TARGET_{short}",
                operation_confirmation=f"DISABLE_WORKFLOW_V2_{short}",
                actor="test",
                audit_path=self.root / "audit.jsonl",
            )
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual("canonical_v2", resolve_workflow_read_mode(connection).value)


if __name__ == "__main__":
    unittest.main()
