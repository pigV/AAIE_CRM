import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.test_student_lifecycle_contract import NOW, build_post_020_fixture


def enable_education_streams(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            ALTER TABLE application_cycles
              ADD COLUMN education_stream TEXT NOT NULL DEFAULT 'school_education'
              CHECK(education_stream IN ('school_education','higher_education'));
            DROP INDEX uq_application_cycles_active_student;
            CREATE UNIQUE INDEX uq_application_cycles_active_student_stream
              ON application_cycles(student_id,education_stream)
              WHERE status='active' AND student_id IS NOT NULL;
            """
        )


class StudentLifecycleEducationStreamQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "lifecycle.db"
        build_post_020_fixture(self.database_path)
        enable_education_streams(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def service(self):
        from student_lifecycle.service import StudentLifecycleService

        return StudentLifecycleService(self.database_path, clock=lambda: NOW)

    def insert_initial(self, uid: str, stream: str, sequence: int) -> int:
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.execute(
                """INSERT INTO application_cycles(
                     cycle_uid,student_id,cycle_type,education_stream,sequence_no,
                     status,opened_at,created_by_user_id,created_at,updated_at
                   ) VALUES(?, 'S1', 'initial', ?, ?, 'active', ?, 1, ?, ?)""",
                (uid, stream, sequence, NOW, NOW, NOW),
            )
            return int(cursor.lastrowid)

    def test_single_active_initial_keeps_compatible_lookup(self):
        cycle_id = self.insert_initial("school-initial", "school_education", 1)

        result = self.service().get_active_lifecycle("S1")

        self.assertEqual(cycle_id, result.cycle_id)
        self.assertEqual("initial", result.lifecycle_type)
        self.assertEqual("school_education", result.education_stream)

    def test_dual_stream_active_lookup_without_context_fails_closed(self):
        from student_lifecycle.errors import LifecycleAmbiguousActiveError

        self.insert_initial("school-initial", "school_education", 1)
        self.insert_initial("higher-initial", "higher_education", 2)

        with self.assertRaises(LifecycleAmbiguousActiveError):
            self.service().get_active_lifecycle("S1")

    def test_explicit_education_stream_selects_exact_active_cycle(self):
        school_id = self.insert_initial("school-initial", "school_education", 1)
        higher_id = self.insert_initial("higher-initial", "higher_education", 2)

        school = self.service().get_active_lifecycle(
            "S1", education_stream="school_education"
        )
        higher = self.service().get_active_lifecycle(
            "S1", education_stream="higher_education"
        )

        self.assertEqual((school_id, "school_education"), (school.cycle_id, school.education_stream))
        self.assertEqual((higher_id, "higher_education"), (higher.cycle_id, higher.education_stream))

    def test_case_and_cycle_context_keep_exact_stream_identity(self):
        draft = self.service().create_lifecycle_draft(
            student_id="S1", lifecycle_type="transfer", actor_user_id=1,
            owner_user_id=1, idempotency_key="draft-transfer",
        )
        active = self.service().activate_lifecycle(
            case_id=draft.case_id, actor_user_id=1,
            idempotency_key="activate-transfer",
        )

        by_case = self.service().get_lifecycle_context(draft.case_id)
        by_cycle = self.service().get_lifecycle_context(cycle_id=active.cycle_id)

        self.assertEqual("school_education", by_case["education_stream"])
        self.assertEqual(by_case, by_cycle)

    def test_list_preserves_both_streams_with_stable_cycle_ids(self):
        school_id = self.insert_initial("school-initial", "school_education", 1)
        higher_id = self.insert_initial("higher-initial", "higher_education", 2)

        rows = self.service().list_student_lifecycles("S1")

        self.assertEqual(
            [
                (school_id, "school_education", "initial"),
                (higher_id, "higher_education", "initial"),
            ],
            [(row.cycle_id, row.education_stream, row.lifecycle_type) for row in rows],
        )


if __name__ == "__main__":
    unittest.main()
