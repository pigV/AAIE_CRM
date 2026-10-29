#!/usr/bin/env python3
"""Isolated browser/HTTP acceptance for direct Fee Item Payment and Receipt V1."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import urllib.error
import urllib.request
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
FORMAL_DB = Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db").resolve()
ADMIN_TOKEN = "revenue-e2e-admin"
ADMIN_CSRF = "csrf-900001"
SENSITIVE_TERMS = ("revenue", "commission", "收入与佣金", "佣金", "master agent")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def get(base_url: str, path: str, token: str = ADMIN_TOKEN) -> dict:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        headers={"Cookie": f"aaie_session={token}", "User-Agent": "AAIE-Revenue-034-Acceptance/1.0"},
    )
    try:
        response = OPENER.open(request, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return {
            "status": int(response.status),
            "headers": dict(response.headers.items()),
            "payload": response.read(),
        }


def multipart_post(base_url: str, path: str, fields: dict[str, str], file_value=None) -> dict:
    boundary = f"----AAIERevenue{uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend((
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
            str(value).encode(), b"\r\n",
        ))
    if file_value:
        field, filename, mime_type, payload = file_value
        chunks.extend((
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {mime_type}\r\n\r\n".encode(), payload, b"\r\n",
        ))
    chunks.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=b"".join(chunks),
        method="POST",
        headers={
            "Cookie": f"aaie_session={ADMIN_TOKEN}",
            "User-Agent": "AAIE-Revenue-034-Acceptance/1.0",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        response = OPENER.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return {"status": int(response.status), "payload": response.read()}


def form_post(base_url: str, path: str, fields: dict[str, str]) -> dict:
    from urllib.parse import urlencode

    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=urlencode(fields).encode(),
        method="POST",
        headers={
            "Cookie": f"aaie_session={ADMIN_TOKEN}",
            "User-Agent": "AAIE-Revenue-034-Acceptance/1.0",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        response = OPENER.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return {"status": int(response.status), "payload": response.read()}


def text(result: dict) -> str:
    return result["payload"].decode("utf-8", errors="replace")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8912")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    database = args.database.resolve()
    output = args.output.resolve()
    if database == FORMAL_DB or database.is_symlink() or not database.is_file():
        raise AssertionError("browser acceptance database must be isolated")
    if not database.is_relative_to(ROOT / "SAFEBOX"):
        raise AssertionError("browser acceptance database must remain inside SAFEBOX")
    if not output.is_relative_to(ROOT / "SAFEBOX"):
        raise AssertionError("browser acceptance evidence must remain inside SAFEBOX")

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        fee = connection.execute(
            """SELECT f.uid,f.net_cents,
                      COALESCE((SELECT SUM(a.allocated_base_cents)
                                  FROM revenue_client_payment_allocations a
                                 WHERE a.fee_item_id=f.id),0) received
                 FROM revenue_fee_items f WHERE f.student_id='UI-FINANCE-001'
                 ORDER BY f.id LIMIT 1"""
        ).fetchone()
        if not fee or int(fee["net_cents"]) - int(fee["received"]) != 120_000:
            raise AssertionError("synthetic client Fee Item is not at the required partial state")
        client_invoice_before = connection.execute(
            "SELECT COUNT(*) FROM revenue_invoices WHERE document_kind='client_invoice'"
        ).fetchone()[0]

    desktop_before = get(args.base_url, "/revenue/billing")
    if desktop_before["status"] != 200:
        raise AssertionError(f"Desktop billing returned {desktop_before['status']}")
    before_html = text(desktop_before)
    required = (
        "添加客户收款", "本次服务费", "付款凭证", "生成客户收据",
        "支付宝", "微信", "澳洲私人账号", "澳洲公司账号",
    )
    missing = [value for value in required if value not in before_html]
    if missing:
        raise AssertionError(f"Desktop billing is missing controls: {missing}")
    if "签发客户发票" in before_html or 'action="/revenue/billing/invoice"' in before_html:
        raise AssertionError("Client Invoice is still exposed in the current workflow")
    if '<option value="cash"' in before_html:
        raise AssertionError("cash is exposed in the client Payment selector")
    if "核销" in before_html:
        raise AssertionError("internal allocation terminology is exposed to the user")

    payment_result = multipart_post(
        args.base_url,
        "/revenue/billing/payment",
        {
            "csrf": ADMIN_CSRF,
            "idempotency_key": "browser-034-wechat-final-payment",
            "fee_item_uid": str(fee["uid"]),
            "payment_method_code": "wechat",
            "actual_currency_code": "CNY",
            "actual_amount_cents": "500000",
            "allocated_base_cents": "120000",
            "received_date": "2026-09-02",
            "external_reference": "BROWSER-WX-034",
        },
        ("payment_evidence", "wechat-proof.png", "image/png", b"\x89PNG\r\n\x1a\nAAIE-E2E"),
    )
    if payment_result["status"] != 303:
        raise AssertionError(f"client Payment POST returned {payment_result['status']}")

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        payment = connection.execute(
            """SELECT p.uid,p.currency_code,p.amount_cents,p.confirmed_by,d.uid evidence_uid,
                      a.allocated_base_cents,a.gst_cents,a.gross_charge_aud_cents
                 FROM revenue_payments p
                 JOIN revenue_client_payment_allocations a ON a.payment_id=p.id
                 JOIN revenue_documents d ON d.id=p.evidence_document_id
                WHERE p.external_reference='BROWSER-WX-034'"""
        ).fetchone()
        if not payment:
            raise AssertionError("browser client Payment was not persisted")
        if tuple(payment[key] for key in (
            "currency_code", "amount_cents", "allocated_base_cents", "gst_cents",
            "gross_charge_aud_cents", "confirmed_by",
        )) != ("CNY", 500_000, 120_000, 0, 120_000, 900001):
            raise AssertionError("browser client Payment authority mismatch")
        fee_summary = connection.execute(
            """SELECT f.net_cents,SUM(a.allocated_base_cents),SUM(a.gst_cents)
                 FROM revenue_fee_items f
                 JOIN revenue_client_payment_allocations a ON a.fee_item_id=f.id
                WHERE f.uid=? GROUP BY f.id""",
            (fee["uid"],),
        ).fetchone()
        if tuple(fee_summary) != (240_000, 240_000, 12_000):
            raise AssertionError(f"mixed partial settlement is wrong: {tuple(fee_summary)}")

    evidence_result = get(
        args.base_url, f"/revenue/documents/{payment['evidence_uid']}/view"
    )
    if evidence_result["status"] != 200 or evidence_result["payload"][:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("protected Payment evidence view failed")

    receipt_result = form_post(
        args.base_url,
        "/revenue/billing/receipt",
        {
            "csrf": ADMIN_CSRF,
            "idempotency_key": "browser-034-wechat-receipt",
            "payment_uid": str(payment["uid"]),
        },
    )
    if receipt_result["status"] != 303:
        raise AssertionError(f"Receipt POST returned {receipt_result['status']}")
    replay = form_post(
        args.base_url,
        "/revenue/billing/receipt",
        {
            "csrf": ADMIN_CSRF,
            "idempotency_key": "browser-034-wechat-receipt-replay",
            "payment_uid": str(payment["uid"]),
        },
    )
    if replay["status"] != 303:
        raise AssertionError("Receipt idempotent replay failed")

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        receipt = connection.execute(
            """SELECT r.receipt_number,d.uid document_uid,d.sha256,s.student_number,
                      s.actual_currency_code,s.actual_amount_cents,s.allocated_base_cents,
                      s.gst_cents,s.transaction_reference
                 FROM revenue_receipts r
                 JOIN revenue_documents d ON d.id=r.document_id
                 JOIN revenue_receipt_snapshots s ON s.receipt_id=r.id
                WHERE r.payment_id=(SELECT id FROM revenue_payments WHERE uid=?)""",
            (payment["uid"],),
        ).fetchone()
        if not receipt:
            raise AssertionError("Receipt snapshot was not persisted")
        if connection.execute(
            "SELECT COUNT(*) FROM revenue_receipts WHERE payment_id=(SELECT id FROM revenue_payments WHERE uid=?)",
            (payment["uid"],),
        ).fetchone()[0] != 1:
            raise AssertionError("Receipt replay created a duplicate")
        client_invoice_after = connection.execute(
            "SELECT COUNT(*) FROM revenue_invoices WHERE document_kind='client_invoice'"
        ).fetchone()[0]
        if client_invoice_before != client_invoice_after:
            raise AssertionError("direct client Payment created a Client Invoice")

    receipt_pdf = get(args.base_url, f"/revenue/documents/{receipt['document_uid']}/view")
    if receipt_pdf["status"] != 200 or not receipt_pdf["payload"].startswith(b"%PDF"):
        raise AssertionError("protected Receipt PDF view failed")
    if hashlib.sha256(receipt_pdf["payload"]).hexdigest() != receipt["sha256"]:
        raise AssertionError("served Receipt PDF failed SHA-256 verification")

    desktop_after = get(args.base_url, "/revenue/billing")
    mobile = get(args.base_url, "/mobile/revenue")
    if desktop_after["status"] != 200 or "已收清" not in text(desktop_after):
        raise AssertionError("Desktop did not reflect Fee Item settlement")
    if mobile["status"] != 200 or "已收清" not in text(mobile):
        raise AssertionError("Mobile read model did not retain current client Payment status")
    mobile_case_href = None
    with sqlite3.connect(database) as connection:
        case_uid = connection.execute(
            "SELECT uid FROM revenue_commercial_cases WHERE student_id='UI-FINANCE-001' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    mobile_case_href = f"/mobile/revenue/cases/{case_uid}"
    mobile_case = get(args.base_url, mobile_case_href)
    if mobile_case["status"] != 200 or f"/revenue/documents/{receipt['document_uid']}/view" not in text(mobile_case):
        raise AssertionError("Mobile case does not expose the protected Receipt link")
    mobile_case_html = text(mobile_case)
    if (
        'action="/revenue/billing/payment"' in mobile_case_html
        or 'action="/revenue/billing/receipt"' in mobile_case_html
    ):
        raise AssertionError("Mobile case unexpectedly exposes complex financial mutation forms")

    unauthorized = {}
    for name, token in (
        ("Consultant", "revenue-e2e-consultant"),
        ("Finance", "revenue-e2e-finance"),
    ):
        for path in (
            "/revenue/billing",
            f"/revenue/documents/{receipt['document_uid']}/view",
            mobile_case_href,
        ):
            denied = get(args.base_url, path, token)
            if denied["status"] != 403:
                raise AssertionError(f"{name} {path} returned {denied['status']}")
            lowered = text(denied).lower()
            leaks = [term for term in SENSITIVE_TERMS if term.lower() in lowered]
            if leaks:
                raise AssertionError(f"{name} denial leaked {leaks}")
            unauthorized[f"{name} {path}"] = {"status": 403, "leakage": []}

    report = {
        "status": "PASS",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "desktop_fee_item_payment": "PASS",
        "payment_evidence_png_protected": "PASS",
        "actual_cny_separate_from_aud_base": "PASS",
        "mixed_partial_settlement": {"base_received_cents": 240000, "gst_collected_cents": 12000},
        "receipt_snapshot_pdf_idempotency": "PASS",
        "mobile_read_only_receipt": "PASS",
        "client_invoice_created": False,
        "cash_selector_exposed": False,
        "unauthorized_zero_leakage": unauthorized,
        "production_service_used": False,
        "production_database_used": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
