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
            case_uid, fee = self._seed_authoritative_fee(connection, "student-name-time-case")
            connection.execute(
                "UPDATE revenue_fee_items SET created_at=? WHERE uid=?",
                ("2026-08-30T13:29:22+00:00", fee["uid"]),
            )
            connection.commit()

            recent = RevenueReadModels(connection).dashboard()["recent_cases"][0]

        self.assertEqual("Jimmy Zhang", recent["student_name"])
        self.assertEqual("S-REVENUE", recent["student_id"])
        self.assertEqual("30/08/2026 23:29", recent["updated_at_display"])
        self.assertEqual("2026-08-30T13:29:22+00:00", recent["updated_at"])

    def test_recent_activity_excludes_empty_cases_and_prefers_confirmed_payment_time(self):
        with self.connect() as connection:
            case_uid, fee = self._seed_authoritative_fee(connection, "recent-activity-fee")
            connection.execute(
                "UPDATE revenue_commercial_cases SET opened_at='2026-08-01T03:00:00Z' WHERE uid=?",
                (case_uid,),
            )
            connection.execute(
                "UPDATE revenue_fee_items SET created_at='2026-08-02T03:00:00Z' WHERE uid=?",
                (fee["uid"],),
            )
            self.seed_prior_existing_cycle(connection)
            empty_case = RevenueService(RevenueRepository(connection)).open_case(90, 1, "recent-empty-case")
            connection.execute(
                "UPDATE revenue_commercial_cases SET opened_at='2030-01-01T00:00:00Z' WHERE uid=?",
                (empty_case["uid"],),
            )
            ClientPaymentService(RevenueRepository(connection)).record_client_payment(
                fee_item_uid=fee["uid"], payment_method_code="au_personal_account",
                actual_currency_code="AUD", actual_amount_cents=100_000, allocated_base_cents=100_000,
                received_date="2026-08-30", external_reference="RECENT-PAY",
                evidence_document_uid=None, actor_user_id=1, actor_role="Admin",
                actor_has_revenue_access=True, idempotency_key="recent-confirmed-payment",
            )
            connection.execute("UPDATE revenue_payments SET confirmed_at='2031-09-04T23:00:00+10:00'")
            connection.commit()
            model = RevenueReadModels(connection)
            actual = model.dashboard()["recent_cases"]
            self.assertEqual([case_uid], [row["uid"] for row in actual])
            self.assertEqual("2031-09-04T23:00:00+10:00", actual[0]["updated_at"])
            self.assertEqual("04/09/2031 23:00", actual[0]["updated_at_display"])
            connection.execute("UPDATE revenue_payments SET status='reversed'")
            self.assertEqual("2026-08-02T03:00:00Z", model.dashboard()["recent_cases"][0]["updated_at"])

    def test_recent_activity_limits_to_ten_financial_updates_in_time_order(self):
        with self.connect() as connection:
            service = RevenueService(RevenueRepository(connection))
            cases = {}
            for day in range(1, 13):
                student_id = f"S-{day}"
                cycle_id = 100 + day
                connection.execute("INSERT INTO students VALUES (?)", (student_id,))
                connection.execute(
                    "INSERT INTO application_cycles VALUES(?,?,?,'initial','active','school_education','2026-08-01')",
                    (cycle_id, f"recent-cycle-{day}", student_id),
                )
                case = service.open_case(cycle_id, 1, f"recent-case-{day}")
                cases[day] = case["uid"]
                self.seed_dashboard_invoice(connection, case["id"], f"2026-08-{day:02d}T12:00:00+10:00")
            connection.commit()

            recent = RevenueReadModels(connection).dashboard()["recent_cases"]
            self.assertEqual([cases[day] for day in range(12, 2, -1)], [row["uid"] for row in recent])

    def test_recent_intake_distinguishes_confirmed_enrolment_offer_and_preference(self):
        with self.connect() as connection:
            case = RevenueService(RevenueRepository(connection)).open_case(100, 1, "recent-intake")
            self.seed_dashboard_invoice(connection, case["id"])
            connection.executescript("""
                ALTER TABLE students ADD preferred_intake_year TEXT;
                ALTER TABLE students ADD preferred_intake_term TEXT;
                UPDATE students SET preferred_intake_year='2027', preferred_intake_term='07';
                ALTER TABLE student_school_enrolments ADD student_id TEXT;
                ALTER TABLE student_school_enrolments ADD start_date TEXT;
                ALTER TABLE student_school_enrolments ADD status TEXT;
                ALTER TABLE cycle_final_acceptances ADD acceptance_status TEXT;
                ALTER TABLE offer_received_events ADD legacy_offer_id INTEGER;
                CREATE TABLE offers (id INTEGER PRIMARY KEY, application_cycle_id INTEGER,
                  student_id TEXT, course_start_date TEXT, offer_status TEXT);
            """)
            model = RevenueReadModels(connection)

            def intake():
                entry = model.dashboard()["recent_cases"][0]
                return entry["intake"], entry["intake_source"]

            self.assertEqual(("2027 年 7 月", "意向入学"), intake())
            connection.execute("INSERT INTO offers VALUES(1,100,'S-REVENUE','2027-02-03','Received')")
            connection.execute("INSERT INTO offers VALUES(2,999,'S-REVENUE','2099-01-01','Received')")
            self.assertEqual(("2027-02-03", "Offer 开学"), intake())
            connection.execute("INSERT INTO offers VALUES(3,100,'S-REVENUE','2027-03-03','Received')")
            self.assertEqual(("2027 年 7 月", "意向入学"), intake())
            connection.execute("INSERT INTO offer_received_events(id,application_cycle_id,legacy_offer_id) VALUES(11,100,3)")
            connection.execute("""INSERT INTO cycle_final_acceptances(id,application_cycle_id,
                                offer_received_event_id,acceptance_status) VALUES(21,100,11,'active')""")
            self.assertEqual(("2027-03-03", "已接受 Offer 开学"), intake())
            connection.execute("""INSERT INTO student_school_enrolments(
                                  id,application_cycle_id,student_id,start_date,status)
                                  VALUES(31,100,'S-REVENUE','2027-03-05','active')""")
            self.assertEqual(("2027-03-05", "已确认入学"), intake())

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
