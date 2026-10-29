from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
import importlib
import sqlite3
import unittest


class _EventDatabase:
    @contextmanager
    def transaction(self):
        yield object()


class TransferContractEventGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = importlib.import_module("app")

    def emit(self, contract):
        old_db = self.app.db
        self.app.db = _EventDatabase()
        try:
            with patch.object(
                self.app, "is_workflow_v2_active", return_value=True
            ), patch.object(
                self.app, "record_application_lifecycle_event"
            ) as lifecycle_event, patch.object(
                self.app, "emit_business_event", return_value={"ok": True}
            ):
                self.app.emit_contract_business_event(
                    contract,
                    event_type="contract.completed",
                    occurred_at="2026-08-17T20:00:00+10:00",
                    actor_type="staff",
                    event_key="completed",
                    actor_user_id=7,
                )
                return lifecycle_event
        finally:
            self.app.db = old_db

    def test_transfer_contract_completion_does_not_use_generic_cycle_binding(self):
        lifecycle_event = self.emit({
            "id": 134,
            "student_id": "20260115",
            "contract_uid": "TRANSFER-134",
            "contract_type": "TransferApplicationContract",
        })

        lifecycle_event.assert_not_called()

    def test_ordinary_contract_completion_keeps_existing_canonical_event(self):
        lifecycle_event = self.emit({
            "id": 135,
            "student_id": "20260116",
            "contract_uid": "INITIAL-135",
            "contract_type": "NewInternationalStudentContract",
        })

        lifecycle_event.assert_called_once()
        self.assertEqual(
            "CONTRACT_SIGNED", lifecycle_event.call_args.args[1]
        )


class TransferContractWorkbenchMarkupTests(unittest.TestCase):
    def test_missing_current_school_action_is_explicit_and_contextual(self):
        workbench = importlib.import_module("student_lifecycle.workbench")
        result_type = importlib.import_module(
            "student_lifecycle.external_transfer_contract"
        ).ExternalTransferContractResult

        html = workbench.render_external_transfer_contract_action(
            result_type(
                status="needs_current_enrolment",
                contract_id=134,
                student_id="20260115",
                action_label="确认当前学校后前往 Student Profile",
                message="请先确认 authoritative Current Enrolment。",
            ),
            csrf="csrf-token",
        )

        self.assertIn("先确认当前学校", html)
        self.assertIn(
            "/students/20260115/school-enrolments?transfer_contract_id=134",
            html,
        )

    def test_ready_contract_links_to_student_profile_with_contract_context(self):
        workbench = importlib.import_module("student_lifecycle.workbench")
        result_type = importlib.import_module(
            "student_lifecycle.external_transfer_contract"
        ).ExternalTransferContractResult

        html = workbench.render_external_transfer_contract_action(
            result_type(
                status="ready",
                contract_id=134,
                student_id="20260115",
                current_enrolment_id=88,
                final_document_id=99,
                message="可以建立或恢复 Transfer Case。",
            ),
            csrf="csrf-token",
        )

        self.assertIn(
            "/students/20260115/transfer-cases?source_contract_id=134#transfer-case-create",
            html,
        )
        self.assertNotIn("<form", html)
        self.assertIn("前往 Student Profile 建立转学记录", html)


class TransferContractRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = importlib.import_module("app")
        flags_type = importlib.import_module(
            "student_case_feature_flags"
        ).StudentCaseFeatureFlags
        cls.flags = flags_type(
            read_enabled=True,
            transfer_write_enabled=True,
            external_transfer_auto_create_enabled=True,
        )

    def handler(self):
        return SimpleNamespace(
            form={"csrf": "csrf", "idempotency_key": "route-attempt"},
            user={
                "id": 7,
                "username": "tester",
                "permissions_json": '["students.view","student_cases.create"]',
            },
            require=lambda permission: True,
            student_capability_subject=lambda student_id: {"student_id": student_id},
            transfer_case_flags=lambda: self.flags,
            flash_redirect=lambda location, message, kind="success": (
                "flash", location, message, kind
            ),
            redirect=lambda location: ("redirect", location),
            error_page=lambda message, status: ("error", message, status),
        )

    def test_legacy_orchestration_route_redirects_to_student_profile(self):
        fake_db = SimpleNamespace(
            path="fixture.sqlite3",
            one=lambda sql, params=(): {"id": 134},
            audit=lambda *args, **kwargs: None,
        )

        with patch.object(self.app, "db", fake_db):
            result = self.app.CRMHandler.student_external_transfer_contract_orchestrate(
                self.handler(), "POST", "20260115", "134"
            )

        self.assertEqual(
            (
                "flash",
                "/students/20260115/transfer-cases?source_contract_id=134#transfer-case-create",
                "旧合同编排入口仅保留兼容跳转；请在 Student Profile 点击建立转学记录。",
                "warning",
            ),
            result,
        )

    def test_cross_student_contract_is_rejected_before_orchestration(self):
        fake_db = SimpleNamespace(
            path="fixture.sqlite3", one=lambda sql, params=(): None
        )

        with patch.object(self.app, "db", fake_db):
            result = self.app.CRMHandler.student_external_transfer_contract_orchestrate(
                self.handler(), "POST", "20260115", "999"
            )

        self.assertEqual("error", result[0])

    @patch("app.SchoolEnrolmentService")
    def test_confirmed_current_school_returns_to_profile_without_creating_case(
        self, enrolment_service,
    ):
        handler = self.handler()
        handler.form = {
            "csrf": "csrf",
            "school_name_snapshot": "Verified Current School",
            "reason": "Current evidence verified",
            "transfer_contract_id": "134",
        }
        handler._student_enrolment_write_allowed = lambda student_id: True
        handler._student_enrolment_school = lambda: (None, "Verified Current School")
        fake_db = SimpleNamespace(
            path="fixture.sqlite3",
            one=lambda sql, params=(): {"id": 134},
        )

        with patch.object(self.app, "db", fake_db):
            result = self.app.CRMHandler.student_school_enrolment_current(
                handler, "POST", "20260115"
            )

        self.assertEqual("flash", result[0])
        self.assertEqual(
            "/students/20260115/transfer-cases?source_contract_id=134#transfer-case-create",
            result[1],
        )
        self.assertIn("Current Enrolment已确认", result[2])
        self.assertIn("Student Profile", result[2])
        self.assertEqual("success", result[3])
        enrolment_service.return_value.confirm_current.assert_called_once()


if __name__ == "__main__":
    unittest.main()
