from contextlib import closing
import sqlite3
import tempfile
from pathlib import Path
import unittest

import progression_case_service
from progression_case_errors import ProgressionCaseConflictError
from student_lifecycle.progression_contract_gate import (
    HIGHER_EDUCATION,
    SCHOOL_PROGRESSION,
)
from tests import test_student_case_phase2_transfer as transfer_tests


class ProgressionContractServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "progression-contract-service.db"
        helper = transfer_tests.TransferPhase2MigrationTests()
        helper.connect = transfer_tests.TransferPhase2MigrationTests.connect
        helper._create_legacy_fixture(self.database_path)
        transfer_tests.apply_all_test_migrations_with_rebuild_backup(
            self.database_path,
            self.root,
            label="progression-contract-service",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                UPDATE contracts
                   SET contract_type='LocalStudentApplicationContract',
                       contract_status='FullyExecuted';
                INSERT INTO contracts(
                  id,student_id,contract_type,contract_status,deleted_at,voided_at
                ) VALUES
                  (300,'student-1','LocalStudentApplicationContract','Draft',NULL,NULL),
                  (301,'student-1','LocalStudentApplicationContract','ParentSigned',NULL,NULL),
                  (302,'student-1','LocalStudentApplicationContract','FullyExecuted',NULL,NULL),
                  (303,'student-1','NewInternationalStudentContract','FullyExecuted',NULL,NULL),
                  (304,'student-1','TransferApplicationContract','FullyExecuted',NULL,NULL),
                  (305,'student-2','LocalStudentApplicationContract','FullyExecuted',NULL,NULL),
                  (306,'student-1','LocalStudentApplicationContract','FullyExecuted',NULL,NULL);
                """
            )
        self.service = progression_case_service.ProgressionCaseService(
            self.database_path
        )

    @staticmethod
    def flags():
        return {"read_enabled": True, "progression_write_enabled": True}

    def create(self, **overrides):
        values = {
            "student_id": "student-1",
            "actor_user_id": 1,
            "actor_permissions": {"progression_cases.create"},
            "flags": self.flags(),
            "business_type": HIGHER_EDUCATION,
            "source_contract_id": None,
            "progression_type": "bachelor",
            "priority": "normal",
            "current_school_id": 10,
            "current_school_name_snapshot": "Current School",
            "current_course_name": "VCE",
            "current_year_level": "secondary",
            "expected_graduation_date": "2026-12-01",
            "target_education_stage": "Higher Education",
            "target_course_type": "bachelor",
            "target_institution_id": 20,
            "target_institution_name_snapshot": "Target University",
            "target_start_date": "2027-02-01",
            "owner_user_id": 2,
            "notes": None,
        }
        values.update(overrides)
        return self.service.create_case(**values)

    def school_case(self, contract_id=302, **overrides):
        return self.create(
            business_type=SCHOOL_PROGRESSION,
            source_contract_id=contract_id,
            progression_type="other",
            target_education_stage="K-12 School Progression",
            target_course_type="secondary",
            **overrides,
        )

    def test_he_creation_starts_active_lifecycle_without_contract(self):
        active = self.create()
        self.assertEqual(HIGHER_EDUCATION, active.business_type)
        self.assertIsNone(active.source_contract_id)
        self.assertEqual("preparing", active.status)
        with closing(sqlite3.connect(self.database_path)) as connection:
            cycle = connection.execute(
                """SELECT status FROM application_cycles
                     WHERE source_student_case_id=?""",
                (active.case_id,),
            ).fetchone()
        self.assertEqual(("active",), cycle)

    def test_school_case_persists_exact_contract_ownership(self):
        draft = self.school_case()
        self.assertEqual(SCHOOL_PROGRESSION, draft.business_type)
        self.assertEqual(302, draft.source_contract_id)
        with closing(sqlite3.connect(self.database_path)) as connection:
            ownership = connection.execute(
                "SELECT business_type,source_contract_id FROM progression_cases WHERE case_id=?",
                (draft.case_id,),
            ).fetchone()
        self.assertEqual((SCHOOL_PROGRESSION, 302), ownership)

    def test_school_creation_starts_lifecycle_and_leaves_invalid_contract_unbound(self):
        active = self.school_case(None)
        self.assertEqual("preparing", active.status)
        self.assertIsNone(active.source_contract_id)
        with closing(sqlite3.connect(self.database_path)) as connection:
            cycle = connection.execute(
                """SELECT status FROM application_cycles
                     WHERE source_student_case_id=?""",
                (active.case_id,),
            ).fetchone()
        self.assertEqual(("active",), cycle)

    def test_other_unclassified_progression_fails_closed(self):
        with self.assertRaises(ProgressionCaseConflictError):
            self.create(
                business_type=None,
                progression_type="other",
                target_course_type="other",
            )

    def test_business_type_and_pathway_type_cannot_be_cross_classified(self):
        with self.assertRaises(ProgressionCaseConflictError):
            self.create(
                business_type=HIGHER_EDUCATION,
                progression_type="other",
                target_course_type="other",
            )
        with self.assertRaises(ProgressionCaseConflictError):
            self.create(
                business_type=SCHOOL_PROGRESSION,
                source_contract_id=302,
                progression_type="bachelor",
            )

    def test_creation_callback_failure_rolls_back_case_and_ownership(self):
        before = self._case_count()

        def fail(_connection, _case_id):
            raise RuntimeError("rollback probe")

        with self.assertRaisesRegex(RuntimeError, "rollback probe"):
            self.school_case(before_commit=fail)
        self.assertEqual(before, self._case_count())
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM progression_cases WHERE source_contract_id=302"
                ).fetchone()[0],
            )

    def test_association_is_idempotent_and_rejects_rebinding(self):
        draft = self.school_case()
        first = self.service.ensure_contract_ownership(
            draft.case_id,
            business_type=SCHOOL_PROGRESSION,
            source_contract_id=302,
            actor_user_id=1,
        )
        second = self.service.ensure_contract_ownership(
            draft.case_id,
            business_type=SCHOOL_PROGRESSION,
            source_contract_id=302,
            actor_user_id=1,
        )
        self.assertEqual(first, second)
        with self.assertRaises(ProgressionCaseConflictError):
            self.service.ensure_contract_ownership(
                draft.case_id,
                business_type=SCHOOL_PROGRESSION,
                source_contract_id=306,
                actor_user_id=1,
            )

    def test_compat_activation_does_not_recreate_or_revalidate_active_lifecycle(self):
        active = self.school_case()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE contracts SET contract_status='ParentSigned' WHERE id=302"
            )
            connection.commit()
        replay = self.service.activate_case(
            active.case_id,
            actor_user_id=1,
            idempotency_key="active-compatibility-replay",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            case_status = connection.execute(
                "SELECT status FROM student_cases WHERE id=?", (active.case_id,)
            ).fetchone()[0]
            cycle_count = connection.execute(
                "SELECT count(*) FROM application_cycles WHERE source_student_case_id=?",
                (active.case_id,),
            ).fetchone()[0]
        self.assertEqual(active.case_id, replay.case_id)
        self.assertEqual("preparing", case_status)
        self.assertEqual(1, cycle_count)

    def _case_count(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            return connection.execute(
                "SELECT count(*) FROM student_cases WHERE case_type='progression'"
            ).fetchone()[0]


if __name__ == "__main__":
    unittest.main()
