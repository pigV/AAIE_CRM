from __future__ import annotations

from tests.revenue_secure_test_support import secure_directory
from pathlib import Path
import tempfile
import unittest

from pypdf import PdfReader

from revenue.pdf import (
    render_claim_pdf,
    render_invoice_pdf,
    render_receipt_pdf,
    render_statement_pdf,
)


class RevenuePDFTests(unittest.TestCase):
    def setUp(self):
        self.output = secure_directory(self)

    def test_invoice_contains_identity_abn_number_gst_total_and_is_deterministic(self):
        snapshot = {
            "issuer_name": "AAIE Education Services Pty Ltd",
            "issuer_abn": "12 345 678 901",
            "document_number": "INV-2026-000001",
            "issue_date": "2026-08-29", "due_date": "2026-09-05",
            "bill_to": "Zhang Family",
            "lines": [{"description": f"Application service item {index + 1}",
                       "net_cents": 4_000, "gst_cents": 400,
                       "gross_cents": 4_400} for index in range(60)],
            "net_cents": 240_000, "gst_cents": 24_000, "gross_cents": 264_000,
            "currency_code": "AUD",
        }
        first = self.output / "invoice.pdf"
        second = self.output / "invoice-copy.pdf"
        first_hash = render_invoice_pdf(snapshot, first)
        second_hash = render_invoice_pdf(snapshot, second)
        self.assertEqual(first_hash, second_hash)
        reader = PdfReader(first)
        self.assertGreater(len(reader.pages), 1)
        text = "\n".join(page.extract_text() for page in reader.pages)
        for expected in ("AAIE Education Services Pty Ltd", "12 345 678 901",
                         "INV-2026-000001", "GST", "$2,640.00"):
            self.assertIn(expected, text)

    def test_receipt_claim_and_statement_have_authoritative_totals(self):
        common = {"issuer_name": "AAIE Education Services Pty Ltd",
                  "issuer_abn": "12 345 678 901", "currency_code": "AUD"}
        artifacts = [
            (render_receipt_pdf, {**common, "document_number": "RCT-2026-000001",
             "issue_date": "2026-08-30", "received_from": "Zhang Family",
             "amount_cents": 100_000, "payment_reference": "PAY-1"}, "Receipt", "$1,000.00"),
            (render_claim_pdf, {**common, "document_number": "CLM-2026-000001",
             "issue_date": "2026-08-30", "claim_to": "Test School",
             "net_cents": 100_000, "gst_cents": 10_000, "gross_cents": 110_000,
             "lines": [{"description": "Commission instalment", "gross_cents": 110_000}]},
             "Commission Claim", "$1,100.00"),
            (render_statement_pdf, {**common, "document_number": "ST-001",
             "issue_date": "2026-08-30", "master_agent": "Test Master Agent",
             "total_cents": 110_000, "share_cents": 22_000, "payable_cents": 88_000,
             "lines": [{"description": "Revenue Student - Year 10",
                        "gross_cents": 110_000, "share_cents": 22_000,
                        "payable_cents": 88_000}]}, "Master Agent Statement", "$880.00"),
        ]
        for index, (renderer, snapshot, heading, total) in enumerate(artifacts):
            path = self.output / f"artifact-{index}.pdf"
            digest = renderer(snapshot, path)
            self.assertEqual(64, len(digest))
            text = "\n".join(page.extract_text() for page in PdfReader(path).pages)
            self.assertIn(heading, text)
            self.assertIn(total, text)

    def test_client_receipt_renders_actual_currency_and_aud_base_without_parent_or_fx(self):
        snapshot = {
            "issuer_name": "AAIE Education Services Pty Ltd",
            "issuer_abn": "12 345 678 901",
            "receipt_number": "RCT-2026-000009",
            "issued_at": "2026-09-02T00:00:00+00:00",
            "student_name": "Jimmy Zhang",
            "student_number": "S-REVENUE",
            "application_cycle_label": "cycle-100",
            "contract_reference": "CONTRACT-70",
            "fee_type": "Client service fee",
            "payment_date": "2026-09-02",
            "payment_method_label": "支付宝",
            "actual_currency_code": "CNY",
            "actual_amount_cents": 1_135_000,
            "allocated_base_cents": 240_000,
            "gst_cents": 0,
            "gross_charge_aud_cents": 240_000,
            "transaction_reference": "TX-ALIPAY-1",
        }
        path = self.output / "client-receipt.pdf"
        render_receipt_pdf(snapshot, path)
        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        for expected in (
            "RCT-2026-000009", "Jimmy Zhang", "S-REVENUE", "cycle-100",
            "CONTRACT-70", "Client service fee", "CNY 11,350.00", "AUD 2,400.00",
            "TX-ALIPAY-1",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("Received from", text)
        self.assertNotIn("Exchange Rate", text)
        self.assertNotIn("Client ID", text)
        self.assertIn("本次服务费", text)
        self.assertNotIn("核销", text)


if __name__ == "__main__":
    unittest.main()
