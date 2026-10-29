from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pypdfium2 as pdfium


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from revenue.pdf import (
    render_claim_pdf, render_invoice_pdf, render_receipt_pdf, render_statement_pdf,
)


DEFAULT_OUTPUT = ROOT / "SAFEBOX/revenue-operational-integration-20260830/pdf"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        raise AssertionError("PDF acceptance output must remain inside the worktree")
    output.mkdir(parents=True, exist_ok=True)
    issuer = {"issuer_name": "AAIE Education Services Pty Ltd",
              "issuer_abn": "12 345 678 901", "currency_code": "AUD"}
    invoice = {**issuer, "document_number": "INV-2026-000001",
               "issue_date": "2026-08-29", "due_date": "2026-09-05",
               "bill_to": "Zhang Family",
               "lines": [{"description": "Application and enrolment service",
                           "net_cents": 240_000, "gst_cents": 24_000,
                           "gross_cents": 264_000}],
               "net_cents": 240_000, "gst_cents": 24_000, "gross_cents": 264_000}
    receipt = {**issuer, "document_number": "RCT-2026-000001",
               "issue_date": "2026-08-30", "received_from": "Zhang Family",
               "amount_cents": 100_000, "payment_reference": "PAY-ISOLATED-001"}
    claim = {**issuer, "document_number": "CLM-2026-000001",
             "issue_date": "2026-08-30", "claim_to": "AAIE Test School",
             "lines": [{"description": "2026 tuition commission instalment",
                         "net_cents": 100_000, "gst_cents": 10_000,
                         "gross_cents": 110_000}],
             "net_cents": 100_000, "gst_cents": 10_000, "gross_cents": 110_000}
    statement = {**issuer, "document_number": "ST-ISOLATED-001",
                 "issue_date": "2026-08-30", "master_agent": "AAIE Test Master Agent",
                 "lines": [{"description": "Revenue Student - Year 10",
                             "gross_cents": 110_000, "share_cents": 22_000,
                             "payable_cents": 88_000}],
                 "total_cents": 110_000, "share_cents": 22_000,
                 "payable_cents": 88_000}
    hashes = {
        "invoice": render_invoice_pdf(invoice, output / "isolated-invoice.pdf"),
        "receipt": render_receipt_pdf(receipt, output / "isolated-receipt.pdf"),
        "claim": render_claim_pdf(claim, output / "isolated-claim.pdf"),
        "statement": render_statement_pdf(statement, output / "isolated-statement.pdf"),
    }
    for name in hashes:
        document = pdfium.PdfDocument(output / f"isolated-{name}.pdf")
        page = document[0]
        page.render(scale=2).to_pil().save(output / f"isolated-{name}-page1.png")
        page.close()
        document.close()
    (output / "pdf-hashes.json").write_text(
        json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(hashes, sort_keys=True))


if __name__ == "__main__":
    main()
