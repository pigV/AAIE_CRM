import json
import sqlite3
import unittest

from final_school_requirements import load_optional_steps, outstanding_steps


class FinalSchoolRequirementsTests(unittest.TestCase):
    def test_latest_decision_applies_to_one_acceptance_and_one_step(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE audit_logs(
            id INTEGER PRIMARY KEY, module_name TEXT, action_type TEXT,
            record_id TEXT, old_value TEXT, new_value TEXT, status TEXT)""")
        conn.executemany("INSERT INTO audit_logs VALUES(?, 'offers', ?, ?, ?, ?, 'success')", [
                (*record[:-1], json.dumps({"decision": record[-1], "reason": "school confirmed"}))
            for record in (
                (1, "final_school_step_requirement", "7", "payment", "optional"),
                (2, "final_school_step_requirement", "7", "signed", "optional"),
                (3, "final_school_step_requirement", "7", "signed", "required"),
                (4, "final_school_step_requirement", "8", "return", "optional"),
            )
        ])
        decisions = load_optional_steps(conn, (7, 8, 9))
        self.assertEqual(decisions, {7: {"payment"}, 8: {"return"}})
        self.assertEqual(outstanding_steps(False, False, False, decisions[7]), ("签署", "回传"))
        self.assertEqual(outstanding_steps(True, True, False, decisions[8]), ())
        self.assertEqual(outstanding_steps(True, True, False, decisions.get(9, ())), ("回传",))
