import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.test_workflow_application_cycles_migration import (
    apply_workflow_migration,
    build_legacy_fixture,
)


APPROVED_SHA256 = "a590d6e3b47d0a706ec667d3e9227ed4e099cc19b24300f322d23765f62ec84c"
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "config" / "workflow" / "frozen-human-decisions-v1.json"


class FrozenHumanDecisionTests(unittest.TestCase):
    def test_locked_fixture_is_exactly_the_approved_25_decisions(self):
        from workflow_human_decisions import load_frozen_decisions

        decisions = load_frozen_decisions(FIXTURE)

        self.assertEqual(APPROVED_SHA256, hashlib.sha256(FIXTURE.read_bytes()).hexdigest())
        self.assertEqual("workflow-human-decisions-v1", decisions.version)
        self.assertEqual(25, len(decisions.decisions))
        self.assertEqual(tuple(range(1, 26)), tuple(item["number"] for item in decisions.decisions))

    def test_changed_execution_input_is_rejected(self):
        from workflow_human_decisions import FrozenDecisionInputError, load_frozen_decisions

        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "changed.json"
            payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
            payload["decisions"][0]["choice"] = "A"
            changed.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(FrozenDecisionInputError):
                load_frozen_decisions(changed)

    def test_preview_is_read_only_and_no_write_dispositions_create_nothing(self):
        from workflow_human_decisions import load_frozen_decisions, preview_human_decisions

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.db"
            conn = build_legacy_fixture(path)
            apply_workflow_migration(conn)
            conn.commit()
            before = path.read_bytes()

            plan = preview_human_decisions(conn, load_frozen_decisions(FIXTURE))

            self.assertEqual(before, path.read_bytes())
            self.assertEqual(25, plan.decision_count)
            self.assertEqual(15, plan.applying_decisions)
            self.assertGreaterEqual(plan.projected_records, 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
