from __future__ import annotations

import unittest
from unittest.mock import patch

import app
from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.read_models import RevenueReadModels
from revenue.repository import RevenueRepository
from revenue.views import render_revenue_workspace_page
from revenue.workbench import RevenueWorkbench
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueStudentContextReadModelTests(RevenueDatabaseFixture, unittest.TestCase):
    def _case_with_fee(self, connection):
        FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
            actor_user_id=1,
            actor_role="Admin",
            idempotency_key="student-context-rule-authority",
        )
        connection.execute(
            "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-09-01','2026-09-02T12:33:44Z')"
        )
        result = RevenueOperationalOrchestrator(
            connection, 1, "Admin"
        ).contract_fully_executed(70)
        connection.commit()
        return result

    def _seed_historical_payment_and_receipt(self, connection, result):
        fee_uid = result["fee_items"][0]["uid"]
        fee = connection.execute(
            "SELECT id,billing_account_id,net_cents FROM revenue_fee_items WHERE uid=?",
            (fee_uid,),
        ).fetchone()
        method = connection.execute(
            """SELECT m.id method_id,v.id version_id,m.method_code
                 FROM revenue_payment_methods m
                 JOIN revenue_payment_method_rule_versions v
                   ON v.payment_method_id=m.id
             ORDER BY v.id LIMIT 1"""
        ).fetchone()
        connection.execute(
            """INSERT INTO revenue_documents(
                   id,uid,document_kind,version_no,storage_path,sha256,mime_type,
                   size_bytes,source_kind,created_by,created_at
               ) VALUES(80,'evidence-historical','client_payment_evidence',1,
                        '/staging/evidence.jpg',?,'image/jpeg',4,'upload',1,
                        '2026-09-04T00:00:00Z')""",
            ("a" * 64,),
        )
        connection.execute(
            """INSERT INTO revenue_documents(
                   id,uid,document_kind,version_no,storage_path,sha256,mime_type,
                   size_bytes,source_kind,created_by,created_at
               ) VALUES(81,'receipt-historical','client_receipt',1,
                        '/staging/receipt.pdf',?,'application/pdf',4,'generated',1,
                        '2026-09-04T00:00:01Z')""",
            ("b" * 64,),
        )
        connection.execute(
            """INSERT INTO revenue_payments(
                   id,uid,payment_direction,counterparty_kind,billing_account_id,
                   payment_method_id,received_date,amount_cents,allocated_cents,
                   currency_code,status,evidence_document_id,confirmed_by,confirmed_at
               ) VALUES(80,'payment-historical','received','client',?,?,'2026-09-04',
                        ?,?,'AUD','confirmed',80,1,'2026-09-04T00:00:00Z')""",
            (fee["billing_account_id"], method["method_id"], fee["net_cents"], fee["net_cents"]),
        )
        connection.execute(
            """INSERT INTO revenue_client_payment_allocations(
                   id,uid,payment_id,fee_item_id,allocated_base_cents,gst_cents,
                   gross_charge_aud_cents,payment_method_rule_version_id,
                   method_code_snapshot,gst_rate_basis_points,created_by,created_at
               ) VALUES(80,'allocation-historical',80,?,?,0,?,?,?,0,1,
                        '2026-09-04T00:00:00Z')""",
            (fee["id"], fee["net_cents"], fee["net_cents"], method["version_id"], method["method_code"]),
        )
        connection.execute(
            """INSERT INTO revenue_receipts(
                   id,uid,receipt_number,payment_id,amount_cents,currency_code,
                   document_id,issued_by,issued_at
               ) VALUES(80,'receipt-row-historical','R-HISTORICAL',80,?,'AUD',81,1,
                        '2026-09-04T00:00:01Z')""",
            (fee["net_cents"],),
        )
        connection.commit()

    @staticmethod
    def _soft_delete_student(connection, student_id="S-REVENUE"):
        connection.execute("ALTER TABLE students ADD COLUMN deleted_at TEXT")
        connection.execute(
            "UPDATE students SET deleted_at='2026-09-12T00:00:00Z' WHERE student_id=?",
            (student_id,),
        )
        connection.commit()

    def test_historical_case_keeps_detail_billing_payment_evidence_and_receipt_access(self):
        with self.connect() as connection:
            result = self._case_with_fee(connection)
            case_uid = result["commercial_case_uid"]
            fee_uid = result["fee_items"][0]["uid"]
            self._seed_historical_payment_and_receipt(connection, result)
            self._soft_delete_student(connection)

            detail = RevenueWorkbench(connection).load(case_uid)
            scoped = RevenueReadModels(connection).workspace(
                "billing", {"item": fee_uid}, case_uid=case_uid
            )
            global_data = RevenueReadModels(connection).workspace("billing", {})

        self.assertEqual(case_uid, detail["case"]["uid"])
        self.assertEqual(
            ["/revenue/documents/evidence-historical/view"],
            [row["evidence_view_url"] for row in detail["payment_history"]],
        )
        self.assertEqual(["R-HISTORICAL"], [row["receipt_number"] for row in detail["receipt_records"]])
        self.assertEqual(
            {
                "/revenue/documents/evidence-historical/view",
                "/revenue/documents/receipt-historical/view",
            },
            {row["view_url"] for row in detail["evidence_records"]},
        )
        self.assertEqual([fee_uid], [row["uid"] for row in scoped["rows"]])
        self.assertEqual(["payment-historical"], [
            row["payment_uid"] for row in scoped["action"]["payment_history"]
        ])
        self.assertEqual("evidence-historical", scoped["action"]["payment_history"][0]["evidence_document_uid"])
        self.assertEqual("receipt-historical", scoped["action"]["payment_history"][0]["receipt_document_uid"])
        self.assertEqual([], global_data["rows"])

    def test_deleted_student_without_revenue_case_cannot_create_case_scope(self):
        with self.connect() as connection:
            connection.execute("INSERT INTO students VALUES('S-DELETED')")
            self._soft_delete_student(connection, "S-DELETED")
            with self.assertRaises(LookupError):
                RevenueReadModels(connection).workspace(
                    "billing", {}, case_uid="case-for-deleted-student"
                )

    def test_case_links_and_scoped_billing_keep_the_authoritative_case_context(self):
        with self.connect() as connection:
            result = self._case_with_fee(connection)
            case_uid = result["commercial_case_uid"]
            fee_uid = result["fee_items"][0]["uid"]
            case_data = RevenueWorkbench(connection).load(case_uid)
            scoped = RevenueReadModels(connection).workspace(
                "billing", {"item": fee_uid}, case_uid=case_uid
            )
            global_data = RevenueReadModels(connection).workspace(
                "billing", {"item": fee_uid}
            )

        scoped_url = f"/revenue/cases/{case_uid}/billing"
        actions = {item["label"]: item["href"] for item in case_data["actions"]}
        self.assertEqual(scoped_url, actions["客户收费"])
        self.assertEqual(
            f"{scoped_url}?item={fee_uid}#revenue-action",
            case_data["client_billing_summary"][0]["add_payment_href"],
        )
        self.assertEqual("S-REVENUE", scoped["student_context"]["student_id"])
        self.assertEqual(case_uid, scoped["student_context"]["case_uid"])
        self.assertEqual(
            f"{scoped_url}/payment", scoped["action"]["payment_action"]
        )
        self.assertEqual(
            f"{scoped_url}/receipt", scoped["action"]["receipt_action"]
        )
        self.assertEqual(
            [fee_uid],
            [item["fee_item_uid"] for item in scoped["action"]["payment_options"]],
        )

        scoped_html = render_revenue_workspace_page(scoped, "csrf", "token")
        self.assertIn(f'action="{scoped_url}/payment"', scoped_html)
        self.assertIn(f'href="/revenue/cases/{case_uid}"', scoped_html)
        self.assertIn("S-REVENUE", scoped_html)

        self.assertNotIn("student_context", global_data)
        self.assertEqual(
            "/revenue/billing/payment", global_data["action"]["payment_action"]
        )
        global_html = render_revenue_workspace_page(global_data, "csrf", "token")
        self.assertIn('action="/revenue/billing/payment"', global_html)

    def test_unknown_case_scope_fails_closed(self):
        with self.connect() as connection:
            with self.assertRaises(LookupError):
                RevenueReadModels(connection).workspace(
                    "billing", {}, case_uid="case-does-not-exist"
                )

    def test_two_student_scopes_never_mix_fee_payment_or_receipt_options(self):
        with self.connect() as connection:
            first = self._case_with_fee(connection)
            first_case_uid = first["commercial_case_uid"]
            first_fee_uid = first["fee_items"][0]["uid"]
            first_fee_id = connection.execute(
                "SELECT id FROM revenue_fee_items WHERE uid=?", (first_fee_uid,)
            ).fetchone()[0]
            first_account_id = connection.execute(
                "SELECT billing_account_id FROM revenue_fee_items WHERE id=?",
                (first_fee_id,),
            ).fetchone()[0]
            connection.execute("INSERT INTO students VALUES('S-OTHER')")
            connection.execute(
                "INSERT INTO application_cycles VALUES(101,'cycle-101','S-OTHER','initial','active','school_education','2026-09-03')"
            )
            connection.execute(
                "INSERT INTO contracts VALUES(71,'S-OTHER',101,'FullyExecuted','2026-09-03','2026-09-03T12:33:44Z')"
            )
            second = RevenueOperationalOrchestrator(
                connection, 1, "Admin"
            ).contract_fully_executed(71)
            second_case_uid = second["commercial_case_uid"]
            second_fee_uid = second["fee_items"][0]["uid"]
            method = connection.execute(
                """SELECT m.id method_id,v.id version_id,m.method_code
                     FROM revenue_payment_methods m
                     JOIN revenue_payment_method_rule_versions v
                       ON v.payment_method_id=m.id
                 ORDER BY v.id LIMIT 1"""
            ).fetchone()
            connection.execute(
                """INSERT INTO revenue_payments(
                       id,uid,payment_direction,counterparty_kind,billing_account_id,
                       payment_method_id,received_date,amount_cents,allocated_cents,
                       currency_code,status,confirmed_by,confirmed_at
                   ) VALUES(80,'payment-first','received','client',?,?,
                            '2026-09-04',100,100,'AUD','confirmed',1,
                            '2026-09-04T00:00:00Z')""",
                (first_account_id, method["method_id"]),
            )
            connection.execute(
                """INSERT INTO revenue_client_payment_allocations(
                       id,uid,payment_id,fee_item_id,allocated_base_cents,gst_cents,
                       gross_charge_aud_cents,payment_method_rule_version_id,
                       method_code_snapshot,gst_rate_basis_points,created_by,created_at
                   ) VALUES(80,'allocation-first',80,?,100,0,100,?,?,0,1,
                            '2026-09-04T00:00:00Z')""",
                (first_fee_id, method["version_id"], method["method_code"]),
            )
            connection.commit()

            first_scope = RevenueReadModels(connection).workspace(
                "billing", {}, case_uid=first_case_uid
            )
            second_scope = RevenueReadModels(connection).workspace(
                "billing", {}, case_uid=second_case_uid
            )

        self.assertEqual(
            [first_fee_uid],
            [item["fee_item_uid"] for item in first_scope["action"]["payment_options"]],
        )
        self.assertEqual(
            ["payment-first"],
            [item["payment_uid"] for item in first_scope["action"]["receipt_options"]],
        )
        self.assertEqual(
            ["payment-first"],
            [item["payment_uid"] for item in first_scope["action"]["payment_history"]],
        )
        self.assertEqual(
            [second_fee_uid],
            [item["fee_item_uid"] for item in second_scope["action"]["payment_options"]],
        )
        self.assertEqual([], second_scope["action"]["receipt_options"])
        self.assertEqual([], second_scope["action"]["payment_history"])


