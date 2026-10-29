import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools" / "workflow_production_operator.py"


class WorkflowProductionOperatorCliTests(unittest.TestCase):
    def test_help_lists_exact_approved_commands_and_no_bypass_flags(self):
        result = subprocess.run(
            [sys.executable, str(CLI), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        for command in (
            "status", "backfill-dry-run", "backfill-apply",
            "v2-activation-dry-run", "v2-activate", "v2-disable",
        ):
            self.assertIn(command, result.stdout)
        for forbidden in ("--force", "--skip-checks", "--ignore-fingerprint", "--formal-database"):
            self.assertNotIn(forbidden, result.stdout)

    def test_unknown_force_flag_is_rejected(self):
        result = subprocess.run(
            [sys.executable, str(CLI), "status", "--force"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertEqual(
            {"error": "invalid_arguments", "ok": False},
            __import__("json").loads(result.stderr),
        )

    def test_rejected_execution_has_stable_json_even_when_file_audit_is_unavailable(self):
        result = subprocess.run(
            [
                sys.executable, str(CLI), "status",
                "--database", "/missing.sqlite3",
                "--approved-candidate-commit", "a" * 40,
                "--audit-output", "/",
            ],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertEqual(
            {"error": "operator_audit_failed", "ok": False},
            __import__("json").loads(result.stderr),
        )


if __name__ == "__main__":
    unittest.main()
