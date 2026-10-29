from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import unittest

import app as app_module
from student_lifecycle.transfer_v2_models import TransferV2Overview


class TransferV2RouteTests(unittest.TestCase):
    def test_legacy_v2_cycle_detail_get_renders_read_only_without_new_projection(self):
        overview = TransferV2Overview(
            student_id="20260140",
            cycle_id=190,
            case_id=26,
            source_label="aaie_existing",
            lifecycle_stage="legacy_unconfirmed",
            lifecycle_label="Legacy Transfer · Lifecycle unconfirmed",
            is_legacy=True,
            current_school_name="Legacy School",
            current_year_level="Year 9",
            application_year_level="Year 10",
            transfer_year=2026,
            transfer_term="Term 4",
            branches=(),
            current_school_id=10,
        )

        class FakeConnection:
            def execute(self, sql, parameters):
                return self

            def fetchone(self):
                return {"id": overview.cycle_id, "v2_version": 2}

        class FakeDatabase:
            path = Path("/isolated/legacy-transfer.sqlite3")

            @staticmethod
            def connect():
                return FakeConnection()

        class FakeTransferV2Service:
            def __init__(self, *args, **kwargs):
                pass

            def load_overview(self, cycle_id):
                self.cycle_id = cycle_id
                return overview

        handler = object.__new__(app_module.CRMHandler)
        handler.user = {"id": 7}
        handler.csrf = "route-csrf"
        handler.require = lambda permission: True
        handler.transfer_case_flags = lambda: {}
        handler.transfer_v2_schema_ready = lambda: True
        handler.student_capability_subject = lambda student_id: {
            "student_id": student_id
        }
        handler.transfer_case_school_choices = lambda: ()
        handler.message = lambda: (None, None)
        handler.send_html = lambda html, headers=(): (
            200,
            dict(headers),
            html,
        )

        with mock.patch.object(app_module, "db", FakeDatabase()), \
             mock.patch.object(app_module, "TransferV2Service", FakeTransferV2Service), \
             mock.patch.object(
                 app_module,
                 "load_transfer_v2_projection",
                 side_effect=AssertionError("legacy GET entered new projection"),
             ), \
             mock.patch.object(
                 app_module,
                 "load_legacy_transfer_history",
                 return_value=None,
             ) as history_loader, \
             mock.patch.object(
                 app_module.CRMHandler,
                 "transfer_v2_action_context",
                 side_effect=AssertionError("legacy GET entered new action context"),
             ), \
             mock.patch.object(
                 app_module.student_capabilities,
                 "can_edit_transfer_case",
                 return_value=True,
             ), \
             mock.patch.object(
                 app_module.student_case_permissions,
                 "can_view_student_cases",
                 return_value=True,
             ), \
             mock.patch.object(
                 app_module,
                 "page",
                 side_effect=lambda title, body, *args, **kwargs: body,
             ), \
             mock.patch.object(app_module.nas, "status", return_value={}):
            status, headers, body = handler.student_transfer_case_detail(
                "GET", "20260140", 26
            )

        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertIn("历史转学记录", body)
        self.assertIn("只读查看", body)
        self.assertNotIn("method=\"post\"", body.lower())
        history_loader.assert_called_once_with(mock.ANY, overview.cycle_id)

    def test_v2_route_is_registered_and_guard_runs_before_module_dispatch(self):
        source = Path(app_module.__file__).read_text(encoding="utf-8")
        self.assertEqual(
            1,
            source.count(
                'transfer-cases/(\\d+)/transfer-v2/action$", self.student_transfer_v2_action'
            ),
        )
        guard = source.index("if self.block_legacy_mutation_for_transfer_v2(method)")
        module_auth = source.index("if module_key and not can_access_module", guard)
        self.assertLess(guard, module_auth)

    def test_v2_case_blocks_legacy_post_but_allows_exact_v2_action(self):
        handler = object.__new__(app_module.CRMHandler)
        errors = []
        handler.error_page = lambda message, status: errors.append((message, status))
        handler.transfer_v2_case_context = lambda case_id: {"cycle_id": 201}

        handler.parsed = SimpleNamespace(path="/students/1/transfer-cases/101/edit")
        self.assertTrue(handler.block_legacy_mutation_for_transfer_v2("POST"))
        self.assertIn("Legacy", errors[0][0])

        handler.parsed = SimpleNamespace(
            path="/students/1/transfer-cases/101/transfer-v2/action"
        )
        self.assertFalse(handler.block_legacy_mutation_for_transfer_v2("POST"))

    def test_creation_parser_accepts_library_and_manual_target_rows(self):
        captured = {}

        class FakeService:
            def __init__(self, *args, **kwargs):
                pass

            def create_cycle(self, request, **kwargs):
                captured["request"] = request
                return SimpleNamespace(case_id=101, cycle_id=201)

        handler = object.__new__(app_module.CRMHandler)
        handler.user = {"id": 7}
        handler.form = {
            "target_school_id": ["20", ""],
            "target_school_manual": ["", "Manual Future School"],
            "current_school_id": "",
            "current_school_manual": "Manual Current School",
            "source_contract_id": "501",
            "source_label": "aaie_existing",
            "current_year_level": "Year 8",
            "application_year_level": "Year 9",
            "transfer_year": "2027",
            "transfer_term": "Term 1",
            "idempotency_key": "create-1",
        }
        handler.transfer_case_school_choices = lambda: (
            {
                "id": 20,
                "name": "Library Target",
                "state": "VIC",
                "city_name": "Melbourne",
                "sector_key": "private",
            },
        )
        handler.flash_redirect = lambda url, message: (url, message)

        with mock.patch.object(app_module, "TransferV2Service", FakeService), \
             mock.patch.object(app_module, "reconcile_transfer_v2_reminders"):
            result = handler.student_transfer_v2_create_post("S1")

        request = captured["request"]
        self.assertEqual("Manual Current School", request.current_school_name)
        self.assertEqual(
            [(20, "Library Target"), (None, "Manual Future School")],
            [(item.school_id, item.school_name) for item in request.targets],
        )
        self.assertIn("/transfer-cases/101", result[0])

    def test_creation_parser_validates_selected_current_school_name(self):
        captured = {}

        class FakeService:
            def __init__(self, *args, **kwargs):
                pass

            def create_cycle(self, request, **kwargs):
                captured["request"] = request
                return SimpleNamespace(case_id=101, cycle_id=201)

        handler = object.__new__(app_module.CRMHandler)
        handler.user = {"id": 7}
        handler.form = {
            "target_school_id": "20",
            "target_school_name": "Library Target",
            "current_school_id": "10",
            "current_school_name": "Library Current",
            "source_contract_id": "501",
            "source_label": "aaie_existing",
            "current_year_level": "Year 8",
            "application_year_level": "Year 9",
            "transfer_year": "2027",
            "transfer_term": "Term 1",
            "idempotency_key": "create-library-current",
        }
        handler.transfer_case_school_choices = lambda: (
            {
                "id": 10,
                "name": "Library Current",
                "state": "VIC",
                "city_name": "Melbourne",
                "sector_key": "public",
            },
            {
                "id": 20,
                "name": "Library Target",
                "state": "NSW",
                "city_name": "Sydney",
                "sector_key": "private",
            },
        )
        handler.flash_redirect = lambda url, message: (url, message)

        with mock.patch.object(app_module, "TransferV2Service", FakeService), \
             mock.patch.object(app_module, "reconcile_transfer_v2_reminders"):
            handler.student_transfer_v2_create_post("S1")

        request = captured["request"]
        self.assertEqual(10, request.current_school_id)
        self.assertEqual("Library Current", request.current_school_name)
        self.assertEqual("VIC", request.current_school_state)
        self.assertEqual("Melbourne", request.current_school_region)
        self.assertEqual("Government", request.current_school_type)

    def test_add_target_school_action_appends_branch_to_existing_cycle(self):
        captured = {}

        class FakeService:
            def __init__(self, *args, **kwargs):
                pass

            def add_target_branch(self, cycle_id, **kwargs):
                captured["cycle_id"] = cycle_id
                captured.update(kwargs)

        class FakeNoop:
            def __init__(self, *args, **kwargs):
                pass

        handler = object.__new__(app_module.CRMHandler)
        handler.user = {"id": 7}
        handler.form = {
            "csrf": "csrf",
            "action": "add_target_school",
            "cycle_id": "201",
            "idempotency_key": "append-target-1",
            "target_school_id": "30",
            "target_school_name": "Target High",
            "target_school_state": "",
            "target_school_region": "",
            "target_school_type": "",
        }
        handler.require = lambda permission: True
        handler.transfer_case_flags = lambda: {}
        handler.student_capability_subject = lambda student_id: {"student_id": student_id}
        handler.transfer_v2_case_context = lambda case_id, student_id: {
            "cycle_id": 201,"student_id": student_id,
        }
        handler.transfer_case_selected_school = lambda school_id, name: {
            "id": school_id,"name": name,"state": "NSW","region": "Sydney",
            "school_type": "Government",
        }
        handler.error_page = lambda message, status: self.fail(message)
        handler.flash_redirect = lambda url, message: (url,message)

        with mock.patch.object(app_module, "TransferV2Service", FakeService), \
             mock.patch.object(app_module, "TransferV2AdmissionsService", FakeNoop), \
             mock.patch.object(app_module, "TransferV2CompletionService", FakeNoop), \
             mock.patch.object(app_module, "TransferV2DocumentService", FakeNoop), \
             mock.patch.object(
                 app_module.student_capabilities,"can_edit_transfer_case",return_value=True
             ), \
             mock.patch.object(app_module, "reconcile_transfer_v2_reminders"):
            result = handler.student_transfer_v2_action("POST","S1",101)

        self.assertEqual(201,captured["cycle_id"])
        self.assertEqual(30,captured["target"].school_id)
        self.assertEqual("Target High",captured["target"].school_name)
        self.assertEqual("append-target-1",captured["idempotency_key"])
        self.assertIn("/transfer-cases/101",result[0])


if __name__ == "__main__":
    unittest.main()
