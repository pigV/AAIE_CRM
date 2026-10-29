from contextlib import closing
import json
import sqlite3

from application_cycle import create_student_cycle, close_cycle
from progression_case_errors import (
    ProgressionCaseConflictError,
    ProgressionCaseIntegrityError,
)
from progression_case_service import ProgressionCaseService
from student_case_feature_flags import StudentCaseFeatureFlags
from student_lifecycle.errors import LifecycleActiveConflictError
from tests import test_student_lifecycle_case_integration as case_integration
from tests.student_lifecycle_test_support import (
    activate_progression,
    activate_transfer,
)
from transfer_case_errors import TransferCaseActiveConflictError


class StudentLifecycleProgressionIntegrationTests(
    case_integration.StudentLifecycleCaseIntegrationTests
):
    """Progression integration contract built from the clean Phase 3 API."""

    @staticmethod
    def flags():
        return StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
            progression_write_enabled=True,
        )

    def create_progression_for(self, student_id="student-1", *, activate=False,
                               key=None):
        draft = self.progression_service.create_case(
            student_id=student_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.create"},
            flags=self.flags(),
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
        if not activate:
            return draft
        return activate_progression(
            self.progression_service, draft,
            key=key or f"activate-progression-{draft.case_id}",
        )

    def create_transfer_for(self, student_id="student-1", *, activate=False):
        draft = self.service.create_internal_transfer(
            student_id=student_id,
            actor_user_id=1,
            actor_permissions={"student_cases.create"},
            flags=self.flags(),
            owner_user_id=2,
            title="Transfer before progression",
            current_school_name_snapshot="Current School",
            current_school_id=10,
            current_city_key="melbourne",
            current_school_sector="public",
            initial_note=None,
        )
        return activate_transfer(self.service, draft) if activate else draft

    def insert_initial_cycle(self, student_id="student-1", *, closed=True):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            cycle = create_student_cycle(
                connection, student_id, "initial",
                "2026-01-01T00:00:00+00:00", 1,
                cycle_uid=f"initial-{student_id}",
            )
            if closed:
                cycle = close_cycle(
                    connection, cycle.id, "completed",
                    "2026-06-01T00:00:00+00:00", 1,
                )
            connection.commit()
            return cycle

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

    def add_offer(self, case_id, record_id=1000):
        target = self.progression_service.get_case_data(case_id)[
            "target_applications"
        ][0]
        return self.progression_service.add_offer(
            case_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.offers"},
            flags=self.flags(),
            stage_id=None,
            existing_offer_id=record_id,
            offer_type="unconditional",
            institution_id=20,
            institution_name_snapshot="Target University",
            course_name="Bachelor of Business",
            offer_date="2026-08-01",
            expiry_date=None,
            conditions_text=None,
            student_document_id=1600,
            notes=None,
            target_application_id=target["id"],
        )

    def link_coe(self, case_id, record_id=1100):
        application = self.progression_service.get_case_data(case_id)[
            "target_applications"
        ][0]
        self.progression_service.select_final_application(
            case_id,
            application["id"],
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.flags(),
        )
        return self.progression_service.link_progression_document(
            case_id,
            document_role="coe",
            student_document_id=1601,
            target_application_id=application["id"],
            status="confirmed",
            notes=None,
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.flags(),
            lifecycle_record_id=record_id,
        )

    def set_oshc(self, case_id, record_id=1301):
        return self.progression_service.set_progression_arrangement(
            case_id,
            arrangement_type="oshc",
            status="confirmed",
            reason=None,
            notes=None,
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.flags(),
            lifecycle_record_id=record_id,
        )

    def close_progression(self, case_id, *, outcome="withdrawn", key=None):
        return self.progression_service.close_case(
            case_id,
            outcome=outcome,
            reason="Lifecycle integration close",
            actor_user_id=1,
            actor_permissions={"progression_cases.close"},
            flags=self.flags(),
            idempotency_key=key or f"close-progression-{case_id}-{outcome}",
        )

    def test_initial_history_and_active_progression_coexist(self):
        initial = self.insert_initial_cycle()
        created = self.create_progression_for()
        context = self.lifecycle.get_lifecycle_context(created.case_id)
        self.assertEqual(("current", "active"), (
            context["context_status"], context["cycle_status"],
        ))
        self.assertEqual("closed", self.lifecycle.get_lifecycle_context(
            cycle_id=initial.id
        )["cycle_status"])

    def test_transfer_history_and_active_progression_coexist(self):
        transfer = self.create_transfer_for(activate=True)
        self.service.cancel_transfer_case(
            transfer.case_id, actor_user_id=1, reason="Transfer finished",
            idempotency_key="finish-transfer-before-progression",
        )
        created = self.create_progression_for()
        self.assertEqual("closed", self.lifecycle.get_lifecycle_context(
            transfer.case_id
        )["cycle_status"])
        self.assertEqual("current", self.lifecycle.get_lifecycle_context(
            created.case_id
        )["context_status"])

    def test_external_profile_formal_service_creates_active_progression(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO students(student_id) VALUES('external-profile')"
            )
            connection.commit()
        created = self.create_progression_for("external-profile")
        self.assertEqual("external-profile", created.student_id)
        context = self.lifecycle.get_lifecycle_context(created.case_id)
        self.assertEqual(("progression", "active"), (
            context["lifecycle_type"], context["cycle_status"],
        ))

    def test_progression_activation_creates_progression_cycle(self):
        draft = self.create_progression_for()
        active = activate_progression(self.progression_service, draft)
        context = self.lifecycle.get_lifecycle_context(active.case_id)
        self.assertEqual(("progression", "active", active.case_id), (
            context["lifecycle_type"], context["cycle_status"],
            context["case_id"],
        ))

    def test_duplicate_activation_is_idempotent(self):
        draft = self.create_progression_for()
        key = "duplicate-progression-activation"
        first = self.progression_service.activate_case(
            draft.case_id, actor_user_id=1, idempotency_key=key
        )
        second = self.progression_service.activate_case(
            draft.case_id, actor_user_id=1, idempotency_key=key
        )
        self.assertEqual(first, second)
        with closing(sqlite3.connect(self.database_path)) as connection:
            count = connection.execute(
                "SELECT count(*) FROM application_cycles WHERE source_student_case_id=?",
                (draft.case_id,),
            ).fetchone()[0]
        self.assertEqual(1, count)

    def test_activation_receipt_replays_immutable_original_result(self):
        draft = self.create_progression_for()
        key = "immutable-progression-activation"
        first = self.lifecycle.activate_lifecycle(
            case_id=draft.case_id, actor_user_id=1, idempotency_key=key
        )
        self.lifecycle.cancel_lifecycle(
            case_id=draft.case_id, actor_user_id=1,
            reason="close after activation", idempotency_key="cancel-after",
        )
        replay = self.lifecycle.activate_lifecycle(
            case_id=draft.case_id, actor_user_id=1, idempotency_key=key
        )
        self.assertEqual(first, replay)
        with closing(sqlite3.connect(self.database_path)) as connection:
            result = json.loads(connection.execute(
                "SELECT result_json FROM student_lifecycle_idempotency_receipts WHERE operation='activate' AND idempotency_key=?",
                (key,),
            ).fetchone()[0])
        self.assertEqual("active", result["cycle_status"])

    def test_second_progression_is_rejected_at_create_time(self):
        first = self.create_progression_for()
        with self.assertRaises(ProgressionCaseConflictError):
            self.create_progression_for()
        self.assertEqual(first.case_id, self.lifecycle.get_active_lifecycle(
            "student-1"
        ).case_id)

    def test_school_initial_and_he_progression_can_be_active_in_parallel(self):
        initial = self.insert_initial_cycle(closed=False)
        progression = self.create_progression_for()
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """SELECT id,cycle_type,education_stream,status
                   FROM application_cycles WHERE student_id='student-1'
                   ORDER BY id"""
            ).fetchall()
        self.assertEqual(
            [
                (initial.id, "initial", "school_education", "active"),
                (
                    self.cycle_id_for(progression.case_id),
                    "progression",
                    "higher_education",
                    "active",
                ),
            ],
            [tuple(row) for row in rows],
        )

    def test_active_transfer_blocks_progression_create(self):
        self.create_transfer_for(activate=True)
        with self.assertRaises(ProgressionCaseConflictError):
            self.create_progression_for()

    def test_active_progression_blocks_transfer_create(self):
        active = self.create_progression_for(activate=True)
        with self.assertRaises(TransferCaseActiveConflictError):
            self.create_transfer_for()
        self.assertEqual(active.case_id, self.lifecycle.get_active_lifecycle(
            "student-1"
        ).case_id)

    def test_closed_initial_is_retained_and_progression_sequence_is_two(self):
        initial = self.insert_initial_cycle()
        active = self.create_progression_for(activate=True)
        context = self.lifecycle.get_lifecycle_context(active.case_id)
        self.assertEqual((2, "progression"), (
            context["cycle_sequence_no"], context["lifecycle_type"],
        ))
        self.assertEqual("closed", self.lifecycle.get_lifecycle_context(
            cycle_id=initial.id
        )["cycle_status"])
        self.assertNotEqual(initial.cycle_uid, context["cycle_uid"])

    def test_case_and_cycle_context_queries_are_identical(self):
        active = self.create_progression_for(activate=True)
        by_case = self.lifecycle.get_lifecycle_context(active.case_id)
        by_cycle = self.lifecycle.get_lifecycle_context(
            cycle_id=by_case["application_cycle_id"]
        )
        self.assertEqual(by_case, by_cycle)

    def test_active_case_prevents_early_canonical_cycle_close(self):
        active = self.create_progression_for(activate=True)
        with self.assertRaises(LifecycleActiveConflictError):
            self.lifecycle.close_lifecycle(
                case_id=active.case_id, outcome="completed", actor_user_id=1,
                idempotency_key="early-progression-close",
            )

    def test_progression_withdrawal_closes_case_and_cycle_atomically(self):
        active = self.create_progression_for(activate=True)
        self.close_progression(active.case_id)
        context = self.lifecycle.get_lifecycle_context(active.case_id)
        self.assertEqual(("cancelled", "closed", "withdrawn"), (
            context["case_status"], context["cycle_status"],
            context["cycle_outcome"],
        ))

    def test_progression_completion_closes_case_and_cycle_atomically(self):
        active = self.create_progression_for(activate=True)
        for stage in (
            "materials_collection", "application_preparation",
            "application_submitted", "offer_received", "offer_accepted",
            "enrolment_preparation", "completed",
        ):
            self.progression_service.change_stage(
                active.case_id, stage, actor_user_id=1,
                actor_permissions={"progression_cases.stage"},
                flags=self.flags(), note=f"advance to {stage}",
            )
        self.lifecycle.complete_operational_case(
            case_id=active.case_id,
            actor_user_id=1,
            idempotency_key=f"complete-progression-{active.case_id}",
        )
        self.close_progression(active.case_id, outcome="completed")
        context = self.lifecycle.get_lifecycle_context(active.case_id)
        self.assertEqual(("completed", "closed", "completed"), (
            context["case_status"], context["cycle_status"],
            context["cycle_outcome"],
        ))

    def test_progression_close_replay_is_idempotent(self):
        active = self.create_progression_for(activate=True)
        key = "repeat-progression-close"
        first = self.close_progression(active.case_id, key=key)
        second = self.close_progression(active.case_id, key=key)
        self.assertEqual(first, second)
        self.assertEqual("closed", self.lifecycle.get_lifecycle_context(
            active.case_id
        )["cycle_status"])

    def test_progression_close_rolls_back_case_when_cycle_close_fails(self):
        active = self.create_progression_for(activate=True)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER fail_progression_cycle_close
                   BEFORE UPDATE OF status ON application_cycles
                   WHEN OLD.source_student_case_id={case_id}
                    AND NEW.status='closed'
                   BEGIN SELECT RAISE(ABORT,'cycle close failure'); END""".format(
                    case_id=active.case_id
                )
            )
            connection.commit()
        with self.assertRaises(ProgressionCaseIntegrityError):
            self.close_progression(active.case_id)
        context = self.lifecycle.get_lifecycle_context(active.case_id)
        case = self.progression_service.get_case(active.case_id)
        self.assertEqual((None, "preparing", "active"), (
            case.case_outcome, context["case_status"],
            context["cycle_status"],
        ))

    def test_closed_history_does_not_reopen_for_new_progression(self):
        first = self.create_progression_for(activate=True)
        self.close_progression(first.case_id)
        first_context = self.lifecycle.get_lifecycle_context(first.case_id)
        second = self.create_progression_for(activate=True)
        self.assertEqual("closed", self.lifecycle.get_lifecycle_context(
            first.case_id
        )["cycle_status"])
        self.assertNotEqual(
            first_context["application_cycle_id"], self.cycle_id_for(second.case_id)
        )

    def test_offer_binds_to_active_progression_cycle(self):
        active = self.create_progression_for(activate=True)
        self.add_offer(active.case_id)
        self.assertEqual(self.cycle_id_for(active.case_id), self.record_cycle(
            "offers", 1000
        ))

    def test_coe_and_updated_oshc_bind_to_active_progression_cycle(self):
        active = self.create_progression_for(activate=True)
        self.link_coe(active.case_id)
        self.set_oshc(active.case_id)
        cycle_id = self.cycle_id_for(active.case_id)
        self.assertEqual((cycle_id, cycle_id), (
            self.record_cycle("coe_oshc", 1100),
            self.record_cycle("coe_oshc", 1301),
        ))

    def test_carried_forward_oshc_keeps_historical_cycle(self):
        initial = self.insert_initial_cycle()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE coe_oshc SET application_cycle_id=? WHERE id=1300",
                (initial.id,),
            )
            connection.commit()
        active = self.create_progression_for(activate=True)
        self.progression_service.set_progression_arrangement(
            active.case_id, arrangement_type="oshc", status="confirmed",
            reason=None, notes="Carried forward historical OSHC",
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.flags(),
        )
        self.assertEqual(initial.id, self.record_cycle("coe_oshc", 1300))

    def test_cross_cycle_record_reassignment_is_rejected_without_mutation(self):
        initial = self.insert_initial_cycle()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE offers SET application_cycle_id=? WHERE id=1000",
                (initial.id,),
            )
            connection.commit()
        active = self.create_progression_for(activate=True)
        with self.assertRaises(ProgressionCaseIntegrityError):
            self.add_offer(active.case_id)
        self.assertEqual(initial.id, self.record_cycle("offers", 1000))

    def test_new_progression_offer_never_remains_cycle_unassigned(self):
        created = self.create_progression_for()
        self.add_offer(created.case_id)
        self.assertEqual(
            self.cycle_id_for(created.case_id), self.record_cycle("offers", 1000)
        )

    def test_list_and_active_queries_cover_full_progression_history(self):
        self.insert_initial_cycle()
        historical_transfer = self.create_transfer_for(activate=True)
        self.service.cancel_transfer_case(
            historical_transfer.case_id, actor_user_id=1,
            reason="historical transfer", idempotency_key="history-transfer",
        )
        closed = self.create_progression_for(activate=True)
        self.close_progression(closed.case_id)
        active = self.create_progression_for(activate=True)
        rows = self.lifecycle.list_student_lifecycles("student-1")
        identities = {
            (row.lifecycle_type, row.case_status, row.cycle_status)
            for row in rows
        }
        self.assertIn(("initial", None, "closed"), identities)
        self.assertIn(("transfer", "cancelled", "closed"), identities)
        self.assertIn(("progression", "cancelled", "closed"), identities)
        self.assertIn(("progression", "preparing", "active"), identities)
        self.assertEqual(active.case_id, self.lifecycle.get_active_lifecycle(
            "student-1"
        ).case_id)

    def test_desktop_and_mobile_read_the_same_lifecycle_results(self):
        self.insert_initial_cycle()
        self.create_progression_for()
        desktop = self.lifecycle.list_student_lifecycles("student-1")
        mobile = self.lifecycle.list_student_lifecycles("student-1")
        self.assertEqual(desktop, mobile)

    def test_activation_creates_one_stable_progression_pathway_identity(self):
        draft = self.create_progression_for()
        active = self.progression_service.activate_case(
            draft.case_id,
            actor_user_id=1,
            idempotency_key="r5-pathway-activation",
        )
        replay = self.progression_service.activate_case(
            draft.case_id,
            actor_user_id=1,
            idempotency_key="r5-pathway-activation",
        )
        cycle_id = self.cycle_id_for(active.case_id)
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """SELECT application_cycle_id,progression_case_id,student_id,
                          pathway_uid,status
                     FROM progression_pathway_acceptances"""
            ).fetchall()
        self.assertEqual(active.case_id, replay.case_id)
        self.assertEqual(
            [(cycle_id, active.case_id, "student-1",
              f"progression-pathway:{cycle_id}:{active.case_id}", "assembling")],
            [tuple(row) for row in rows],
        )

    def test_three_final_targets_project_to_three_required_components(self):
        active = self.create_progression_for(activate=True)
        targets = list(self.progression_service.get_case_data(active.case_id)[
            "target_applications"
        ])
        for number in (2, 3):
            targets.append(self.progression_service.add_target_application(
                active.case_id,
                institution_id=20,
                institution_name_snapshot=f"Target University {number}",
                course_type="bachelor",
                course_name=f"Degree {number}",
                expected_start_date="2027-02-01",
                notes=None,
                actor_user_id=1,
                actor_permissions={"progression_cases.update"},
                flags=self.flags(),
            ))
        for target in targets:
            self.progression_service.select_final_application(
                active.case_id,
                target["id"],
                actor_user_id=1,
                actor_permissions={"progression_cases.update"},
                flags=self.flags(),
            )
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """SELECT progression_target_application_id,component_order,
                          component_role,active_final
                     FROM progression_pathway_components
                    ORDER BY component_order"""
            ).fetchall()
        self.assertEqual(
            [(target["id"], number, "required", 1)
             for number, target in enumerate(targets, start=1)],
            [tuple(row) for row in rows],
        )

    def test_final_target_binds_to_exact_canonical_application(self):
        active = self.create_progression_for(activate=True)
        target = self.progression_service.get_case_data(active.case_id)[
            "target_applications"
        ][0]
        self.progression_service.select_final_application(
            active.case_id,
            target["id"],
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.flags(),
        )
        binding = self.progression_service.bind_target_lifecycle_context(
            active.case_id,
            target["id"],
            canonical_application_kind="private",
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.flags(),
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            canonical = connection.execute(
                """SELECT application_cycle_id,student_id,school_id
                     FROM private_school_applications WHERE id=?""",
                (binding["canonical_application_id"],),
            ).fetchone()
        self.assertEqual("private", binding["canonical_application_kind"])
        self.assertEqual(
            (self.cycle_id_for(active.case_id), "student-1", 20),
            tuple(canonical),
        )

    def test_component_offer_acceptance_requires_exact_component_mapping(self):
        active = self.create_progression_for(activate=True)
        target = self.progression_service.get_case_data(active.case_id)[
            "target_applications"
        ][0]
        self.progression_service.select_final_application(
            active.case_id,
            target["id"],
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.flags(),
        )
        offer = self.progression_service.add_offer(
            active.case_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.offers"},
            flags=self.flags(),
            stage_id=None,
            existing_offer_id=1000,
            offer_type="unconditional",
            institution_id=20,
            institution_name_snapshot="Target University",
            course_name="Bachelor",
            offer_date="2026-08-14",
            expiry_date=None,
            conditions_text=None,
            student_document_id=1600,
            target_application_id=target["id"],
            notes=None,
        )
        with self.assertRaises(ProgressionCaseIntegrityError):
            self.progression_service.update_offer(
                active.case_id,
                offer.offer_id,
                actor_user_id=1,
                actor_permissions={"progression_cases.offers"},
                flags=self.flags(),
                offer_status="accepted",
                notes=None,
            )
        self.progression_service.bind_target_lifecycle_context(
            active.case_id,
            target["id"],
            canonical_application_kind="private",
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.flags(),
        )
        accepted = self.progression_service.update_offer(
            active.case_id,
            offer.offer_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.offers"},
            flags=self.flags(),
            offer_status="accepted",
            notes=None,
        )
        self.assertEqual("accepted", accepted.offer_status)
        with closing(sqlite3.connect(self.database_path)) as connection:
            component = connection.execute(
                """SELECT accepted_progression_offer_id,canonical_offer_event_id
                     FROM progression_pathway_components"""
            ).fetchone()
        self.assertEqual(offer.offer_id, component[0])
        self.assertIsNotNone(component[1])

    def test_lifecycle_query_exposes_exact_progression_pathway_components(self):
        active = self.create_progression_for(activate=True)
        target = self.progression_service.get_case_data(active.case_id)[
            "target_applications"
        ][0]
        self.progression_service.select_final_application(
            active.case_id, target["id"], actor_user_id=1,
            actor_permissions={"progression_cases.update"}, flags=self.flags(),
        )
        self.progression_service.bind_target_lifecycle_context(
            active.case_id, target["id"],
            canonical_application_kind="private", actor_user_id=1,
            actor_permissions={"progression_cases.update"}, flags=self.flags(),
        )

        context = self.lifecycle.get_lifecycle_context(active.case_id)

        self.assertEqual("assembling", context["progression_pathway"]["status"])
        self.assertEqual(1, len(context["progression_components"]))
        component = context["progression_components"][0]
        self.assertEqual(target["id"], component["target_application_id"])
        self.assertEqual("private", component["canonical_application_kind"])
        self.assertIsNotNone(component["canonical_application_id"])

    def test_component_documents_and_coe_remain_bound_to_exact_target(self):
        active = self.create_progression_for(activate=True)
        target = self.progression_service.get_case_data(active.case_id)[
            "target_applications"
        ][0]
        self.progression_service.select_final_application(
            active.case_id, target["id"], actor_user_id=1,
            actor_permissions={"progression_cases.update"}, flags=self.flags(),
        )
        self.progression_service.bind_target_lifecycle_context(
            active.case_id, target["id"], canonical_application_kind="private",
            actor_user_id=1, actor_permissions={"progression_cases.update"},
            flags=self.flags(),
        )
        offer = self.progression_service.add_offer(
            active.case_id, actor_user_id=1,
            actor_permissions={"progression_cases.offers"}, flags=self.flags(),
            stage_id=None, existing_offer_id=1000, offer_type="unconditional",
            institution_id=20, institution_name_snapshot="Target University",
            course_name="Bachelor", offer_date="2026-08-14", expiry_date=None,
            conditions_text=None, student_document_id=1600,
            target_application_id=target["id"], notes=None,
        )
        self.progression_service.update_offer(
            active.case_id, offer.offer_id, actor_user_id=1,
            actor_permissions={"progression_cases.offers"}, flags=self.flags(),
            offer_status="accepted", notes=None,
        )
        signed = self.progression_service.link_progression_document(
            active.case_id, document_role="signed_offer",
            student_document_id=1600, target_application_id=target["id"],
            status="confirmed", notes=None, actor_user_id=1,
            actor_permissions={"progression_cases.materials"}, flags=self.flags(),
        )
        payment = self.progression_service.link_progression_document(
            active.case_id, document_role="payment_receipt",
            student_document_id=1601, target_application_id=target["id"],
            status="confirmed", notes=None, actor_user_id=1,
            actor_permissions={"progression_cases.materials"}, flags=self.flags(),
        )
        coe = self.progression_service.link_progression_document(
            active.case_id, document_role="coe", student_document_id=1601,
            target_application_id=target["id"], status="confirmed", notes=None,
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"}, flags=self.flags(),
            lifecycle_record_id=1100,
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            component = connection.execute(
                """SELECT signed_offer_document_link_id,payment_document_link_id,
                          coe_document_link_id,coe_record_id
                     FROM progression_pathway_components"""
            ).fetchone()
            pathway_status = connection.execute(
                "SELECT status FROM progression_pathway_acceptances"
            ).fetchone()[0]
        self.assertEqual(
            (signed["id"], payment["id"], coe["id"], 1100), tuple(component)
        )
        self.assertEqual("accepted", pathway_status)

if __name__ == "__main__":
    import unittest

    unittest.main()
