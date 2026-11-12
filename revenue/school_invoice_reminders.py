"""Overdue school invoice projections and manual reminder records."""

import hashlib
import json
import re

from revenue.repository import canonical_json


ACTION = "school_commission_reminder_marked_sent"
EMAIL = re.compile(r"[A-Za-z0-9_+%'-]+(?:\.[A-Za-z0-9_+%'-]+)*@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+")


def invoice_overviews(issuer, settlement, entity_id, *, issued_by=None):
    for invoice in issuer.school_invoices(entity_id, issued_by=issued_by):
        try:
            detail = settlement.detail(invoice["uid"])
        except ValueError:
            # Keep the issued document visible even if older data cannot be settled.
            yield {**invoice, "settlement": None}
        else:
            yield {**invoice, "settlement": detail["settlement"], "legal_name": detail["legal_name"]}


def draft_reminder(invoice, recipient, *, simulation=None):
    settlement = invoice["settlement"]
    if not settlement["overdue_days"]:
        raise ValueError("只有已逾期且尚有未收余额的发票可以催款。")
    recipient = str(recipient or "").strip()
    if simulation is not None:
        from revenue.school_invoice_release import payer_mail_addresses

        recipient = ", ".join(payer_mail_addresses(recipient, simulation=simulation))
    elif len(recipient) > 254 or not EMAIL.fullmatch(recipient):
        raise ValueError("请输入并核对学校财务邮箱。")
    number = invoice["invoice_number"]
    due = settlement.get("due_date") or settlement["delivery"]["due_date"]
    outstanding = settlement["outstanding_cents"]
    amount = f"AUD {outstanding // 100:,}.{outstanding % 100:02d}"
    body = (
        "Dear Accounts Payable team,\n\n"
        f"We are following up on commission invoice {number}, due on {due}. "
        f"The outstanding balance is {amount}. Please see the attached invoice.\n\n"
        "If payment has already been made, please send the remittance advice so we can update our records.\n\n"
        "Kind regards,\nAccounts Receivable"
    )
    draft = dict(invoice_uid=invoice["uid"], recipient=recipient, due_date=due,
                 outstanding_cents=outstanding, document_uid=invoice["document_uid"],
                 subject=f"Payment reminder for commission invoice {number}", body=body)
    return {**draft, "digest": hashlib.sha256(canonical_json(draft).encode()).hexdigest()}


def record_reminder(settlement, invoice_uid, recipient, digest, key, confirmed):
    if not confirmed or not key or not digest:
        raise ValueError("请核对催款内容并确认已经发送。")
    request = dict(invoice_uid=invoice_uid, recipient=recipient, digest=digest,
                   actor=settlement.actor, simulation=settlement.simulation)

    def record():
        draft = draft_reminder(settlement.detail(invoice_uid), recipient)
        if draft["digest"] != digest:
            raise ValueError("发票余额或催款内容已变更，请重新预览。")
        return {**request, "uid": invoice_uid, "due_date": draft["due_date"],
                "outstanding_cents": draft["outstanding_cents"], "document_uid": draft["document_uid"]}

    return settlement.repository.command(
        idempotency_key=f"school-reminder:{key}", command_type=ACTION, aggregate_type="invoice",
        payload=request, actor_user_id=settlement.actor, operation=record,
    )


def reminder_history(connection, invoice_uid):
    return [
        {**json.loads(row["after_json"]), "occurred_at": row["occurred_at"]}
        for row in connection.execute(
            """SELECT after_json,occurred_at FROM revenue_audit_events
               WHERE action=? AND aggregate_uid=? ORDER BY id DESC""", (ACTION, invoice_uid),
        )
    ]
