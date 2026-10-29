from __future__ import annotations

import json
import unittest

from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.read_models import RevenueReadModels
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from revenue.workbench import RevenueWorkbench
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueOperationalVisibilityTests(RevenueDatabaseFixture, unittest.TestCase):
    def enable_student_visibility_authority(self, connection):
        connection.execute("ALTER TABLE students ADD COLUMN deleted_at TEXT")
        connection.execute("ALTER TABLE students ADD COLUMN form_data_json TEXT")
        connection.execute("ALTER TABLE students ADD COLUMN passport_first_name TEXT")
        connection.execute("ALTER TABLE students ADD COLUMN passport_last_name TEXT")
        connection.execute(
            """UPDATE students
                  SET form_data_json='{}',passport_first_name='Active',passport_last_name='Student'
                WHERE student_id='S-REVENUE'"""
        )

    @staticmethod
    def add_case(
        connection,
        *,
        cycle_id: int,
        student_id: str,
        first_name: str,
        last_name: str,
        deleted_at: str | None = None,
        metadata: dict | None = None,
    ):
        connection.execute(
            """INSERT INTO students(
                   student_id,deleted_at,form_data_json,passport_first_name,passport_last_name
               ) VALUES(?,?,?,?,?)""",
            (
                student_id,
                deleted_at,
                json.dumps(metadata or {}, sort_keys=True),
                first_name,
                last_name,
            ),
        )
        connection.execute(
            """INSERT INTO application_cycles(
                   id,cycle_uid,student_id,cycle_type,status,education_stream,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                cycle_id,
                f"cycle-{cycle_id}",
                student_id,
                "initial",
                "active",
                "school_education",
                "2026-08-31T00:00:00Z",
            ),
        )
        return RevenueService(RevenueRepository(connection)).open_case(
            cycle_id, 1, f"case-{cycle_id}"
        )

    def test_current_case_surfaces_share_student_visibility_without_name_guessing(self):
        with self.connect() as connection:
            self.enable_student_visibility_authority(connection)
            service = RevenueService(RevenueRepository(connection))
            active_case = service.open_case(100, 1, "visible-active-case")
            deleted_case = self.add_case(
                connection,
                cycle_id=101,
                student_id="S-DELETED",
                first_name="Deleted",
                last_name="Student",
                deleted_at="2026-08-30T00:00:00Z",
            )
            synthetic_case = self.add_case(
                connection,
                cycle_id=102,
                student_id="S-SYNTHETIC",
                first_name="Ordinary",
                last_name="Name",
                metadata={"synthetic_acceptance": True},
            )
            name_only_test_case = self.add_case(
                connection,
                cycle_id=103,
                student_id="S-NAME-TEST",
                first_name="Test",
                last_name="Family",
            )
            connection.commit()

            models = RevenueReadModels(connection)
            dashboard = models.dashboard()
            case_workspace = models.workspace("cases", {})

        expected_uids = {active_case["uid"], name_only_test_case["uid"]}
        self.assertEqual(2, dashboard["open_cases"])
        self.assertEqual(expected_uids, {row["uid"] for row in dashboard["recent_cases"]})
        self.assertEqual(expected_uids, {row["uid"] for row in case_workspace["rows"]})
        self.assertEqual(2, case_workspace["coverage_cards"][0]["count"])
        self.assertNotIn(deleted_case["uid"], expected_uids)
        self.assertNotIn(synthetic_case["uid"], expected_uids)

    def test_deleted_student_is_excluded_from_contextual_case_lookup(self):
        with self.connect() as connection:
            self.enable_student_visibility_authority(connection)
            active_case = RevenueService(RevenueRepository(connection)).open_case(
                100, 1, "context-active-case"
            )
            deleted_case = self.add_case(
                connection,
                cycle_id=101,
                student_id="S-DELETED-CONTEXT",
                first_name="Deleted",
                last_name="Context",
                deleted_at="2026-08-30T00:00:00Z",
            )
            connection.commit()
            models = RevenueReadModels(connection)

            active_href = models.contextual_case_href("student", "S-REVENUE")
            deleted_student_href = models.contextual_case_href("student", "S-DELETED-CONTEXT")
            deleted_cycle_href = models.contextual_case_href("cycle", 101)

        self.assertEqual(f"/revenue/cases/{active_case['uid']}", active_href)
        self.assertIsNone(deleted_student_href)
        self.assertIsNone(deleted_cycle_href)
        self.assertNotEqual(f"/revenue/cases/{deleted_case['uid']}", deleted_student_href)

    def test_deleted_student_is_excluded_from_school_contextual_case_lookup(self):
        with self.connect() as connection:
            self.enable_student_visibility_authority(connection)
            self.seed_component_authorities(connection)
            case = self.add_case(
                connection,
                cycle_id=101,
                student_id="S-DELETED-SCHOOL-CONTEXT",
                first_name="Deleted",
                last_name="School Context",
                deleted_at="2026-08-30T00:00:00Z",
            )
            RevenueService(RevenueRepository(connection)).link_enrolment_component(
                case["uid"],
                "school",
                "school_enrolment",
                204,
                1,
                "deleted-school-context-component",
                final_acceptance_id=201,
                school_enrolment_id=204,
                coe_oshc_id=202,
            )
            connection.commit()

            href = RevenueReadModels(connection).contextual_case_href("school", 10)

        self.assertIsNone(href)

    def test_deleted_student_unbilled_fee_is_not_an_operational_invoice_candidate(self):
        with self.connect() as connection:
            self.enable_student_visibility_authority(connection)
            FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
                actor_user_id=1,
                actor_role="Admin",
                idempotency_key="visibility-fee-authority",
            )
            case = self.add_case(
                connection,
                cycle_id=101,
                student_id="S-DELETED-BILLING",
                first_name="Deleted",
                last_name="Billing",
                deleted_at="2026-08-30T00:00:00Z",
            )
            service = RevenueService(RevenueRepository(connection))
            account = service.create_billing_account(
                "family", "Deleted Billing Family", 1, "visibility-account"
            )
            service.create_fee_items(
                case["uid"],
                account["uid"],
                [{
                    "milestone_code": "application",
                    "description": "Retained unbilled fee",
                    "net_cents": 100_000,
                    "gst_cents": 10_000,
                }],
                1,
                "visibility-fee-item",
                "fee-rule-v1",
            )
            connection.commit()

            billing = RevenueReadModels(connection).workspace("billing", {})

        self.assertIsNone(billing["action"])

    def test_deleted_student_financial_history_remains_available_by_exact_admin_case(self):
        with self.connect() as connection:
            self.enable_student_visibility_authority(connection)
            FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
                actor_user_id=1,
                actor_role="Admin",
                idempotency_key="history-fee-authority",
            )
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, 1, "history-case")
            account = service.create_billing_account(
                "family", "Historical Family", 1, "history-account"
            )
            item = service.create_fee_items(
                case["uid"],
                account["uid"],
                [{
                    "milestone_code": "application",
                    "description": "Historical fee",
                    "net_cents": 100_000,
                    "gst_cents": 10_000,
                }],
                1,
                "history-fee-item",
                "fee-rule-v1",
            )[0]
            invoice = service.issue_invoice(
                [item["uid"]],
                1,
                "history-invoice",
                "2026-08-31",
                "2026-09-07",
                planned_payment_allocations=[{"method_code": "au_company_account"}],
            )
            connection.execute(
                "UPDATE students SET deleted_at='2026-08-31T01:00:00Z' WHERE student_id='S-REVENUE'"
            )
            connection.commit()

            models = RevenueReadModels(connection)
            dashboard = models.dashboard()
            workbench = RevenueWorkbench(connection).load(case["uid"])

        self.assertEqual(0, dashboard["open_cases"])
        self.assertEqual([], dashboard["recent_cases"])
        financial_rows = next(
            section["rows"]
            for section in workbench["sections"]
            if section["key"] == "invoice_payment_receipt"
        )
        self.assertEqual(invoice["uid"], financial_rows[0]["uid"])

    def test_case_linked_current_exceptions_follow_operational_visibility(self):
        with self.connect() as connection:
            self.enable_student_visibility_authority(connection)
            service = RevenueService(RevenueRepository(connection))
            active_case = service.open_case(100, 1, "exception-active-case")
            deleted_case = self.add_case(
                connection,
                cycle_id=101,
                student_id="S-DELETED-EXCEPTION",
                first_name="Deleted",
                last_name="Exception",
                deleted_at="2026-08-30T00:00:00Z",
            )
            for index, case in enumerate((active_case, deleted_case), start=1):
                connection.execute(
                    """INSERT INTO revenue_exceptions(
                           uid,exception_code,severity,aggregate_type,aggregate_uid,message,
                           status,opened_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        f"visibility-exception-{index}",
                        "MISSING_EVIDENCE",
                        "warning",
                        "commercial_case",
                        case["uid"],
                        "Evidence required",
                        "open",
                        f"2026-08-31T00:00:0{index}Z",
                    ),
                )
            connection.commit()

            models = RevenueReadModels(connection)
            dashboard = models.dashboard()
            reconciliation = models.workspace(
                "reconciliation", {"filter": "open-exceptions"}
            )

        self.assertEqual(1, dashboard["open_exceptions"])
        self.assertEqual(
            ["visibility-exception-1"],
            [row["uid"] for row in reconciliation["rows"]],
        )


if __name__ == "__main__":
    unittest.main()
