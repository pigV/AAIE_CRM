import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class WorkflowProductionOperatorRehearsalTests(unittest.TestCase):
    def test_confirmations_bind_to_business_state_fingerprint(self):
        from tools import rehearse_workflow_production_operator as rehearsal

        context = SimpleNamespace(
            database_sha256="a" * 64,
            state_fingerprint="b" * 64,
        )

        target, operation = rehearsal._confirmations("backfill-apply", context)

        self.assertEqual("CONFIRM_PRODUCTION_TARGET_bbbbbbbbbbbb", target)
        self.assertEqual("APPLY_PRODUCTION_BACKFILL_bbbbbbbbbbbb", operation)

    def test_rehearsal_refuses_compiled_formal_database_and_existing_target(self):
        from tools import rehearse_workflow_production_operator as rehearsal
        from workflow_production_operator import OperatorGateError

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.sqlite3"
            target = root / "target.sqlite3"
            source.write_bytes(b"isolated-fixture")
            with mock.patch.object(rehearsal.operator, "FORMAL_DATABASE_PATH", source.resolve()):
                with self.assertRaisesRegex(OperatorGateError, "production_database_forbidden_in_rehearsal"):
                    rehearsal.run_rehearsal(
                        source, target, candidate_commit="a" * 40,
                        expected_metrics={}, audit_path=root / "audit.jsonl",
                        today=__import__("datetime").date(2026, 8, 11),
                    )
            target.write_bytes(b"do-not-overwrite")
            with self.assertRaisesRegex(OperatorGateError, "rehearsal_target_must_not_exist"):
                rehearsal.run_rehearsal(
                    source, target, candidate_commit="a" * 40,
                    expected_metrics={}, audit_path=root / "audit.jsonl",
                    today=__import__("datetime").date(2026, 8, 11),
                )
            self.assertEqual(b"do-not-overwrite", target.read_bytes())


if __name__ == "__main__":
    unittest.main()
