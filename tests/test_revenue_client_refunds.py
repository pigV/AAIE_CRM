"""Paid customer refunds preserve the original receipt and its settlement."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import sqlite3
from threading import Barrier
import unittest
from unittest.mock import patch, PropertyMock

from revenue.client_refunds import ClientRefundService, can_view_refund_evidence, client_cash_totals
from revenue.repository import RevenueRepository
from revenue.read_models import RevenueReadModels
from revenue.services import RevenueService
from revenue.ui_actions import RevenueUIActions
from tests import test_revenue_client_payment_v1 as payments
from tests.revenue_test_support import RevenueDatabaseFixture


MIGRATION = Path(__file__).resolve().parents[1] / "migrations/20261126_059_client_refunds.sql"


class RevenueClientRefundTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.connection = self.connect()
        self.connection.execute("ALTER TABLE users ADD COLUMN username TEXT")
        self.connection.execute("UPDATE users SET username='test-operator' WHERE id=1")
        self.connection.executescript(MIGRATION.read_text(encoding="utf-8"))
        self.repository = RevenueRepository(
            self.connection, now=lambda: "2026-09-08T00:00:00+00:00"
        )
        self.service = payments.ClientPaymentService(self.repository)
        payments.RevenueClientPaymentV1Tests._seed_fee(self)
        payments.RevenueClientPaymentV1Tests._seed_payment_methods(self)
        self.connection.commit()
        self.refunds = ClientRefundService(self.repository)
        self.documents = RevenueService(self.repository)
        self.document = self.documents.register_document(
            "client_refund_evidence", "/synthetic/refund.png", "a"*64,
            "image/png", 23, "upload", 1, "refund-document",
        )

    def _payment(self, method="au_personal_account", currency="AUD", amount=240_000):
        result = payments.RevenueClientPaymentV1Tests._record(
            self, method=method, actual_currency=currency,
            actual_amount=amount, base=240_000, key="payment",
        )
        return result["payment"]

    def _refund(self, payment, amount=100_000, key="refund", actor_role="Admin", time="2026-09-05T13:25"):
        return self.refunds.record(
            payment_uid=payment["uid"], actual_amount_cents=amount, refunded_at=time,
            reason="Agreed partial return", evidence_document_uid=self.document["uid"],
            actor_user_id=1, actor_role=actor_role, idempotency_key=key,
        )

    def test_paid_net_follows_refund_date_and_does_not_reopen_fee(self):
        payment = self._payment()
        receipt_document = self.documents.register_document(
            "receipt", "/synthetic/original.pdf", "b"*64, "application/pdf", 23, "generated", 1, "receipt-document",
        )
        receipt = self.documents.issue_receipt(
            payment["uid"], 240_000, receipt_document["uid"], 1, "original-receipt",
        )
        registered = self._refund(payment)
        self.assertEqual("paid", registered["status"])
        self.assertEqual("2026-09-05T13:25:00+10:00", registered["refunded_at"])
        self.assertEqual(100_000, registered["refunded_base_cents"])
        self.assertEqual(140_000, client_cash_totals(self.connection, start="2026-09-02", end="2026-09-05")[0]["net_actual_cents"])
        self.assertEqual(240_000, client_cash_totals(self.connection, start="2026-09-02", end="2026-09-02")[0]["net_actual_cents"])
        self.assertEqual(-100_000, client_cash_totals(self.connection, start="2026-09-05", end="2026-09-05")[0]["net_actual_cents"])
        fee = RevenueReadModels(self.connection)._billing_rows("settled", case_uid="case-1")[0]
        self.assertEqual((240_000, 140_000, 0), (fee["base_received_cents"], fee["net_base_received_cents"], fee["outstanding_base_cents"]))
        case = RevenueReadModels(self.connection)._recent_case_fees(1)["client_service_fee"]
        self.assertEqual((240_000, 100_000, 0), (case["received_cents"], case["refunded_base_cents"], case["outstanding_cents"]))
        workspace = RevenueReadModels(self.connection).workspace(
            "billing", {"cash_start": ["2026-09-05"], "cash_end": ["2026-09-05"]}, case_uid="case-1",
        )
        self.assertEqual(-100_000, workspace["cash_summary"]["rows"][0]["net_actual_cents"])
        payment_history = workspace["action"]["payment_history"][0]
        self.assertEqual(140_000, payment_history["refund_remaining_cents"])
        self.assertEqual("test-operator", payment_history["refunds"][0]["operator"])
        self.assertEqual(payment, self.repository.require_by_uid("revenue_payments", payment["uid"]))
        self.assertEqual(receipt, self.repository.require_by_uid("revenue_receipts", receipt["uid"]))

    def test_retries_and_partial_refunds_respect_actual_limit(self):
        payment = self._payment()
        original = self._refund(payment)
        self.assertEqual(original, self._refund(payment))
        self._refund(payment, 140_000, "balance")
        with self.assertRaisesRegex(ValueError, "可退款"):
            self._refund(payment, 1, "excess")
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM revenue_refunds").fetchone()[0])

    def test_cross_currency_preserves_actual_currency_and_apportions_aud(self):
        payment = self._payment("alipay", "CNY", 1_234_567)
        first = self._refund(payment, 400_000, "first")
        last = self._refund(payment, 834_567, "last")
        self.assertEqual("CNY", first["currency_code"])
        self.assertEqual(240_000, first["refunded_base_cents"] + last["refunded_base_cents"])
        self.assertEqual(0, last["refunded_gst_cents"])
        total = client_cash_totals(self.connection)[0]
        self.assertEqual(("CNY", 0, 0), (total["currency_code"], total["net_actual_cents"], total["net_base_cents"]))

    def test_tax_partial_rounding_reconciles_at_full_return(self):
        payment = self._payment("au_company_account", "AUD", 264_000)
        first = self._refund(payment, 110_001, "first")
        self.assertEqual(110_001, first["refunded_base_cents"] + first["refunded_gst_cents"])
        last = self._refund(payment, 153_999, "last")
        self.assertEqual(24_000, first["refunded_gst_cents"] + last["refunded_gst_cents"])

    def test_approved_only_is_excluded_from_paid_figures(self):
        payment = self._payment()
        self.documents.record_refund(payment["uid"], 90_000, "Approved only", 1, "approval")
        self.assertEqual(240_000, client_cash_totals(self.connection)[0]["net_actual_cents"])
        self._refund(payment, 240_000)
        self.assertEqual(0, client_cash_totals(self.connection)[0]["net_actual_cents"])

    def test_validates_authority_and_melbourne_local_time(self):
        payment = self._payment()
        with self.assertRaises(PermissionError):
            self._refund(payment, actor_role="Staff")
        with self.assertRaisesRegex(ValueError, "早于"):
            self._refund(payment, time="2026-09-01T12:00")
        with self.assertRaisesRegex(ValueError, "墨尔本"):
            self._refund(payment, time="2026-10-04T02:30")

    def test_competing_partial_refunds_cannot_exceed_the_same_payment(self):
        payment = self._payment()
        concurrent = Barrier(2)

        def attempt(key):
            with closing(sqlite3.connect(self.database_path)) as connection:
                service = ClientRefundService(RevenueRepository(
                    connection, now=lambda: "2026-09-08T00:00:00+00:00",
                ))
                concurrent.wait(timeout=5)
                try:
                    return service.record(
                        payment_uid=payment["uid"], actual_amount_cents=150_000,
                        refunded_at="2026-09-05T13:25", reason="Partial return",
                        evidence_document_uid=self.document["uid"], actor_user_id=1,
                        actor_role="Admin", idempotency_key=key,
                    )["status"]
                except ValueError as exc:
                    return str(exc)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ("concurrent-1", "concurrent-2")))
        self.assertCountEqual(["paid", "退款金额超过此笔收款的可退款余额。"], results)
        self.assertEqual(90_000, client_cash_totals(self.connection)[0]["net_actual_cents"])

    def test_evidence_stays_linked_to_original_case_and_obeys_student_visibility(self):
        payment = self._payment()
        self.assertFalse(can_view_refund_evidence(self.connection, self.document["uid"]))
        self._refund(payment)
        self.connection.execute("UPDATE revenue_commercial_cases SET status='closed' WHERE id=1")
        self.assertTrue(can_view_refund_evidence(self.connection, self.document["uid"]))
        self.connection.execute("ALTER TABLE students ADD COLUMN deleted_at TEXT")
        self.connection.execute("UPDATE students SET deleted_at='2026-09-06' WHERE student_id='S-REVENUE'")
        self.assertFalse(can_view_refund_evidence(self.connection, self.document["uid"]))

    def test_legacy_paid_needs_manual_reconciliation_before_a_new_refund(self):
        payment = self._payment()
        self.connection.execute(
            """INSERT INTO revenue_refunds (uid,payment_uid,amount_cents,reason,status,created_at)
               VALUES ('legacy-paid',?,2000,'legacy','paid','2026-09-03')""",
            (payment["uid"],),
        )
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "历史实退记录"):
            self._refund(payment)

    def test_rejected_upload_rolls_back_document_and_retry_keeps_one_link(self):
        payment = self._payment()
        root = Path(self.temp_dir.name)
        staged = SimpleNamespace(
            path=root / "evidence.png", sha256="c"*64, mime_type="image/png", size_bytes=27,
        )
        form = {
            "payment_uid": payment["uid"], "amount": "1000.00", "reason": "Partial payout",
            "refunded_at": "2026-09-05T13:25", "idempotency_key": "upload-granted",
        }
        actions = RevenueUIActions(self.connection, root, 1, "Admin")
        # The actual protected upload/readback uses the approved staging storage root.
        # Only the storage boundary is replaced here to isolate SQL rollback and retry.
        with patch.object(RevenueUIActions, "document_root", new_callable=PropertyMock, return_value=root), patch(
            "revenue.ui_actions.stage_revenue_upload", return_value=staged,
        ):
            with self.assertRaisesRegex(ValueError, "可退款"):
                actions.record_client_refund({**form, "amount": "2500.00"}, object())
            self.assertEqual(0, self.connection.execute(
                "SELECT COUNT(*) FROM revenue_documents WHERE storage_path=?", (str(staged.path),),
            ).fetchone()[0])
            recorded = actions.record_client_refund(form, object())
            self.assertEqual(recorded, actions.record_client_refund(form, object()))
        documents = self.connection.execute(
            "SELECT * FROM revenue_documents WHERE storage_path=?", (str(staged.path),),
        ).fetchall()
        self.assertEqual(1, len(documents))
        self.assertEqual((recorded["evidence_document_id"], staged.sha256), (documents[0]["id"], documents[0]["sha256"]))
        self.assertTrue(can_view_refund_evidence(self.connection, documents[0]["uid"]))
