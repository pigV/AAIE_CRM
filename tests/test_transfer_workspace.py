from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

import record_picker_and_uploader
from student_case_feature_flags import StudentCaseFeatureFlags
import transfer_case_errors
import transfer_case_views
import transfer_completion_service
import transfer_workspace_view_model
from tests.test_student_case_phase2_transfer import (
    TransferCaseT6AdmissionRecordTests,
)


class TransferUploadReturnContextTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "return-context.db"
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE student_cases(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL,
                    case_type TEXT NOT NULL
                );
                CREATE TABLE transfer_school_applications(
                    id INTEGER PRIMARY KEY,
                    case_id INTEGER NOT NULL
                );
                CREATE TABLE case_payment_requirements(
                    id INTEGER PRIMARY KEY,
                    case_id INTEGER NOT NULL,
                    transfer_application_id INTEGER NOT NULL
                );
                INSERT INTO student_cases VALUES(41,'student-1','transfer');
                INSERT INTO transfer_school_applications VALUES(71,41);
                INSERT INTO transfer_school_applications VALUES(72,41);
                INSERT INTO case_payment_requirements VALUES(91,41,71);
                """
            )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_offer_context_round_trips_to_its_application_anchor(self):
        context = record_picker_and_uploader.begin_record_upload(
            41,
            "offer",
            application_id=71,
            return_section="application",
        )
        self.assertEqual(
            context.form_values()["transfer_return_section"],
            "application",
        )
        parsed = record_picker_and_uploader.upload_context_from_form(
            context.form_values()
        )
        self.assertEqual(parsed, context)
        self.assertEqual(
            record_picker_and_uploader.resolve_transfer_return_target(
                self.database_path,
                "student-1",
                parsed,
            ),
            "/students/student-1/transfer-cases/41#transfer-application-71",
        )

    def test_payment_context_returns_to_exact_requirement_and_rejects_tampering(self):
        context = record_picker_and_uploader.begin_record_upload(
            41,
            "payment",
            application_id=71,
            payment_requirement_id=91,
            return_section="payment",
        )
        self.assertEqual(
            record_picker_and_uploader.resolve_transfer_return_target(
                self.database_path,
                "student-1",
                context,
            ),
            "/students/student-1/transfer-cases/41#transfer-payment-91",
        )
        for student_id, changes in (
            ("student-2", {}),
            ("student-1", {"application_id": 72}),
            ("student-1", {"return_section": "https://example.com"}),
        ):
            values = {
                "case_id": context.case_id,
                "record_type": context.record_type,
                "application_id": context.application_id,
                "payment_requirement_id": context.payment_requirement_id,
                "return_section": context.return_section,
            }
            values.update(changes)
            tampered = record_picker_and_uploader.TransferUploadContext(**values)
            with self.subTest(student_id=student_id, changes=changes), self.assertRaises(
                ValueError
            ):
                record_picker_and_uploader.resolve_transfer_return_target(
                    self.database_path,
                    student_id,
                    tampered,
                )


class TransferWorkspaceBusinessContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TransferCaseT6AdmissionRecordTests(methodName="runTest")
        self.fixture.setUp()
        self.database_path = self.fixture.database_path
        self.service = self.fixture.service

    def tearDown(self):
        self.fixture.tearDown()

    @staticmethod
    def flags():
        return StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
            completion_enabled=True,
        )

    @staticmethod
    def user(*permissions):
        return {"id": 1, "permissions": list(permissions)}

    def edit_arguments(self):
        return {
            "actor_user_id": 1,
            "actor_permissions": {"student_cases.edit"},
            "flags": self.flags(),
        }

    def render(self, case_id):
        return transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            case_id,
            self.user(
                "student_cases.view",
                "student_cases.edit",
                "student_cases.complete",
            ),
            self.flags(),
            csrf="csrf-token",
            staff_users=(),
        )

    def test_offer_school_mismatch_cannot_be_overridden_by_a_reason(self):
        case = self.fixture.create_internal()
        application = self.fixture.add_application(
            case.case_id,
            school_name_snapshot="Different Target School",
        )
        with self.assertRaises(
            transfer_case_errors.TransferAdmissionRecordSchoolMismatchError
        ):
            self.service.attach_transfer_offer(
                case.case_id,
                application.application_id,
                1000,
                manual_school_confirmation=True,
                confirmation_reason="Employee confirmed manually",
                **self.edit_arguments(),
            )

    def test_normal_page_has_no_manual_workflow_or_offer_history_controls(self):
        case, application = self.fixture.prepare_final_application()
        self.service.attach_transfer_offer(
            case.case_id,
            application.application_id,
            1000,
            **self.edit_arguments(),
        )
        html = self.render(case.case_id)
        self.assertNotIn("开始办理", html)
        self.assertNotIn("标记Offer历史", html)

    def test_each_target_school_has_its_own_offer_and_coe_workspace(self):
        case = self.fixture.create_internal()
        first = self.fixture.add_application(
            case.case_id, school_name_snapshot="Target School"
        )
        second = self.fixture.add_application(
            case.case_id, school_name_snapshot="Second School"
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE offers SET school_name='Second School' WHERE id=1001"
            )
            connection.execute(
                "UPDATE coe_oshc SET school_name='Second School' WHERE id=1101"
            )
        self.service.attach_transfer_offer(
            case.case_id, first.application_id, 1000, **self.edit_arguments()
        )
        self.service.attach_transfer_offer(
            case.case_id, second.application_id, 1001, **self.edit_arguments()
        )
        html = self.render(case.case_id)
        for application, school in ((first, "Target School"), (second, "Second School")):
            with self.subTest(school=school):
                self.assertIn(
                    f'id="transfer-application-{application.application_id}"',
                    html,
                )
                self.assertIn(
                    f'data-dialog-open="transfer-offer-select-{application.application_id}"',
                    html,
                )
                self.assertIn(
                    f'data-dialog-open="transfer-coe-select-{application.application_id}"',
                    html,
                )
        self.assertIn("上传新Offer", html)
        self.assertIn("上传新COE", html)

    def test_confirmed_coe_uses_one_persisted_link_across_workspace_and_completion(self):
        case = self.fixture.create_internal()
        application = self.fixture.add_application(
            case.case_id,
            school_name_snapshot="Target School",
            city_key=None,
            school_sector=None,
        )
        self.service.set_final_transfer_school_application(
            case.case_id,
            application.application_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.assertEqual(
            self.service.get_transfer_case(case.case_id).rule_status,
            "pending_confirmation",
        )

        linked = self.service.attach_transfer_coe(
            case.case_id,
            application.application_id,
            1100,
            **self.edit_arguments(),
        )
        repeated = self.service.attach_transfer_coe(
            case.case_id,
            application.application_id,
            1100,
            **self.edit_arguments(),
        )
        workspace = transfer_workspace_view_model.build_transfer_workspace_view_model(
            self.database_path,
            case.case_id,
            self.user("student_cases.view", "student_cases.edit"),
            self.flags(),
        )
        evaluation = transfer_completion_service.evaluate_transfer_completion(
            self.database_path, case.case_id
        )

        self.assertEqual(repeated.link_id, linked.link_id)
        self.assertEqual(workspace.applications[0].current_coe.link_id, linked.link_id)
        self.assertEqual(workspace.applications[0].current_coe.record_id, 1100)
        self.assertEqual(evaluation.coe_record_id, 1100)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM case_record_links
                       WHERE case_id=? AND transfer_application_id=?
                         AND record_type='coe' AND record_id=?""",
                    (case.case_id, application.application_id, 1100),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM case_events
                       WHERE case_id=? AND event_type='transfer_coe_attached'""",
                    (case.case_id,),
                ).fetchone()[0],
                1,
            )

    def test_replacing_coe_keeps_history_and_updates_the_shared_current_link(self):
        case, application = self.fixture.prepare_final_application()
        first = self.service.attach_transfer_coe(
            case.case_id,
            application.application_id,
            1100,
            **self.edit_arguments(),
        )
        current = self.service.replace_transfer_coe(
            case.case_id,
            application.application_id,
            1101,
            **self.edit_arguments(),
        )
        links = self.service.list_transfer_record_links(
            case.case_id, application.application_id, "coe"
        )
        workspace = transfer_workspace_view_model.build_transfer_workspace_view_model(
            self.database_path,
            case.case_id,
            self.user("student_cases.view", "student_cases.edit"),
            self.flags(),
        )
        evaluation = transfer_completion_service.evaluate_transfer_completion(
            self.database_path, case.case_id
        )

        self.assertEqual(current.record_id, 1101)
        self.assertEqual(
            [(item.record_id, item.validity_status) for item in links],
            [(1101, "current"), (1100, "superseded")],
        )
        self.assertEqual(links[1].link_id, first.link_id)
        self.assertEqual(links[1].superseded_by, current.link_id)
        self.assertEqual(workspace.applications[0].current_coe.record_id, 1101)
        self.assertEqual(evaluation.coe_record_id, 1101)

    def test_coe_school_mismatch_rolls_back_without_a_partial_link(self):
        case = self.fixture.create_internal()
        application = self.fixture.add_application(
            case.case_id,
            school_name_snapshot="Different Target School",
        )
        with self.assertRaises(
            transfer_case_errors.TransferAdmissionRecordSchoolMismatchError
        ):
            self.service.attach_transfer_coe(
                case.case_id,
                application.application_id,
                1100,
                **self.edit_arguments(),
            )
        self.assertEqual(
            self.service.list_transfer_record_links(
                case.case_id, application.application_id, "coe"
            ),
            (),
        )

    def test_payment_upload_context_targets_the_exact_payment_card(self):
        case, application = self.fixture.prepare_final_application()
        offer = self.service.attach_transfer_offer(
            case.case_id, application.application_id, 1000, **self.edit_arguments()
        )
        requirement = self.service.create_transfer_payment_requirement(
            case.case_id,
            application.application_id,
            "deposit",
            source_offer_id=offer.record_id,
            **self.edit_arguments(),
        )
        html = self.render(case.case_id)
        self.assertIn(
            f'id="transfer-payment-{requirement.requirement_id}"', html
        )
        self.assertIn(
            'name="transfer_return_section" value="payment"', html
        )


if __name__ == "__main__":
    unittest.main()
