from contextlib import closing
import json
import sqlite3

from application_cycle import create_student_cycle, close_cycle
from progression_case_errors import ProgressionCaseConflictError
from progression_case_service import ProgressionCaseService
from student_case_feature_flags import StudentCaseFeatureFlags
from tests import test_student_lifecycle_case_integration as case_integration
from tests.student_lifecycle_test_support import (
    activate_progression,
    activate_transfer,
)
from transfer_arrangement_service import TransferArrangementService
from transfer_case_errors import (
    TransferCaseActiveConflictError,
    TransferCaseIntegrityError,
)


class StudentLifecycleTransferIntegrationTests(
    case_integration.StudentLifecycleCaseIntegrationTests
):
    """Transfer integration contract built only from the clean Phase 2 API."""

    def setUp(self):
        super().setUp()
        self.arrangements = TransferArrangementService(self.database_path)
        with closing(sqlite3.connect(self.database_path)) as connection:
            for table in ("offers", "coe_oshc"):
                columns = {
                    row[1]
                    for row in connection.execute(
                        f"PRAGMA table_info('{table}')"
                    )
                }
                if "school_name" not in columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN school_name TEXT"
                    )
            connection.execute(
                "UPDATE offers SET school_name='Target School'"
            )
            connection.execute(
                "UPDATE coe_oshc SET school_name='Target School'"
            )
            connection.commit()

    @staticmethod
    def flags():
        return StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
            progression_write_enabled=True,
        )

    def edit_arguments(self):
        return {
            "actor_user_id": 1,
            "actor_permissions": {"student_cases.edit"},
            "flags": self.flags(),
        }

    def create_transfer_for(self, student_id, *, activate=False, key=None):
        draft = self.service.create_internal_transfer(
            student_id=student_id,
            actor_user_id=1,
            actor_permissions={"student_cases.create"},
            flags=self.flags(),
            owner_user_id=2,
            title="Transfer lifecycle integration",
            current_school_name_snapshot="Current School",
            current_school_id=10,
            current_city_key="melbourne",
            current_school_sector="public",
            initial_note=None,
        )
        if not activate:
            return draft
        return activate_transfer(self.service, draft, key=key)

    def create_progression_for(self, student_id, *, activate=False):
        service = ProgressionCaseService(self.database_path)
        draft = service.create_case(
            student_id=student_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.create"},
            flags={"read_enabled": True, "progression_write_enabled": True},
            progression_type="bachelor",
            priority="normal",
            current_school_id=10,
            current_school_name_snapshot="Current School",
            current_course_name="VCE",
            current_year_level="secondary",
            expected_graduation_date="2026-12-01",
            target_education_stage="Higher Education",
            target_course_type="bachelor",
            target_institution_id=20,
            target_institution_name_snapshot="Target University",
            target_start_date="2027-02-01",
            owner_user_id=2,
            notes=None,
        )
        return activate_progression(service, draft) if activate else draft

    def add_application(self, case_id):
        return self.service.add_transfer_school_application(
            case_id,
            school_id=20,
            school_name_snapshot="Target School",
            institution_group_id=None,
            city_key="melbourne",
            campus_key=None,
            school_sector="private",
            expected_start_date="2027-02-01",
            preference_order=None,
            application_status="preparing",
            notes=None,
            **self.edit_arguments(),
        )

    def insert_closed_initial_cycle(self, student_id="student-1"):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            initial = create_student_cycle(
                connection,
                student_id,
                "initial",
                "2026-01-01T00:00:00+00:00",
                1,
                cycle_uid=f"initial-{student_id}",
            )
            result = close_cycle(
                connection,
                initial.id,
                "completed",
                "2026-06-01T00:00:00+00:00",
                1,
            )
            connection.commit()
            return result

    def cycle_id_for(self, case_id):
        return self.lifecycle.get_lifecycle_context(case_id)[
            "application_cycle_id"
        ]

    def record_cycle(self, table, record_id):
        with closing(sqlite3.connect(self.database_path)) as connection:
            return connection.execute(
                f"SELECT application_cycle_id FROM {table} WHERE id=?",
                (record_id,),
            ).fetchone()[0]

    def test_formal_student_profiles_create_active_transfer_cycles(self):
        internal = self.create_transfer_for("student-1")
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO students(student_id) VALUES('external-profile')"
            )
            connection.commit()
        external = self.create_transfer_for("external-profile")
        for created in (internal, external):
            context = self.lifecycle.get_lifecycle_context(created.case_id)
            self.assertEqual(
                ("current", "transfer", "active"),
                (
                    context["context_status"],
                    context["lifecycle_type"],
                    context["cycle_status"],
                ),
            )

    def test_closed_initial_cycle_is_retained_with_transfer_sequence_two(self):
        initial = self.insert_closed_initial_cycle()
        transfer = self.create_transfer_for("student-1", activate=True)
        history = self.lifecycle.list_student_lifecycles("student-1")
        context = self.lifecycle.get_lifecycle_context(transfer.case_id)
        self.assertEqual(
            (initial.cycle_uid, "initial", "closed", 1),
            (
                history[0].cycle_uid,
                history[0].lifecycle_type,
                history[0].cycle_status,
                history[0].cycle_sequence_no,
            ),
        )
        self.assertEqual(
            ("transfer", "active", 2),
            (
                context["lifecycle_type"],
                context["cycle_status"],
                context["cycle_sequence_no"],
            ),
        )

    def test_second_transfer_is_rejected_at_create_time(self):
        first = self.create_transfer_for("student-1")
        with self.assertRaises(TransferCaseActiveConflictError):
            self.create_transfer_for("student-1")
        self.assertEqual(
            first.case_id,
            self.lifecycle.get_active_lifecycle("student-1").case_id,
        )

    def test_active_transfer_blocks_progression_create(self):
        transfer = self.create_transfer_for("student-1", activate=True)
        with self.assertRaises(ProgressionCaseConflictError):
            self.create_progression_for("student-1")
        self.assertEqual(
            transfer.case_id,
            self.lifecycle.get_active_lifecycle("student-1").case_id,
        )

    def test_active_progression_blocks_transfer_create(self):
        progression = self.create_progression_for("student-2", activate=True)
        with self.assertRaises(TransferCaseActiveConflictError):
            self.create_transfer_for("student-2")
        self.assertEqual(
            progression.case_id,
            self.lifecycle.get_active_lifecycle("student-2").case_id,
        )

    def test_duplicate_transfer_activation_is_idempotent(self):
        draft = self.create_transfer_for("student-1")
        key = "duplicate-transfer-activation"
        first = self.service.activate_transfer_case(
            draft.case_id, actor_user_id=1, idempotency_key=key
        )
        second = self.service.activate_transfer_case(
            draft.case_id, actor_user_id=1, idempotency_key=key
        )
        self.assertEqual(first, second)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT count(*) FROM application_cycles WHERE source_student_case_id=?",
                    (draft.case_id,),
                ).fetchone()[0],
            )

    def test_activation_receipt_replays_immutable_original_result(self):
        draft = self.create_transfer_for("student-1")
        key = "immutable-transfer-activation"
        first = self.lifecycle.activate_lifecycle(
            case_id=draft.case_id, actor_user_id=1, idempotency_key=key
        )
        self.lifecycle.cancel_lifecycle(
            case_id=draft.case_id,
            actor_user_id=1,
            reason="cancel after activation",
            idempotency_key="cancel-after-activation",
        )
        replay = self.lifecycle.activate_lifecycle(
            case_id=draft.case_id, actor_user_id=1, idempotency_key=key
        )
        self.assertEqual(first, replay)
        with closing(sqlite3.connect(self.database_path)) as connection:
            result = json.loads(
                connection.execute(
                    """SELECT result_json
                         FROM student_lifecycle_idempotency_receipts
                        WHERE operation='activate' AND idempotency_key=?""",
                    (key,),
                ).fetchone()[0]
            )
        self.assertEqual("active", result["cycle_status"])

    def test_case_and_cycle_contexts_are_identical(self):
        transfer = self.create_transfer_for("student-1", activate=True)
        by_case = self.lifecycle.get_lifecycle_context(transfer.case_id)
        by_cycle = self.lifecycle.get_lifecycle_context(
            cycle_id=by_case["application_cycle_id"]
        )
        self.assertEqual(by_case, by_cycle)

    def test_transfer_cancel_closes_case_and_cycle_together(self):
        transfer = self.create_transfer_for("student-1", activate=True)
        self.service.cancel_transfer_case(
            transfer.case_id,
            actor_user_id=1,
            reason="Student withdrew",
            idempotency_key="atomic-transfer-cancel",
        )
        context = self.lifecycle.get_lifecycle_context(transfer.case_id)
        self.assertEqual(
            ("cancelled", 0, "closed", "cancelled"),
            (
                context["case_status"],
                self.lifecycle.list_student_lifecycles("student-1")[-1].active_slot,
                context["cycle_status"],
                context["cycle_outcome"],
            ),
        )

    def test_transfer_cancel_replay_remains_closed(self):
        transfer = self.create_transfer_for("student-1", activate=True)
        arguments = {
            "actor_user_id": 1,
            "reason": "Student withdrew",
            "idempotency_key": "repeat-transfer-cancel",
        }
        first = self.service.cancel_transfer_case(transfer.case_id, **arguments)
        second = self.service.cancel_transfer_case(transfer.case_id, **arguments)
        self.assertEqual(first, second)
        self.assertEqual(
            "closed",
            self.lifecycle.get_lifecycle_context(transfer.case_id)[
                "cycle_status"
            ],
        )

    def test_get_active_lifecycle_returns_only_operational_transfer(self):
        self.insert_closed_initial_cycle()
        active = self.create_transfer_for("student-1", activate=True)
        result = self.lifecycle.get_active_lifecycle("student-1")
        self.assertEqual((active.case_id, "transfer", "active"), (
            result.case_id, result.lifecycle_type, result.cycle_status
        ))

    def test_list_retains_closed_cycles_and_one_current_active_cycle(self):
        self.insert_closed_initial_cycle()
        closed = self.create_transfer_for("student-1", activate=True)
        self.service.cancel_transfer_case(
            closed.case_id,
            actor_user_id=1,
            reason="close historical transfer",
            idempotency_key="close-historical-transfer",
        )
        active = self.create_transfer_for("student-1", activate=True)
        self.service.cancel_transfer_case(
            active.case_id,
            actor_user_id=1,
            reason="close second historical transfer",
            idempotency_key="close-second-historical-transfer",
        )
        progression = self.create_progression_for("student-1")
        rows = self.lifecycle.list_student_lifecycles("student-1")
        identities = {
            (row.lifecycle_type, row.case_status, row.cycle_status)
            for row in rows
        }
        self.assertIn(("initial", None, "closed"), identities)
        self.assertIn(("transfer", "cancelled", "closed"), identities)
        self.assertIn(("progression", "preparing", "active"), identities)
        self.assertEqual(progression.case_id, self.lifecycle.get_active_lifecycle(
            "student-1"
        ).case_id)

    def test_desktop_and_mobile_share_the_same_lifecycle_results(self):
        self.insert_closed_initial_cycle()
        self.create_transfer_for("student-1")
        desktop = self.lifecycle.list_student_lifecycles("student-1")
        mobile = self.lifecycle.list_student_lifecycles("student-1")
        self.assertEqual(desktop, mobile)

    def test_transfer_offer_and_coe_bind_to_active_transfer_cycle(self):
        transfer = self.create_transfer_for("student-1", activate=True)
        application = self.add_application(transfer.case_id)
        self.service.attach_transfer_offer(
            transfer.case_id,
            application.application_id,
            1000,
            **self.edit_arguments(),
        )
        self.service.attach_transfer_coe(
            transfer.case_id,
            application.application_id,
            1100,
            **self.edit_arguments(),
        )
        cycle_id = self.cycle_id_for(transfer.case_id)
        self.assertEqual(cycle_id, self.record_cycle("offers", 1000))
        self.assertEqual(cycle_id, self.record_cycle("coe_oshc", 1100))

    def test_updated_oshc_binds_but_carried_forward_keeps_initial_cycle(self):
        initial = self.insert_closed_initial_cycle()
        transfer = self.create_transfer_for("student-1", activate=True)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE coe_oshc SET application_cycle_id=? WHERE id=1300",
                (initial.id,),
            )
            connection.commit()
        self.arrangements.carry_forward_transfer_oshc(
            transfer.case_id, 1300, **self.edit_arguments()
        )
        self.arrangements.update_transfer_oshc(
            transfer.case_id, 1301, **self.edit_arguments()
        )
        self.assertEqual(initial.id, self.record_cycle("coe_oshc", 1300))
        self.assertEqual(
            self.cycle_id_for(transfer.case_id),
            self.record_cycle("coe_oshc", 1301),
        )

    def test_record_owned_by_another_cycle_fails_without_mutation(self):
        initial = self.insert_closed_initial_cycle()
        transfer = self.create_transfer_for("student-1", activate=True)
        application = self.add_application(transfer.case_id)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE offers SET application_cycle_id=? WHERE id=1000",
                (initial.id,),
            )
            connection.commit()
        with self.assertRaises(TransferCaseIntegrityError):
            self.service.attach_transfer_offer(
                transfer.case_id,
                application.application_id,
                1000,
                **self.edit_arguments(),
            )
        self.assertEqual(initial.id, self.record_cycle("offers", 1000))
        self.assertEqual(
            (),
            self.service.list_transfer_record_links(
                transfer.case_id, application.application_id, "offer"
            ),
        )

    def test_completed_transfer_keeps_offer_coe_and_oshc_cycle_ownership(self):
        transfer = self.create_transfer_for("student-1", activate=True)
        application = self.add_application(transfer.case_id)
        self.service.attach_transfer_offer(
            transfer.case_id,
            application.application_id,
            1000,
            **self.edit_arguments(),
        )
        self.service.attach_transfer_coe(
            transfer.case_id,
            application.application_id,
            1100,
            **self.edit_arguments(),
        )
        self.arrangements.update_transfer_oshc(
            transfer.case_id, 1301, **self.edit_arguments()
        )
        cycle_id = self.cycle_id_for(transfer.case_id)
        self.lifecycle.complete_operational_case(
            case_id=transfer.case_id,
            actor_user_id=1,
            idempotency_key="complete-transfer-ownership",
        )
        self.lifecycle.close_lifecycle(
            case_id=transfer.case_id,
            outcome="completed",
            actor_user_id=1,
            idempotency_key="close-transfer-ownership",
        )
        self.assertEqual(
            (cycle_id, cycle_id, cycle_id),
            (
                self.record_cycle("offers", 1000),
                self.record_cycle("coe_oshc", 1100),
                self.record_cycle("coe_oshc", 1301),
            ),
        )

    def test_submitted_transfer_application_maps_by_exact_school_id_idempotently(self):
        transfer = self.create_transfer_for("student-1", activate=True)
        application = self.add_application(transfer.case_id)
        self.service.change_transfer_school_application_status(
            transfer.case_id,
            application.application_id,
            "submitted",
            **self.edit_arguments(),
        )
        repeated = self.service.change_transfer_school_application_status(
            transfer.case_id,
            application.application_id,
            "submitted",
            **self.edit_arguments(),
        )
        self.assertEqual("submitted", repeated.application_status)
        cycle_id = self.cycle_id_for(transfer.case_id)
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """SELECT student_id,school_id,status
                     FROM private_school_applications
                    WHERE application_cycle_id=?""",
                (cycle_id,),
            ).fetchall()
        self.assertEqual([("student-1", 20, "submitted")], [tuple(row) for row in rows])

    def test_transfer_offer_creates_one_canonical_event_for_exact_application(self):
        transfer = self.create_transfer_for("student-1", activate=True)
        application = self.add_application(transfer.case_id)
        first = self.service.attach_transfer_offer(
            transfer.case_id, application.application_id, 1000,
            **self.edit_arguments(),
        )
        repeated = self.service.attach_transfer_offer(
            transfer.case_id, application.application_id, 1000,
            **self.edit_arguments(),
        )
        self.assertEqual(first, repeated)
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """SELECT event.application_cycle_id,event.legacy_offer_id,
                          application.school_id
                     FROM offer_received_events AS event
                     JOIN private_school_applications AS application
                       ON application.id=event.private_application_id"""
            ).fetchall()
        self.assertEqual(
            [(self.cycle_id_for(transfer.case_id), 1000, 20)],
            [tuple(row) for row in rows],
        )

    def test_transfer_acceptance_is_cycle_scoped_and_query_exposes_context(self):
        transfer = self.create_transfer_for("student-1", activate=True)
        application = self.add_application(transfer.case_id)
        self.service.set_final_transfer_school_application(
            transfer.case_id, application.application_id,
            **self.edit_arguments(),
        )
        self.service.attach_transfer_offer(
            transfer.case_id, application.application_id, 1000,
            **self.edit_arguments(),
        )
        for status in ("submitted", "waiting_offer", "offer_received", "accepted"):
            self.service.change_transfer_school_application_status(
                transfer.case_id, application.application_id, status,
                **self.edit_arguments(),
            )
        context = self.lifecycle.get_lifecycle_context(transfer.case_id)
        self.assertEqual(1, len(context["target_applications"]))
        target = context["target_applications"][0]
        self.assertEqual(
            (application.application_id, "private", 20),
            (
                target["transfer_application_id"],
                target["canonical_application_kind"],
                target["school_id"],
            ),
        )
        self.assertEqual(target, context["selected_application_context"])
        with closing(sqlite3.connect(self.database_path)) as connection:
            acceptance = connection.execute(
                """SELECT application_cycle_id,student_id
                     FROM cycle_final_acceptances"""
            ).fetchone()
        self.assertEqual(
            (self.cycle_id_for(transfer.case_id), "student-1"),
            tuple(acceptance),
        )

    def test_non_final_transfer_application_cannot_become_cycle_acceptance(self):
        transfer = self.create_transfer_for("student-1", activate=True)
        application = self.add_application(transfer.case_id)
        self.service.attach_transfer_offer(
            transfer.case_id, application.application_id, 1000,
            **self.edit_arguments(),
        )
        for status in ("submitted", "waiting_offer", "offer_received"):
            self.service.change_transfer_school_application_status(
                transfer.case_id, application.application_id, status,
                **self.edit_arguments(),
            )
        with self.assertRaises(TransferCaseIntegrityError):
            self.service.change_transfer_school_application_status(
                transfer.case_id, application.application_id, "accepted",
                **self.edit_arguments(),
            )
        with closing(sqlite3.connect(self.database_path)) as connection:
            status = connection.execute(
                """SELECT application_status
                     FROM transfer_school_applications WHERE id=?""",
                (application.application_id,),
            ).fetchone()[0]
            acceptance_count = connection.execute(
                "SELECT count(*) FROM cycle_final_acceptances"
            ).fetchone()[0]
        self.assertEqual("offer_received", status)
        self.assertEqual(0, acceptance_count)

    def test_transfer_document_context_preserves_central_document_identity(self):
        transfer = self.create_transfer_for("student-1", activate=True)
        material = self.service.ensure_transfer_material_template(
            transfer.case_id,
            request_id="r5-delta-document-template",
            **self.edit_arguments(),
        )[0]
        with closing(sqlite3.connect(self.database_path)) as connection:
            before = tuple(connection.execute(
                "SELECT * FROM student_documents WHERE id=1600"
            ).fetchone())
        self.service.attach_transfer_material_document(
            transfer.case_id,
            material.material_id,
            1600,
            **self.edit_arguments(),
        )
        context = self.lifecycle.get_lifecycle_context(transfer.case_id)
        self.assertEqual(1600, context["document_contexts"][0][
            "student_document_id"
        ])
        with closing(sqlite3.connect(self.database_path)) as connection:
            after = tuple(connection.execute(
                "SELECT * FROM student_documents WHERE id=1600"
            ).fetchone())
        self.assertEqual(before, after)


if __name__ == "__main__":
    import unittest

    unittest.main()
