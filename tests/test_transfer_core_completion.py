from contextlib import closing
from http import HTTPStatus
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app
from tests.test_transfer_completion_readiness import (
    TransferCompletionReadinessTests,
)
import transfer_case_views
import transfer_completion_service
import transfer_workspace_view_model
import record_picker_and_uploader
from student_case_feature_flags import StudentCaseFeatureFlags


class TransferCoreCompletionTests(unittest.TestCase):
    connect = TransferCompletionReadinessTests.connect
    _insert_case = TransferCompletionReadinessTests._insert_case
    _insert_final_application = (
        TransferCompletionReadinessTests._insert_final_application
    )

    def setUp(self):
        TransferCompletionReadinessTests.setUp(self)
        self.final_application_id = self._insert_final_application()
        self.offer_path = self.root / "offer.pdf"
        self.coe_path = self.root / "coe.pdf"
        self.payment_path = self.root / "payment.pdf"
        for path in (self.offer_path, self.coe_path, self.payment_path):
            path.write_bytes(b"formal-nas-fixture")
        with closing(self.connect()) as connection:
            connection.executescript(
                """CREATE TABLE IF NOT EXISTS upload_sessions(
                       id INTEGER PRIMARY KEY,session_token TEXT UNIQUE NOT NULL,
                       student_id TEXT NOT NULL,module_key TEXT NOT NULL,
                       uploaded_by INTEGER,uploaded_at TEXT NOT NULL,
                       status TEXT NOT NULL,total_files INTEGER NOT NULL DEFAULT 0,
                       confirmed_files INTEGER NOT NULL DEFAULT 0,
                       cancelled_files INTEGER NOT NULL DEFAULT 0,notes TEXT
                   );
                   CREATE TABLE IF NOT EXISTS upload_review_items(
                       id INTEGER PRIMARY KEY,upload_session_id INTEGER NOT NULL,
                       student_id TEXT NOT NULL,module_key TEXT NOT NULL,
                       original_file_name TEXT NOT NULL,
                       staging_file_path TEXT NOT NULL,final_file_name TEXT,
                       final_file_path TEXT,file_type TEXT,
                       file_size INTEGER NOT NULL DEFAULT 0,
                       selected_document_type TEXT,target_name TEXT,notes TEXT,
                       status TEXT NOT NULL,uploaded_by INTEGER,
                       uploaded_at TEXT NOT NULL,confirmed_by INTEGER,
                       confirmed_at TEXT,cancelled_by INTEGER,
                       cancelled_at TEXT,error_message TEXT
                   );"""
            )
            additions = {
                "offers": (
                    "school_name TEXT", "offer_status TEXT",
                    "offer_file_path TEXT", "created_at TEXT",
                ),
                "coe_oshc": (
                    "school_name TEXT", "file_path TEXT", "created_at TEXT",
                ),
                "payments": (
                    "receipt_file_path TEXT", "created_at TEXT",
                ),
            }
            for table, definitions in additions.items():
                columns = {
                    row[1] for row in connection.execute(
                        f"PRAGMA table_info('{table}')"
                    )
                }
                for definition in definitions:
                    if definition.split()[0] not in columns:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {definition}"
                        )
            connection.execute(
                """UPDATE offers SET school_name='Target School',
                       offer_status='received',offer_file_path=?,
                       created_at='2026-07-30T10:00:00+10:00' WHERE id=1000""",
                (str(self.offer_path),),
            )
            connection.execute(
                """UPDATE coe_oshc SET school_name='Target School',file_path=?,
                       created_at='2026-07-30T10:00:00+10:00' WHERE id=1100""",
                (str(self.coe_path),),
            )
            connection.execute(
                """UPDATE payments SET receipt_file_path=?,
                       created_at='2026-07-30T10:00:00+10:00' WHERE id=1500""",
                (str(self.payment_path),),
            )
            connection.commit()
        completion = __import__("transfer_completion_service")
        self.completion = completion.TransferCompletionService(
            self.database_path,
            clock=lambda: "2026-08-03T12:00:00+10:00",
        )

    def tearDown(self):
        TransferCompletionReadinessTests.tearDown(self)

    def _insert_link(self, record_type, record_id, link_id, role="current"):
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO case_record_links(
                       id,case_id,transfer_application_id,record_type,record_id,
                       document_id,link_role,validity_status,effective_from,
                       created_by,created_at
                   ) VALUES(?,?,?,?,?,NULL,?,'current',?,1,?)""",
                (
                    link_id, self.case_id, self.final_application_id,
                    record_type, record_id, role,
                    "2026-07-30T10:00:00+10:00",
                    "2026-07-30T10:00:00+10:00",
                ),
            )
            connection.commit()

    def _confirm_review(self, item_id, module, path):
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO upload_sessions(
                       id,session_token,student_id,module_key,uploaded_by,
                       uploaded_at,status,total_files,confirmed_files,
                       cancelled_files
                   ) VALUES(?,?, 'student-1',?,1,?,'Confirmed',1,1,0)""",
                (
                    item_id,
                    f"review-{item_id}",
                    module,
                    "2026-07-30T10:00:00+10:00",
                ),
            )
            connection.execute(
                """INSERT INTO upload_review_items(
                       id,upload_session_id,student_id,module_key,
                       original_file_name,staging_file_path,final_file_name,
                       final_file_path,file_size,status,uploaded_by,uploaded_at,
                       confirmed_by,confirmed_at
                   ) VALUES(?,?, 'student-1',?,?,?, ?,?,100,'Confirmed',1,?,1,?)""",
                (
                    item_id,
                    item_id,
                    module,
                    Path(path).name,
                    f"staging/{Path(path).name}",
                    Path(path).name,
                    str(path),
                    "2026-07-30T10:00:00+10:00",
                    "2026-07-30T10:00:00+10:00",
                ),
            )
            connection.commit()

    def add_offer(self):
        self._confirm_review(7001, "Offer", self.offer_path)
        self._insert_link("offer", 1000, 8001)

    def add_coe(self):
        self._confirm_review(7002, "COE + OSHC", self.coe_path)
        self._insert_link("coe", 1100, 8002)

    def add_payment(self, status="paid"):
        self._confirm_review(7003, "Payments", self.payment_path)
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO case_payment_requirements(
                       id,case_id,transfer_application_id,requirement_type,
                       status,created_at,updated_at
                   ) VALUES(9001,?,?, 'deposit',?,?,?)""",
                (
                    self.case_id, self.final_application_id, status,
                    "2026-07-30T10:00:00+10:00",
                    "2026-07-30T10:00:00+10:00",
                ),
            )
            connection.execute(
                """INSERT INTO case_record_links(
                       id,case_id,transfer_application_id,record_type,record_id,
                       document_id,link_role,validity_status,effective_from,
                       created_by,created_at
                   ) VALUES(8003,?,?, 'payment',1500,NULL,'payment_proof',
                            'current',?,1,?)""",
                (
                    self.case_id, self.final_application_id,
                    "2026-07-30T10:00:00+10:00",
                    "2026-07-30T10:00:00+10:00",
                ),
            )
            connection.execute(
                """INSERT INTO case_payment_requirement_proofs(
                       id,case_id,transfer_application_id,payment_requirement_id,
                       case_record_link_id,validity_status,effective_from,
                       created_by,created_at
                   ) VALUES(9101,?,?,9001,8003,'current',?,1,?)""",
                (
                    self.case_id, self.final_application_id,
                    "2026-07-30T10:00:00+10:00",
                    "2026-07-30T10:00:00+10:00",
                ),
            )
            connection.commit()

    def assert_missing(self, expected):
        result = self.completion.evaluate_transfer_completion(self.case_id)
        self.assertFalse(result.is_ready_to_complete)
        self.assertEqual(set(result.missing_requirements), set(expected))

    def test_only_offer_is_not_ready(self):
        self.add_offer()
        self.assert_missing({"payment_receipt", "final_coe"})

    def test_offer_and_payment_are_not_ready(self):
        self.add_offer()
        self.add_payment()
        self.assert_missing({"final_coe"})

    def test_offer_and_coe_are_not_ready(self):
        self.add_offer()
        self.add_coe()
        self.assert_missing({"payment_receipt"})

    def test_payment_and_coe_are_not_ready(self):
        self.add_payment()
        self.add_coe()
        self.assert_missing({"final_offer"})

    def test_three_formal_links_for_same_final_school_are_ready_without_optional_arrangements(self):
        self.add_offer()
        self.add_payment()
        self.add_coe()
        result = self.completion.evaluate_transfer_completion(self.case_id)
        self.assertTrue(result.is_ready_to_complete)
        self.assertEqual(result.missing_requirements, ())
        self.assertEqual(result.inconsistent_requirements, ())
        self.assertEqual(result.final_school_application_id, self.final_application_id)
        self.assertEqual(result.offer_record_id, 1000)
        self.assertEqual(result.payment_record_id, 1500)
        self.assertEqual(result.coe_record_id, 1100)
        self.assertEqual(result.payment_status, "paid")

    def test_real_session_permissions_drive_workspace_and_business_completion_ui(self):
        self.add_offer()
        self.add_payment()
        self.add_coe()
        flags = StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
            completion_enabled=True,
        )
        for username in ("admin", "sonnyz", "tongy", "tedc"):
            with self.subTest(username=username):
                user = {
                    "id": 1,
                    "username": username,
                    "permissions_json": json.dumps([
                        "student_cases.view",
                        "student_cases.complete",
                    ]),
                }
                workspace = (
                    transfer_workspace_view_model.build_transfer_workspace_view_model(
                        self.database_path, self.case_id, user, flags
                    )
                )
                html = transfer_case_views.render_transfer_case_detail(
                    self.database_path,
                    "student-1",
                    self.case_id,
                    user,
                    flags,
                    csrf="csrf-token",
                )
                self.assertFalse(getattr(workspace, "completion_ready", None))
                self.assertFalse(getattr(workspace, "can_complete", None))
                self.assertTrue(getattr(workspace, "completion_enabled", None))
                self.assertFalse(getattr(workspace, "case_completed", None))
                self.assertNotIn(
                    'data-dialog-open="transfer-core-complete-case"', html
                )
                for label, status in (
                    ("最终学校", "已确认"),
                    ("Offer", "已关联"),
                    ("Payment Receipt", "已关联"),
                    ("付款状态", "已支付"),
                    ("COE", "已关联"),
                    ("完成条件", "未满足"),
                ):
                    self.assertIn(
                        f"<span>{label}</span><strong>{status}</strong>", html
                    )
                    self.assertNotIn(
                        f"<strong>{label}：{status}</strong>", html
                    )

    def test_workspace_hides_completion_when_permission_ready_or_case_state_blocks(self):
        flags = StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
            completion_enabled=True,
        )
        view_only = {
            "id": 1,
            "permissions_json": json.dumps(["student_cases.view"]),
        }
        not_ready = transfer_workspace_view_model.build_transfer_workspace_view_model(
            self.database_path, self.case_id, view_only, flags
        )
        self.assertFalse(getattr(not_ready, "completion_ready", True))
        self.assertFalse(getattr(not_ready, "can_complete", True))

        self.add_offer()
        self.add_payment()
        self.add_coe()
        permitted = {
            "id": 1,
            "permissions_json": json.dumps([
                "student_cases.view",
                "student_cases.complete",
            ]),
        }
        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE student_cases
                   SET status='completed',active_slot=0,completed_at=?
                   WHERE id=?""",
                ("2026-08-03T12:00:00+10:00", self.case_id),
            )
            connection.execute(
                "UPDATE transfer_cases SET workflow_state='completed' WHERE case_id=?",
                (self.case_id,),
            )
            connection.commit()
        completed = transfer_workspace_view_model.build_transfer_workspace_view_model(
            self.database_path, self.case_id, permitted, flags
        )
        self.assertTrue(getattr(completed, "case_completed", False))
        self.assertFalse(getattr(completed, "can_complete", True))

    def test_school_mismatch_unpaid_and_missing_formal_file_block(self):
        self.add_offer()
        self.add_payment(status="pending")
        self.add_coe()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE coe_oshc SET school_name='Different School' WHERE id=1100"
            )
            connection.commit()
        self.offer_path.unlink()
        result = self.completion.evaluate_transfer_completion(self.case_id)
        self.assertFalse(result.is_ready_to_complete)
        self.assertIn("offer_file_not_available", result.inconsistent_requirements)
        self.assertIn("payment_not_paid", result.inconsistent_requirements)
        self.assertIn("coe_final_school_mismatch", result.inconsistent_requirements)

    def test_pending_upload_review_never_counts_as_complete(self):
        self.add_offer()
        self.add_payment()
        self.add_coe()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE upload_review_items SET status='NeedsReview' WHERE id=7002"
            )
            connection.commit()
        result = self.completion.evaluate_transfer_completion(self.case_id)
        self.assertFalse(result.is_ready_to_complete)
        self.assertIn("coe_upload_review_pending", result.inconsistent_requirements)

    def test_payment_picker_marks_paid_only_after_verified_link(self):
        self._confirm_review(7003, "Payments", self.payment_path)
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO case_payment_requirements(
                       id,case_id,transfer_application_id,requirement_type,
                       status,created_at,updated_at
                   ) VALUES(9001,?,?,'deposit','pending',?,?)""",
                (
                    self.case_id,
                    self.final_application_id,
                    "2026-07-30T10:00:00+10:00",
                    "2026-07-30T10:00:00+10:00",
                ),
            )
            connection.commit()
        context = record_picker_and_uploader.begin_record_upload(
            self.case_id,
            "payment",
            application_id=self.final_application_id,
            payment_requirement_id=9001,
        )
        record_picker_and_uploader.select_existing_record(
            self.database_path,
            context=context,
            record_id=1500,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=StudentCaseFeatureFlags(
                read_enabled=True,
                transfer_write_enabled=True,
            ),
        )
        with closing(self.connect()) as connection:
            requirement = connection.execute(
                "SELECT status FROM case_payment_requirements WHERE id=9001"
            ).fetchone()
            proof = connection.execute(
                """SELECT proof.id FROM case_payment_requirement_proofs proof
                   JOIN case_record_links link
                     ON link.id=proof.case_record_link_id
                   WHERE proof.payment_requirement_id=9001
                     AND proof.validity_status='current'
                     AND link.validity_status='current'
                     AND link.record_type='payment'
                     AND link.record_id=1500"""
            ).fetchone()
        self.assertEqual(requirement["status"], "paid")
        self.assertIsNotNone(proof)

    def test_upload_context_round_trip_is_strict(self):
        context = record_picker_and_uploader.begin_record_upload(
            self.case_id,
            "coe",
            application_id=self.final_application_id,
        )
        self.assertEqual(
            record_picker_and_uploader.upload_context_from_form(
                context.form_values()
            ),
            context,
        )
        forged = context.form_values()
        forged["transfer_case_id"] = "not-an-id"
        with self.assertRaises(ValueError):
            record_picker_and_uploader.upload_context_from_form(forged)

    def test_confirmed_coe_link_sets_matching_application_as_only_final(self):
        self._confirm_review(7002, "COE + OSHC", self.coe_path)
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE transfer_school_applications SET is_final=0 WHERE case_id=?",
                (self.case_id,),
            )
            connection.execute(
                """UPDATE transfer_cases SET final_application_id=NULL,
                          workflow_state='waiting_completion_document'
                   WHERE case_id=?""",
                (self.case_id,),
            )
            connection.execute(
                "UPDATE student_cases SET status='waiting' WHERE id=?",
                (self.case_id,),
            )
            connection.commit()
        context = record_picker_and_uploader.begin_record_upload(
            self.case_id,
            "coe",
            application_id=self.final_application_id,
        )
        record_picker_and_uploader.select_existing_record(
            self.database_path,
            context=context,
            record_id=1100,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=StudentCaseFeatureFlags(
                read_enabled=True,
                transfer_write_enabled=True,
            ),
        )
        with closing(self.connect()) as connection:
            final_rows = connection.execute(
                """SELECT id FROM transfer_school_applications
                   WHERE case_id=? AND is_final=1""",
                (self.case_id,),
            ).fetchall()
            transfer = connection.execute(
                "SELECT final_application_id FROM transfer_cases WHERE case_id=?",
                (self.case_id,),
            ).fetchone()
        self.assertEqual([row["id"] for row in final_rows], [self.final_application_id])
        self.assertEqual(transfer["final_application_id"], self.final_application_id)

    def test_manual_completion_rechecks_permission_is_idempotent_and_keeps_enrolments(self):
        self.add_offer()
        self.add_payment()
        self.add_coe()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE transfer_cases SET workflow_state='preparing' WHERE case_id=?",
                (self.case_id,),
            )
            connection.commit()
        first = self.completion.complete_transfer_case(
            self.case_id,
            1,
            actor_permissions={"student_cases.complete"},
            completion_enabled=True,
        )
        second = self.completion.complete_transfer_case(
            self.case_id,
            1,
            actor_permissions={"student_cases.complete"},
            completion_enabled=True,
        )
        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(first.confirmation_id, second.confirmation_id)
        with closing(self.connect()) as connection:
            case = connection.execute(
                """SELECT sc.status,sc.active_slot,sc.completed_at,sc.updated_by,
                          tc.workflow_state,tc.current_school_enrolment_id,
                          tc.upcoming_school_enrolment_id
                   FROM student_cases sc JOIN transfer_cases tc ON tc.case_id=sc.id
                   WHERE sc.id=?""",
                (self.case_id,),
            ).fetchone()
            confirmation = connection.execute(
                "SELECT actor_user_id FROM case_completion_confirmations WHERE id=?",
                (first.confirmation_id,),
            ).fetchone()
            event_count = connection.execute(
                """SELECT count(*) FROM case_events WHERE case_id=?
                     AND event_type='transfer_completion_completed'""",
                (self.case_id,),
            ).fetchone()[0]
        self.assertEqual(case["status"], "completed")
        self.assertEqual(case["workflow_state"], "completed")
        self.assertEqual(case["active_slot"], 0)
        self.assertEqual(case["updated_by"], 1)
        self.assertTrue(case["completed_at"])
        self.assertIsNone(case["current_school_enrolment_id"])
        self.assertIsNone(case["upcoming_school_enrolment_id"])
        self.assertEqual(confirmation["actor_user_id"], 1)
        self.assertEqual(event_count, 1)

    def test_manual_completion_requires_flag_exact_permission_and_fresh_readiness(self):
        self.add_offer()
        self.add_payment()
        self.add_coe()
        for permissions, enabled in ((set(), True), ({"student_cases.complete"}, False)):
            with self.subTest(permissions=permissions, enabled=enabled):
                with self.assertRaises(PermissionError):
                    self.completion.complete_transfer_case(
                        self.case_id,
                        1,
                        actor_permissions=permissions,
                        completion_enabled=enabled,
                    )
        self.coe_path.unlink()
        with self.assertRaises(ValueError):
            self.completion.complete_transfer_case(
                self.case_id,
                1,
                actor_permissions={"student_cases.complete"},
                completion_enabled=True,
            )


class TransferCoreCompletionViewTests(unittest.TestCase):
    @staticmethod
    def evaluation(*, ready=False):
        return transfer_completion_service.TransferCompletionEvaluation(
            case_id=19,
            student_id="student-1",
            is_ready_to_complete=ready,
            missing_requirements=() if ready else ("payment_receipt", "final_coe"),
            inconsistent_requirements=(),
            final_school_application_id=88,
            offer_record_id=101,
            payment_record_id=202 if ready else None,
            coe_record_id=303 if ready else None,
            payment_status="paid" if ready else None,
        )

    @staticmethod
    def flags(*, completion=True):
        return StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
            completion_enabled=completion,
        )

    def render(
        self,
        *,
        ready=False,
        permissions=("student_cases.view", "student_cases.complete"),
        completion=True,
    ):
        return transfer_case_views._core_completion_section(
            "student-1",
            19,
            self.evaluation(ready=ready),
            can_complete=(
                ready
                and completion
                and "student_cases.complete" in permissions
            ),
            csrf="csrf-token",
        )

    def test_business_status_is_chinese_and_button_only_when_ready(self):
        blocked = self.render()
        self.assertIn("<span>完成条件</span><strong>未满足</strong>", blocked)
        self.assertIn("<span>Payment Receipt</span><strong>缺失</strong>", blocked)
        self.assertIn("<span>COE</span><strong>缺失</strong>", blocked)
        self.assertNotIn("确认转学完成", blocked)
        for forbidden in ("Blocking", "Snapshot", "missing", "payment_receipt"):
            self.assertNotIn(forbidden, blocked)

        ready = self.render(ready=True)
        self.assertIn("<span>完成条件</span><strong>已满足</strong>", ready)
        self.assertIn("确认转学完成", ready)
        self.assertIn(
            'action="/students/student-1/transfer-cases/19/complete"', ready
        )
        self.assertIn('name="effective_date"', ready)
        self.assertIn('name="confirmed"', ready)

    def test_completion_button_requires_exact_permission_and_enabled_flag(self):
        for permissions, enabled in (
            (("student_cases.view",), True),
            (("student_cases.view", "*"), True),
            (("student_cases.view", "student_cases.complete"), False),
        ):
            with self.subTest(permissions=permissions, enabled=enabled):
                html = self.render(
                    ready=True,
                    permissions=permissions,
                    completion=enabled,
                )
                self.assertNotIn("确认转学完成", html)

    def test_four_approved_accounts_share_the_exact_completion_button_contract(self):
        for username in ("admin", "sonnyz", "tongy", "tedc"):
            with self.subTest(username=username):
                html = transfer_case_views._core_completion_section(
                    "student-1",
                    19,
                    self.evaluation(ready=True),
                    can_complete=True,
                    csrf="csrf-token",
                )
                self.assertIn("确认转学完成", html)

        for username in ("plain-manager", "plain-consultant", "readonly"):
            with self.subTest(username=username):
                html = transfer_case_views._core_completion_section(
                    "student-1",
                    19,
                    self.evaluation(ready=True),
                    can_complete=False,
                    csrf="csrf-token",
                )
                self.assertNotIn("确认转学完成", html)


class TransferCoreCompletionRoutePermissionTests(unittest.TestCase):
    @staticmethod
    def flags():
        return StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
            completion_enabled=True,
        )

    def handler(self, username, permissions):
        handler = object.__new__(app.CRMHandler)
        handler.user = {
            "id": 1,
            "username": username,
            "permissions_json": json.dumps(list(permissions)),
        }
        handler.form = {
            "confirmed": ["1"],
            "idempotency_key": [f"test-{username}"],
            "effective_date": ["2027-02-01"],
            "completion_note": ["All transfer requirements confirmed"],
        }
        handler.require = mock.Mock(return_value=True)
        handler.transfer_case_flags = mock.Mock(return_value=self.flags())
        handler.error_page = mock.Mock(
            side_effect=lambda message, status: (int(status), message)
        )
        handler.transfer_completion_redirect = mock.Mock(
            return_value=(HTTPStatus.SEE_OTHER, "redirect")
        )
        handler.transfer_case_for_student = mock.Mock(return_value={"id": 19})
        return handler

    def test_original_roles_and_unprivileged_accounts_receive_403(self):
        for username in ("plain-manager", "plain-consultant", "readonly"):
            with self.subTest(username=username):
                handler = self.handler(username, ("student_cases.view",))
                result = handler.student_transfer_completion_action(
                    "POST", "student-1", "19", "complete"
                )
                self.assertEqual(result[0], HTTPStatus.FORBIDDEN)

    def test_four_approved_accounts_reach_the_unified_completion_service(self):
        for username in ("admin", "sonnyz", "tongy", "tedc"):
            with self.subTest(username=username):
                handler = self.handler(
                    username,
                    ("student_cases.view", "student_cases.complete"),
                )
                completion = mock.Mock()
                with mock.patch.object(
                    app, "TransferCaseService", return_value=completion
                ):
                    result = handler.student_transfer_completion_action(
                        "POST", "student-1", "19", "complete"
                    )
                self.assertEqual(result[0], HTTPStatus.SEE_OTHER)
                completion.complete_transfer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
