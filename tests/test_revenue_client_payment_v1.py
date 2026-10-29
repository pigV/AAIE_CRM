
from tests.revenue_secure_test_support import secure_directory
from contextlib import closing
import json
import sqlite3
import threading
from pathlib import Path
import unittest

from pypdf import PdfReader

from revenue.client_payments import ClientPaymentService
from revenue.repository import RevenueRepository
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueClientPaymentV1Tests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.connection = self.connect()
        self.connection.execute("ALTER TABLE students ADD COLUMN passport_first_name TEXT")
        self.connection.execute("ALTER TABLE students ADD COLUMN passport_last_name TEXT")
        self.connection.execute(
            "UPDATE students SET passport_first_name='Jimmy',passport_last_name='Zhang' WHERE student_id='S-REVENUE'"
        )
        self.service = ClientPaymentService(
            RevenueRepository(self.connection, now=lambda: "2026-09-02T00:00:00+00:00")
        )
        self._seed_fee()
        self._seed_payment_methods()
        self.connection.commit()

    def _seed_fee(self, *, net_cents=240_000, contract_id=70, snapshot_contract_id=None):
        snapshot_contract_id = contract_id if snapshot_contract_id is None else snapshot_contract_id
        self.connection.execute(
            "INSERT INTO contracts VALUES(?,?,?,?,?,?)",
            (contract_id, "S-REVENUE", 100, "FullyExecuted", "2026-09-01", "2026-09-01T00:00:00Z"),
        )
        self.connection.execute(
            "INSERT INTO revenue_commercial_cases VALUES(1,'case-1',100,'S-REVENUE','open',0,'{}',1,'2026-09-01T00:00:00Z',NULL)"
        )
        self.connection.execute(
            "INSERT INTO revenue_billing_accounts VALUES(1,'account-1','individual','S-REVENUE','AUD','active',1,'2026-09-01T00:00:00Z')"
        )
        self.connection.execute(
            "INSERT INTO revenue_billing_account_students VALUES(1,'account-student-1',1,'S-REVENUE','2026-09-01',NULL,'2026-09-01T00:00:00Z')"
        )
        payload = {
            "commercial_case_uid": "case-1",
            "billing_account_uid": "account-1",
            "source_rule_uid": "rule-1",
            "items": [{
                "contract_id": snapshot_contract_id,
                "application_cycle_id": 100,
                "milestone_code": "contract_execution",
                "description": "Client service fee",
                "net_cents": net_cents,
                "gst_cents": 0,
            }],
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.connection.execute(
            "INSERT INTO revenue_rule_snapshots VALUES(1,'snapshot-1','fee','rule-1',?,?,1,'2026-09-01T00:00:00Z')",
            (canonical, "a" * 64),
        )
        self.connection.execute(
            """INSERT INTO revenue_fee_items(
                 id,uid,commercial_case_id,billing_account_id,student_id,rule_snapshot_id,
                 milestone_code,description,net_cents,gst_cents,gross_cents,currency_code,status,created_at
               ) VALUES(1,'fee-1',1,1,'S-REVENUE',1,'contract_execution','Client service fee',
                        ?,0,?,'AUD','unbilled','2026-09-01T00:00:00Z')""",
            (net_cents, net_cents),
        )

    def _seed_payment_methods(self):
        rows = (
            (1, "alipay", "支付宝", 0),
            (2, "wechat", "微信", 0),
            (3, "cash", "现金", 0),
            (4, "au_personal_account", "澳洲个人账户", 0),
            (5, "au_company_account", "澳洲公司账户", 1000),
        )
        for identifier, code, label, gst_bps in rows:
            self.connection.execute(
                "INSERT INTO revenue_payment_methods VALUES(?,?,?,?,'active')",
                (identifier, f"method-{code}", code, label),
            )
            self.connection.execute(
                """INSERT INTO revenue_payment_method_rule_versions(
                     id,uid,payment_method_id,effective_from,effective_to,gst_rate_basis_points,
                     surcharge_basis_points,status,created_at
                   ) VALUES(?,?,?,'2026-01-01',NULL,?,0,'active','2026-09-01T00:00:00Z')""",
                (identifier, f"method-rule-{code}-v1", identifier, gst_bps),
            )

    def _record(self, *, method, actual_currency, actual_amount, base, key, actor_role="Admin", access=False):
        return self.service.record_client_payment(
            fee_item_uid="fee-1",
            payment_method_code=method,
            actual_currency_code=actual_currency,
            actual_amount_cents=actual_amount,
            allocated_base_cents=base,
            received_date="2026-09-02",
            external_reference=f"TX-{key}",
            evidence_document_uid=None,
            actor_user_id=1,
            actor_role=actor_role,
            actor_has_revenue_access=access,
            idempotency_key=key,
        )

    def test_lazy_formal_contract_link_is_exact_and_idempotent(self):
        first = self.service.ensure_fee_item_contract_link(
            "fee-1", actor_user_id=1, idempotency_key="link-fee-1"
        )
        replay = self.service.ensure_fee_item_contract_link(
            "fee-1", actor_user_id=1, idempotency_key="link-fee-1"
        )
        self.assertEqual(first["uid"], replay["uid"])
        self.assertEqual(70, first["contract_id"])
        self.assertEqual(1, self.connection.execute(
            "SELECT COUNT(*) FROM revenue_fee_item_contract_links"
        ).fetchone()[0])

    def test_lazy_link_fails_closed_when_snapshot_contract_mismatches(self):
        payload = {
            "commercial_case_uid": "case-1",
            "billing_account_uid": "account-1",
            "source_rule_uid": "rule-bad",
            "items": [{
                "contract_id": 999,
                "application_cycle_id": 100,
                "milestone_code": "contract_execution",
                "description": "Bad client fee",
                "net_cents": 1,
                "gst_cents": 0,
            }],
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.connection.execute(
            "INSERT INTO revenue_rule_snapshots VALUES(2,'snapshot-bad','fee','rule-bad',?,?,1,'2026-09-01T00:00:00Z')",
            (canonical, "b" * 64),
        )
        self.connection.execute(
            """INSERT INTO revenue_fee_items(
                 id,uid,commercial_case_id,billing_account_id,student_id,rule_snapshot_id,
                 milestone_code,description,net_cents,gst_cents,gross_cents,currency_code,status,created_at
               ) VALUES(2,'fee-bad',1,1,'S-REVENUE',2,'contract_execution','Bad client fee',
                        1,0,1,'AUD','unbilled','2026-09-01T00:00:00Z')"""
        )
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "immutable Fee Item rule snapshot Contract"):
            self.service.ensure_fee_item_contract_link(
                "fee-bad", actor_user_id=1, idempotency_key="link-mismatch"
            )
        self.assertEqual(0, self.connection.execute(
            "SELECT COUNT(*) FROM revenue_fee_item_contract_links"
        ).fetchone()[0])

    def test_alipay_and_wechat_keep_cny_actual_separate_from_aud_base_and_no_fx(self):
        alipay = self._record(
            method="alipay", actual_currency="CNY", actual_amount=1_135_000,
            base=120_000, key="pay-alipay",
        )
        wechat = self._record(
            method="wechat", actual_currency="CNY", actual_amount=500_000,
            base=120_000, key="pay-wechat",
        )
        self.assertEqual(("CNY", 1_135_000), (
            alipay["payment"]["currency_code"], alipay["payment"]["amount_cents"]
        ))
        self.assertEqual(0, alipay["payment"]["allocated_cents"])
        self.assertEqual((120_000, 0, 120_000), (
            alipay["allocation"]["allocated_base_cents"],
            alipay["allocation"]["gst_cents"],
            alipay["allocation"]["gross_charge_aud_cents"],
        ))
        self.assertEqual("settled", wechat["fee_status"])
        self.assertNotIn("exchange_rate", json.dumps(alipay, sort_keys=True))
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM revenue_invoices").fetchone()[0])

    def test_australian_private_and_company_validation_and_integer_rounding(self):
        private = self._record(
            method="au_personal_account", actual_currency="AUD", actual_amount=100_050,
            base=100_050, key="pay-private",
        )
        self.assertEqual(0, private["allocation"]["gst_cents"])
        company = self._record(
            method="au_company_account", actual_currency="AUD", actual_amount=153_944,
            base=139_949, key="pay-company",
        )
        self.assertEqual(13_995, company["allocation"]["gst_cents"])
        self.assertEqual(153_944, company["allocation"]["gross_charge_aud_cents"])
        with self.assertRaisesRegex(ValueError, "actual AUD amount must equal"):
            self._record(
                method="au_company_account", actual_currency="AUD", actual_amount=2,
                base=1, key="bad-company",
            )

    def test_partial_mixed_methods_settle_base_and_collect_gst_per_allocation(self):
        first = self._record(
            method="alipay", actual_currency="CNY", actual_amount=1_000_000,
            base=120_000, key="partial-1",
        )
        second = self._record(
            method="au_company_account", actual_currency="AUD", actual_amount=132_000,
            base=120_000, key="partial-2",
        )
        self.assertEqual("partially_received", first["fee_status"])
        self.assertEqual("settled", second["fee_status"])
        totals = self.service.fee_payment_summary("fee-1")
        self.assertEqual(240_000, totals["final_base_cents"])
        self.assertEqual(240_000, totals["base_received_cents"])
        self.assertEqual(12_000, totals["gst_collected_cents"])
        self.assertEqual(0, totals["outstanding_base_cents"])

    def test_overallocation_rolls_back_payment_and_allocation_atomically(self):
        self._record(
            method="au_personal_account", actual_currency="AUD", actual_amount=120_000,
            base=120_000, key="atomic-first",
        )
        before = self.connection.execute("SELECT COUNT(*) FROM revenue_payments").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "exceeds Fee Item final base"):
            self._record(
                method="au_personal_account", actual_currency="AUD", actual_amount=130_000,
                base=130_000, key="atomic-over",
            )
        self.assertEqual(before, self.connection.execute(
            "SELECT COUNT(*) FROM revenue_payments"
        ).fetchone()[0])
        self.assertEqual(1, self.connection.execute(
            "SELECT COUNT(*) FROM revenue_client_payment_allocations"
        ).fetchone()[0])

    def test_replay_is_idempotent_and_cash_or_unauthorized_access_is_denied(self):
        first = self._record(
            method="au_personal_account", actual_currency="AUD", actual_amount=120_000,
            base=120_000, key="idempotent-payment",
        )
        replay = self._record(
            method="au_personal_account", actual_currency="AUD", actual_amount=120_000,
            base=120_000, key="idempotent-payment",
        )
        self.assertEqual(first["payment"]["uid"], replay["payment"]["uid"])
        with self.assertRaises(PermissionError):
            self._record(
                method="wechat", actual_currency="CNY", actual_amount=1,
                base=1, key="denied", actor_role="Staff", access=False,
            )
        with self.assertRaisesRegex(ValueError, "cash is not allowed"):
            self._record(
                method="cash", actual_currency="AUD", actual_amount=1,
                base=1, key="forged-cash",
            )
        allowed = self._record(
            method="wechat", actual_currency="CNY", actual_amount=50_000,
            base=120_000, key="explicit-user", actor_role="Staff", access=True,
        )
        self.assertEqual(1, allowed["payment"]["confirmed_by"])

    def test_receipt_is_one_per_payment_idempotent_and_uses_immutable_structured_snapshot(self):
        payment = self._record(
            method="alipay", actual_currency="CNY", actual_amount=1_135_000,
            base=240_000, key="receipt-payment",
        )["payment"]
        output = secure_directory(self) / "receipt-documents"
        first = self.service.issue_client_receipt(
            payment_uid=payment["uid"],
            document_root=output,
            actor_user_id=1,
            actor_role="Admin",
            actor_has_revenue_access=False,
            idempotency_key="receipt-issue-1",
            issuer_name="AAIE Education Services Pty Ltd",
            issuer_abn="12 345 678 901",
        )
        replay = self.service.issue_client_receipt(
            payment_uid=payment["uid"],
            document_root=output,
            actor_user_id=1,
            actor_role="Admin",
            actor_has_revenue_access=False,
            idempotency_key="receipt-issue-2",
            issuer_name="AAIE Education Services Pty Ltd",
            issuer_abn="12 345 678 901",
        )
        self.assertEqual(first["receipt"]["uid"], replay["receipt"]["uid"])
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM revenue_receipts").fetchone()[0])
        snapshot = self.connection.execute("SELECT * FROM revenue_receipt_snapshots").fetchone()
        self.assertEqual("Jimmy Zhang", snapshot["student_name"])
        self.assertEqual("S-REVENUE", snapshot["student_number"])
        self.assertEqual(70, snapshot["contract_id"])
        self.assertEqual("CNY", snapshot["actual_currency_code"])
        self.assertEqual(1_135_000, snapshot["actual_amount_cents"])
        self.assertEqual(240_000, snapshot["allocated_base_cents"])
        self.assertEqual(0, snapshot["gst_cents"])
        self.assertEqual(64, len(snapshot["snapshot_sha256"]))
        document = self.connection.execute(
            "SELECT * FROM revenue_documents WHERE id=?", (first["receipt"]["document_id"],)
        ).fetchone()
        pdf_path = Path(document["storage_path"])
        self.assertTrue(pdf_path.is_file())
        text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
        for expected in (
            snapshot["receipt_number"], "Jimmy Zhang", "S-REVENUE", "cycle-100",
            "CONTRACT-70", "Client service fee", "CNY 11,350.00", "AUD 2,400.00",
            "TX-receipt-payment",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("Exchange Rate", text)
        self.assertNotIn("Client ID", text)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "Receipted Revenue Payment"):
            self.connection.execute(
                "UPDATE revenue_payments SET external_reference='changed' WHERE id=?",
                (payment["id"],),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "financial document"):
            self.connection.execute(
                "UPDATE revenue_documents SET storage_path='changed' WHERE id=?",
                (first["receipt"]["document_id"],),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "Receipt is immutable"):
            self.connection.execute(
                "UPDATE revenue_receipts SET amount_cents=1 WHERE id=?",
                (first["receipt"]["id"],),
            )

    def test_begin_immediate_serializes_competing_allocations(self):
        self.connection.close()
        outcomes = []
        barrier = threading.Barrier(2)

        def run(key):
            connection = sqlite3.connect(self.database_path, timeout=5)
            connection.row_factory = sqlite3.Row
            try:
                barrier.wait(timeout=5)
                service = ClientPaymentService(
                    RevenueRepository(connection, now=lambda: "2026-09-02T00:00:00+00:00")
                )
                try:
                    service.record_client_payment(
                        fee_item_uid="fee-1", payment_method_code="au_personal_account",
                        actual_currency_code="AUD", actual_amount_cents=150_000,
                        allocated_base_cents=150_000, received_date="2026-09-02",
                        external_reference=key, evidence_document_uid=None,
                        actor_user_id=1, actor_role="Admin", actor_has_revenue_access=False,
                        idempotency_key=key,
                    )
                except ValueError as exc:
                    outcomes.append(type(exc).__name__)
                else:
                    outcomes.append("ok")
            finally:
                connection.close()

        threads = [threading.Thread(target=run, args=(f"concurrent-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(["ValueError", "ok"], sorted(outcomes))
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_client_payment_allocations"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_payments WHERE counterparty_kind='client'"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
