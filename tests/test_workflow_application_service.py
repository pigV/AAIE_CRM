import tempfile
import unittest
from pathlib import Path

from application_cycle import close_cycle, create_student_cycle
from tests.test_workflow_application_cycles_migration import (
    NOW,
    apply_workflow_migration,
    build_legacy_fixture,
)
from workflow_application_service import (
    activate_public_plan,
    create_private_application,
    create_public_group_from_confirmation,
    record_school_result,
    submit_private_application,
    submit_public_group,
)


LATER = "2026-08-12T10:00:00+10:00"


class WorkflowApplicationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "applications.db"
        self.conn = build_legacy_fixture(self.database_path)
        self.conn.row_factory = __import__("sqlite3").Row
        apply_workflow_migration(self.conn)
        self.cycle = create_student_cycle(
            self.conn,
            "S1",
            "initial",
            NOW,
            1,
            cycle_uid="cycle-applications",
        )
        self.conn.execute(
            """
            UPDATE school_plan_reports
            SET application_cycle_id=?,cycle_is_current=1
            WHERE id=20
            """,
            (self.cycle.id,),
        )

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def add_confirmation(self, confirmation_id, report_id, sector, school_rows):
        self.conn.execute(
            """
            INSERT INTO school_plan_report_confirmations(
              id,report_id,student_id,confirmed_at
            ) VALUES(?,?, 'S1', ?)
            """,
            (confirmation_id, report_id, NOW),
        )
        for confirmed_id, school_name, order in school_rows:
            self.conn.execute(
                """
                INSERT INTO school_plan_confirmed_schools(
                  id,confirmation_id,report_id,student_id,school_name_snapshot,
                  city_id,plan_sector,submission_status,result_status
                ) VALUES(?,?,?,'S1',?,1,?,'not_submitted','pending')
                """,
                (confirmed_id, confirmation_id, report_id, school_name, sector),
            )

    def test_public_preferences_share_one_application_group(self):
        self.add_confirmation(
            100,
            20,
            "public",
            ((101, "Public A", 1), (102, "Public B", 2), (103, "Public C", 3)),
        )

        group = create_public_group_from_confirmation(
            self.conn, self.cycle.id, 100, actor_user_id=1, occurred_at=NOW
        )
        submitted = submit_public_group(
            self.conn, group.id, actor_user_id=1, occurred_at=LATER
        )

        self.assertEqual("submitted", submitted.status)
        self.assertEqual(
            3,
            self.conn.execute(
                "SELECT count(*) FROM public_application_preferences WHERE public_application_group_id=?",
                (group.id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            {"submitted"},
            {
                row[0]
                for row in self.conn.execute(
                    "SELECT application_status FROM public_application_preferences WHERE public_application_group_id=?",
                    (group.id,),
                ).fetchall()
            },
        )

    def test_private_schools_create_independent_applications_and_results(self):
        self.conn.execute(
            """
            INSERT INTO school_plan_reports(
              id,student_id,city_id,plan_sector,status,report_version,is_current,
              application_cycle_id,cycle_is_current,updated_at
            ) VALUES(21,'S1',1,'private','active',1,1,?,1,?)
            """,
            (self.cycle.id, NOW),
        )
        self.add_confirmation(
            110,
            21,
            "private",
            ((111, "Private A", 1), (112, "Private B", 2)),
        )

        first = create_private_application(
            self.conn, self.cycle.id, 111, actor_user_id=1, occurred_at=NOW
        )
        second = create_private_application(
            self.conn, self.cycle.id, 112, actor_user_id=1, occurred_at=NOW
        )
        submit_private_application(
            self.conn, first.id, actor_user_id=1, occurred_at=LATER
        )
        record_school_result(
            self.conn,
            source_kind="private",
            source_id=first.id,
            result_status="offer_received",
            occurred_at=LATER,
            result_note=None,
        )

        self.assertNotEqual(first.id, second.id)
        statuses = {
            row[0]: row[1]
            for row in self.conn.execute(
                "SELECT school_name_snapshot,status FROM private_school_applications"
            ).fetchall()
        }
        self.assertEqual("offer_received", statuses["Private A"])
        self.assertEqual("not_submitted", statuses["Private B"])

    def test_public_plan_supersedes_only_same_cycle_and_city(self):
        close_cycle(self.conn, self.cycle.id, "granted", LATER, 1)
        self.conn.execute(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(70,'S1','transfer')"
        )
        current_cycle = create_student_cycle(
            self.conn,
            "S1",
            "transfer",
            "2028-01-10T09:00:00+11:00",
            1,
            source_student_case_id=70,
            cycle_uid="cycle-transfer-plan",
        )
        self.conn.execute(
            "UPDATE school_plan_reports SET is_current=0 WHERE id=20"
        )
        self.conn.execute(
            """
            INSERT INTO school_plan_reports(
              id,student_id,city_id,plan_sector,status,report_version,is_current,
              application_cycle_id,cycle_is_current,updated_at
            ) VALUES
              (21,'S1',1,'public','generated',1,0,?,0,?),
              (22,'S1',1,'public','generated',2,0,?,0,?),
              (23,'S1',2,'public','generated',1,0,?,0,?)
            """,
            (
                current_cycle.id,
                NOW,
                current_cycle.id,
                NOW,
                current_cycle.id,
                NOW,
            ),
        )

        activate_public_plan(self.conn, current_cycle.id, 21, 1, NOW)
        activate_public_plan(self.conn, current_cycle.id, 23, 1, NOW)
        result = activate_public_plan(self.conn, current_cycle.id, 22, 1, LATER)

        rows = {
            row[0]: tuple(row[1:])
            for row in self.conn.execute(
                """
                SELECT id,status,is_current,cycle_is_current,superseded_by_report_id
                FROM school_plan_reports ORDER BY id
                """
            ).fetchall()
        }
        self.assertEqual(("active", 0, 1, None), rows[20])
        self.assertEqual(("superseded", 0, 0, 22), rows[21])
        self.assertEqual(("active", 1, 1, None), rows[22])
        self.assertEqual(("active", 1, 1, None), rows[23])
        self.assertEqual((21,), result.superseded_report_ids)


if __name__ == "__main__":
    unittest.main()