class RevenueStudentContextActionTests(RevenueDatabaseFixture, unittest.TestCase):
    def _case_with_fee(self, connection):
        FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
            actor_user_id=1,
            actor_role="Admin",
            idempotency_key="student-context-action-rule-authority",
        )
        connection.execute(
            "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-09-01','2026-09-02T12:33:44Z')"
        )
        result = RevenueOperationalOrchestrator(
            connection, 1, "Admin"
        ).contract_fully_executed(70)
        connection.commit()
        return result

    def test_scoped_payment_success_redirects_to_the_same_case(self):
        connection = self.connect()
        result = self._case_with_fee(connection)
        case_uid = result["commercial_case_uid"]
        fee_uid = result["fee_items"][0]["uid"]
        connection.execute("ALTER TABLE students ADD COLUMN deleted_at TEXT")
        connection.execute(
            "UPDATE students SET deleted_at='2026-09-12T00:00:00Z' WHERE student_id='S-REVENUE'"
        )
        connection.commit()
        responses = []

        class Actions:
            def __init__(self, *_args, **_kwargs):
                pass

            @staticmethod
            def record_client_payment(form, _upload):
                self.assertEqual(fee_uid, form["fee_item_uid"])
                return {"uid": "payment-student-context"}

        class Handler:
            user = {"id": 1, "role_name": "Admin"}
            headers = {"Accept": "application/json", "X-Requested-With": "fetch"}
            files = []
            form = {"fee_item_uid": fee_uid}

            @staticmethod
            def json_response(payload, status=200):
                responses.append((payload, status))
                return payload

            @staticmethod
            def flash_redirect(*_args):
                raise AssertionError("fetch response must stay JSON")

        with (
            patch("app.db.connect", return_value=connection),
            patch("app.db._release_connection"),
            patch("app.RevenueUIActions", Actions),
        ):
            payload = app.CRMHandler._execute_revenue_action(
                Handler(), "billing/payment", scoped_case_uid=case_uid
            )

        self.assertEqual(
            f"/revenue/cases/{case_uid}", payload["redirect"]
        )
        self.assertEqual(payload, responses[0][0])
        self.assertTrue(connection.in_transaction is False)

    def test_scoped_payment_rejects_a_case_that_does_not_own_the_fee(self):
        connection = self.connect()
        result = self._case_with_fee(connection)
        fee_uid = result["fee_items"][0]["uid"]
        connection.execute("INSERT INTO students VALUES('S-OTHER')")
        connection.execute(
            "INSERT INTO application_cycles VALUES(101,'cycle-101','S-OTHER','initial','active','school_education','2026-09-03')"
        )
        connection.execute(
            "INSERT INTO contracts VALUES(71,'S-OTHER',101,'FullyExecuted','2026-09-03','2026-09-03T12:33:44Z')"
        )
        other_result = RevenueOperationalOrchestrator(
            connection, 1, "Admin"
        ).contract_fully_executed(71)
        other_case_uid = other_result["commercial_case_uid"]
        connection.commit()

        class Actions:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("cross-case action must not reach the write service")

        class Handler:
            user = {"id": 1, "role_name": "Admin"}
            headers = {}
            files = []
            form = {"fee_item_uid": fee_uid}

        with (
            patch("app.db.connect", return_value=connection),
            patch("app.db._release_connection"),
            patch("app.RevenueUIActions", Actions),
        ):
            with self.assertRaises(LookupError):
                app.CRMHandler._execute_revenue_action(
                    Handler(), "billing/payment", scoped_case_uid=other_case_uid
                )

        self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM revenue_payments").fetchone()[0])
        self.assertEqual(
            0,
            connection.execute(
                "SELECT COUNT(*) FROM revenue_client_payment_allocations"
            ).fetchone()[0],
        )

    def test_scoped_receipt_rejects_a_real_other_case_payment(self):
        connection = self.connect()
        first = self._case_with_fee(connection)
        first_fee_uid = first["fee_items"][0]["uid"]
        first_fee = connection.execute(
            "SELECT id,billing_account_id FROM revenue_fee_items WHERE uid=?",
            (first_fee_uid,),
        ).fetchone()
        connection.execute("INSERT INTO students VALUES('S-OTHER')")
        connection.execute(
            "INSERT INTO application_cycles VALUES(101,'cycle-101','S-OTHER','initial','active','school_education','2026-09-03')"
        )
        connection.execute(
            "INSERT INTO contracts VALUES(71,'S-OTHER',101,'FullyExecuted','2026-09-03','2026-09-03T12:33:44Z')"
        )
        second = RevenueOperationalOrchestrator(
            connection, 1, "Admin"
        ).contract_fully_executed(71)
        method = connection.execute(
            """SELECT m.id method_id,v.id version_id,m.method_code
                 FROM revenue_payment_methods m
                 JOIN revenue_payment_method_rule_versions v
                   ON v.payment_method_id=m.id
             ORDER BY v.id LIMIT 1"""
        ).fetchone()
        connection.execute(
            """INSERT INTO revenue_payments(
                   id,uid,payment_direction,counterparty_kind,billing_account_id,
                   payment_method_id,received_date,amount_cents,allocated_cents,
                   currency_code,status,confirmed_by,confirmed_at
               ) VALUES(80,'payment-first','received','client',?,?,
                        '2026-09-04',100,100,'AUD','confirmed',1,
                        '2026-09-04T00:00:00Z')""",
            (first_fee["billing_account_id"], method["method_id"]),
        )
        connection.execute(
            """INSERT INTO revenue_client_payment_allocations(
                   id,uid,payment_id,fee_item_id,allocated_base_cents,gst_cents,
                   gross_charge_aud_cents,payment_method_rule_version_id,
                   method_code_snapshot,gst_rate_basis_points,created_by,created_at
               ) VALUES(80,'allocation-first',80,?,100,0,100,?,?,0,1,
                        '2026-09-04T00:00:00Z')""",
            (first_fee["id"], method["version_id"], method["method_code"]),
        )
        connection.commit()

        class Actions:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("cross-case receipt must not reach the write service")

        class Handler:
            user = {"id": 1, "role_name": "Admin"}
            headers = {}
            files = []
            form = {"payment_uid": "payment-first"}

        with (
            patch("app.db.connect", return_value=connection),
            patch("app.db._release_connection"),
            patch("app.RevenueUIActions", Actions),
        ):
            with self.assertRaises(LookupError):
                app.CRMHandler._execute_revenue_action(
                    Handler(),
                    "billing/receipt",
                    scoped_case_uid=second["commercial_case_uid"],
                )

        self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM revenue_receipts").fetchone()[0])

    def test_scoped_get_and_post_deny_non_admin_before_database_access(self):
        class Denied:
            user = {
                "role_name": "Staff",
                "module_permissions_json": '[]',
            }

            @staticmethod
            def error_page(message, status):
                return message, int(status)

            def __getattr__(self, name):
                raise AssertionError("denied scoped route continued: " + name)

        with patch.object(
            app.db, "connect", side_effect=AssertionError("DB lookup forbidden")
        ):
            get_result = app.CRMHandler.revenue_case_billing(
                Denied(), "GET", "case-a"
            )
            post_result = app.CRMHandler.revenue_action(
                Denied(), "POST", "billing/payment", scoped_case_uid="case-a"
            )

        self.assertEqual(403, get_result[1])
        self.assertEqual(403, post_result[1])


if __name__ == "__main__":
    unittest.main()
