from __future__ import annotations

import unittest

from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.client_payments import ClientPaymentService
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.read_models import RevenueReadModels
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueReadModelTests(RevenueDatabaseFixture, unittest.TestCase):
    def _seed_authoritative_fee(self, connection, key: str):
        FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
            actor_user_id=1, actor_role="Admin", idempotency_key=f"{key}:authority"
        )
        connection.execute(
            "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-29','2026-08-29T00:00:00Z')"
        )
        result = RevenueOperationalOrchestrator(
            connection, 1, "Admin"
        ).contract_fully_executed(70)
        return result["commercial_case_uid"], result["fee_items"][0]

    def test_empty_dashboard_has_six_real_navigation_metrics_and_pending_categories(self):
        with self.connect() as connection:
            dashboard = RevenueReadModels(connection).dashboard()

        self.assertEqual("revenue.v1", dashboard["schema_version"])
        self.assertEqual(
            [
                ("open_cases", "收费与佣金档案", 0, "/revenue/cases?filter=open"),
                ("client_receivable_cents", "客户应收", 0, "/revenue/billing?filter=outstanding"),
                ("commission_forecast_cents", "佣金预测", 0, "/revenue/direct-commission?filter=forecast"),
                ("claimable_commission_cents", "可申领佣金", 0, "/revenue/direct-commission?filter=claimable"),
                ("unmatched_statement_items", "待匹配流水", 0, "/revenue/master-agent?filter=unmatched"),
                ("open_exceptions", "待处理异常", 0, "/revenue/reconciliation?filter=open-exceptions"),
            ],
            [
                (item["key"], item["label"], item["value"], item["href"])
                for item in dashboard["metrics"]
            ],
        )
        self.assertEqual([], dashboard["recent_cases"])
        self.assertEqual(
            [
                ("查看待收客户费用", "/revenue/billing?filter=outstanding", True),
                ("查看待匹配流水", "/revenue/master-agent?filter=unmatched", True),
                ("查看待处理异常", "/revenue/reconciliation?filter=open-exceptions", True),
            ],
            [
                (item["label"], item["href"], item["mobile_safe"])
                for item in dashboard["quick_actions"]
            ],
        )
        self.assertTrue(all(isinstance(item["count"], int) for item in dashboard["pending_items"]))
        self.assertTrue(all(item["href"].startswith("/revenue/") for item in dashboard["pending_items"]))

    def test_dashboard_projects_authoritative_balances_pending_work_and_recent_case(self):
        with self.connect() as connection:
            case_uid, fee = self._seed_authoritative_fee(connection, "read-model")
            ClientPaymentService(RevenueRepository(connection)).record_client_payment(
                fee_item_uid=fee["uid"],
                payment_method_code="au_personal_account",
                actual_currency_code="AUD",
                actual_amount_cents=100_000,
                allocated_base_cents=100_000,
                received_date="2026-08-30",
                external_reference="RM-100",
                evidence_document_uid=None,
                actor_user_id=1,
                actor_role="Admin",
                actor_has_revenue_access=True,
                idempotency_key="read-model-payment",
            )
            connection.execute(
                """INSERT INTO revenue_exceptions(
                       uid,exception_code,severity,aggregate_type,aggregate_uid,message,
                       status,opened_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("exc-read-model", "MISSING_EVIDENCE", "warning", "commercial_case",
                 case_uid, "Evidence required", "open", "2026-08-29T10:00:00Z"),
            )
            connection.commit()

            dashboard = RevenueReadModels(connection).dashboard()

        self.assertEqual(1, dashboard["open_cases"])
        self.assertEqual(140_000, dashboard["client_receivable_cents"])
        self.assertEqual(1, dashboard["open_exceptions"])
        self.assertEqual(1, len(dashboard["recent_cases"]))
        recent = dashboard["recent_cases"][0]
        self.assertEqual(case_uid, recent["uid"])
        self.assertEqual("S-REVENUE", recent["student_id"])
        self.assertEqual("姓名待关联", recent["student_name"])
        self.assertEqual("新客户", recent["classification"])
        self.assertEqual("K–12 学校教育", recent["business_type"])
        self.assertEqual("未关联", recent["institution"])
        self.assertEqual("—", recent["intake"])
        self.assertEqual("部分收款", recent["billing_status"])
        self.assertEqual("未建立", recent["commission_status"])
        self.assertEqual(f"/revenue/cases/{case_uid}", recent["href"])
        by_category = {item["category"]: item for item in dashboard["pending_items"]}
        self.assertEqual(1, by_category["open_exceptions"]["count"])
        self.assertEqual("/revenue/reconciliation?filter=open-exceptions", by_category["open_exceptions"]["href"])

    def test_dashboard_only_exposes_mutation_shortcuts_when_service_prerequisites_exist(self):
        with self.connect() as connection:
            self._seed_authoritative_fee(connection, "quick-action")
            dashboard = RevenueReadModels(connection).dashboard()

        actions = {item["key"]: item for item in dashboard["quick_actions"]}
        self.assertEqual("/revenue/billing#revenue-action", actions["record_client_payment"]["href"])
        self.assertFalse(actions["record_client_payment"]["mobile_safe"])

    def test_billing_workspace_uses_fee_items_and_client_allocations_without_client_invoice(self):
        with self.connect() as connection:
            _case_uid, fee = self._seed_authoritative_fee(connection, "billing-workspace")
            service = ClientPaymentService(RevenueRepository(connection))
            service.record_client_payment(
                fee_item_uid=fee["uid"], payment_method_code="wechat",
                actual_currency_code="CNY", actual_amount_cents=500_000,
                allocated_base_cents=120_000, received_date="2026-09-02",
                external_reference="WX-1", evidence_document_uid=None,
                actor_user_id=2, actor_role="Staff", actor_has_revenue_access=True,
                idempotency_key="billing-workspace-payment",
            )
            model = RevenueReadModels(connection)
            workspace = model.workspace("billing")
            dashboard = model.dashboard()

        self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM revenue_invoices").fetchone()[0])
        self.assertEqual(120_000, dashboard["client_receivable_cents"])
        self.assertEqual(1, len(workspace["rows"]))
        row = workspace["rows"][0]
        self.assertEqual(240_000, row["final_base_cents"])
        self.assertEqual(120_000, row["base_received_cents"])
        self.assertEqual(0, row["gst_collected_cents"])
        self.assertEqual(120_000, row["outstanding_base_cents"])
        self.assertEqual("部分收款", row["status"])
        self.assertNotIn("invoice_groups", workspace["action"])
        self.assertEqual(
            {"alipay", "wechat", "au_personal_account", "au_company_account"},
            {item["code"] for item in workspace["action"]["payment_methods"]},
        )

    def test_reconciliation_view_localizes_machine_exception_codes(self):
        with self.connect() as connection:
            case = RevenueService(RevenueRepository(connection)).open_case(
                100, 1, "localized-exception-case"
            )
            connection.execute(
                """INSERT INTO revenue_exceptions(
                       uid,exception_code,severity,aggregate_type,aggregate_uid,message,
                       status,opened_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("exception-localized", "STATEMENT_REVIEW", "warning", "commercial_case",
                 case["uid"], "请核对一级代理对账单金额", "open", "2026-08-29T10:00:00Z"),
            )
            rows = RevenueReadModels(connection).workspace(
                "reconciliation", {"filter": "open-exceptions"}
            )["rows"]

        self.assertEqual("对账单审核", rows[0]["reference"])
        self.assertEqual("STATEMENT_REVIEW", rows[0]["exception_code"])

    def test_case_projection_uses_authoritative_student_name_and_melbourne_time(self):
        with self.connect() as connection:
            connection.execute("ALTER TABLE students ADD COLUMN passport_first_name TEXT")
            connection.execute("ALTER TABLE students ADD COLUMN passport_last_name TEXT")
            connection.execute(
                "UPDATE students SET passport_first_name='Jimmy',passport_last_name='Zhang' "
                "WHERE student_id='S-REVENUE'"
            )
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, 1, "student-name-time-case")
            connection.execute(
                "UPDATE revenue_commercial_cases SET opened_at=? WHERE uid=?",
                ("2026-08-30T13:29:22+00:00", case["uid"]),
            )
            connection.commit()

            recent = RevenueReadModels(connection).dashboard()["recent_cases"][0]

        self.assertEqual("Jimmy Zhang", recent["student_name"])
        self.assertEqual("S-REVENUE", recent["student_id"])
        self.assertEqual("30/08/2026 23:29", recent["updated_at_display"])
        self.assertEqual("2026-08-30T13:29:22+00:00", recent["updated_at"])

    def test_requested_exception_codes_use_business_chinese_labels(self):
        expected = {
            "ambiguous_engagement_contract": "存在多份已签服务合同，请确认有效合同",
            "missing_engagement_contract": "缺少已签服务合同",
            "missing_he_application": "缺少高等教育申请资料",
            "missing_revenue_entity": "缺少佣金结算机构",
            "missing_student_authority": "缺少学生权威关联",
            "unresolved_fee_classification": "收费类型待确认",
            "payment_method_ambiguity": "支付方式待确认",
        }
        with self.connect() as connection:
            case = RevenueService(RevenueRepository(connection)).open_case(
                100, 1, "requested-exception-label-case"
            )
            for index, code in enumerate(expected, start=1):
                connection.execute(
                    """INSERT INTO revenue_exceptions(
                           uid,exception_code,severity,aggregate_type,aggregate_uid,message,
                           status,opened_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (f"exception-{index}", code, "warning", "commercial_case",
                     case["uid"], "technical message", "open", f"2026-08-29T10:00:{index:02d}Z"),
                )
            connection.commit()
            rows = RevenueReadModels(connection).workspace(
                "reconciliation", {"filter": "open-exceptions"}
            )["rows"]

        labels = {row["exception_code"]: row["reference"] for row in rows}
        self.assertEqual(expected, labels)


if __name__ == "__main__":
    unittest.main()
