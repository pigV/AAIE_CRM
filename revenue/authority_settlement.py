"""Isolated bank reconciliation against immutable authority invoice drafts."""

from datetime import date, datetime
from hashlib import sha256
import json
import re
import unicodedata
from zoneinfo import ZoneInfo

from revenue.repository import new_uid
from revenue.state_school_commission import STATE_AUTHORITIES


PAYMENT = "authority_commission_rehearsal_payment"
RECONCILIATION = "authority_commission_rehearsal_reconciled"


class AuthoritySettlement:
    def __init__(self, repository, invoice, actor, *, rehearsal):
        kind, state = invoice["target"]
        if not rehearsal or invoice.get("simulation") is not True or kind != "authority" or state not in STATE_AUTHORITIES:
            raise PermissionError("到账演练仅开放给隔离环境中的州教育局测试发票。")
        self.repo = repository
        self.invoice = invoice
        self.actor = actor
        self.state = state

    def summary(self):
        records = self.repo.connection.execute(
            """SELECT action,after_json FROM revenue_audit_events
                 WHERE action IN (?,?) AND json_extract(after_json,'$.invoice_uid')=?
                   AND json_extract(after_json,'$.state')=?
                 ORDER BY id""",
            (PAYMENT, RECONCILIATION, self.invoice["uid"], self.state),
        )
        lines = {line["uid"]: line["gross_cents"] for line in self.invoice["lines"]}
        payments, reconciliation = [], None
        for action, raw in records:
            item = json.loads(raw)
            if action == RECONCILIATION:
                reconciliation = item
                continue
            payments.append(item)
            for key, cents in item["allocations"].items():
                lines[key] -= cents
        received = sum(item["amount_cents"] for item in payments)
        outstanding = self.invoice["gross_cents"] - received
        status = "unpaid"
        if reconciliation:
            status = "reconciled"
        elif not outstanding:
            status = "awaiting_reconciliation"
        elif received:
            status = "partial"
        return {"status": status, "received_cents": received, "outstanding_cents": outstanding,
                "line_balances": lines, "payments": payments, "reconciliation": reconciliation}

    def record(self, state, reference, received_on, amount, allocations, proof):
        if state != self.state:
            raise ValueError("银行流水付款方与发票所属教育局不一致。")
        reference = " ".join(unicodedata.normalize("NFKC", reference).upper().split())
        if not reference or len(reference) > 100 or type(amount) is not int or amount <= 0:
            raise ValueError("请填写正数金额和有效银行流水号。")
        try:
            received_on = date.fromisoformat(received_on).isoformat()
        except ValueError:
            raise ValueError("请填写有效的到账日期。") from None
        if received_on > datetime.now(ZoneInfo("Australia/Melbourne")).date().isoformat():
            raise ValueError("到账日期不能晚于今天。")
        if (not allocations or any(type(cents) is not int or cents <= 0 for cents in allocations.values())
                or sum(allocations.values()) != amount):
            raise ValueError("学生明细分配合计须等于本次到账金额。")
        payload = {"invoice_uid": self.invoice["uid"], "state": self.state, "received_on": received_on,
                   "amount_cents": amount, "reference": reference, "allocations": allocations,
                   "evidence_sha256": proof.sha256}

        def operation():
            current = self.summary()
            if current["reconciliation"] or amount > current["outstanding_cents"]:
                raise ValueError("发票已核销或到账超过未收余额。")
            if any(key not in current["line_balances"] or cents > current["line_balances"][key]
                   for key, cents in allocations.items()):
                raise ValueError("到账分配须对应本发票且不得超出学生明细余额。")
            return {**payload, "uid": new_uid("authority-payment"), "confirmed_by": self.actor,
                    "confirmed_at": self.repo.now(), "evidence_name": proof.original_filename,
                    "evidence_path": str(proof.path), "evidence_size": proof.size_bytes}

        reference_key = sha256(reference.encode()).hexdigest()
        return self.repo.command(
            idempotency_key=f"authority-rehearsal-bank:{reference_key}", command_type=PAYMENT,
            aggregate_type="authority_rehearsal_payment", payload=payload, actor_user_id=self.actor,
            operation=operation,
        )

    def reconcile(self, reviewed):
        if reviewed is not True:
            raise ValueError("请先核对银行流水和学生明细分配。")
        uid = self.invoice["uid"]
        payload = {"invoice_uid": uid, "state": self.state, "terms_revision": self.invoice["terms_revision"]}

        def operation():
            current = self.summary()
            if current["outstanding_cents"] or any(current["line_balances"].values()) or not current["payments"]:
                raise ValueError("全部到账收齐并完成明细分配后才能核销。")
            return {**payload, "uid": new_uid("authority-reconciliation"), "confirmed_by": self.actor,
                    "confirmed_at": self.repo.now(), "amount_cents": current["received_cents"],
                    "payment_uids": [item["uid"] for item in current["payments"]]}

        return self.repo.command(
            idempotency_key=f"authority-rehearsal-reconcile:{uid}", command_type=RECONCILIATION,
            aggregate_type="authority_rehearsal_reconciliation", payload=payload, actor_user_id=self.actor,
            operation=operation,
        )
