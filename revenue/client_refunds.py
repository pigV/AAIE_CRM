"""Record actual client refunds against the immutable receipt allocation."""
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo

from revenue.operational_visibility import RevenueOperationalVisibility
from revenue.repository import RevenueRepository, new_uid


EVIDENCE_KIND = "client_refund_evidence"
MELBOURNE = ZoneInfo("Australia/Melbourne")


def refunds_available(connection):
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='revenue_client_cash_movements'"
    ).fetchone())


def paid_refund_totals(connection, payment_uid):
    return connection.execute(
        """SELECT COALESCE(SUM(amount_cents),0),COALESCE(SUM(refunded_base_cents),0),
                  COALESCE(SUM(refunded_gst_cents),0),
                  COUNT(*) FILTER (WHERE client_payment_allocation_id IS NULL)
             FROM revenue_refunds WHERE payment_uid=? AND status='paid'""",
        (payment_uid,),
    ).fetchone()


def payment_refund_details(connection, payments):
    """Annotate the existing payment rows, including their immutable payout history."""
    by_uid = {row["payment_uid"]: row for row in payments}
    if not by_uid or not refunds_available(connection):
        return
    for payment in by_uid.values():
        payment.update(refund_remaining_cents=payment["amount_cents"], refunds=[])
    blocked = set()
    marks = ",".join("?" for _ in by_uid)
    for row in connection.execute(
        """SELECT r.*,d.uid AS evidence_document_uid,u.username AS operator
             FROM revenue_refunds r
             LEFT JOIN revenue_documents d ON d.id=r.evidence_document_id
             LEFT JOIN users u ON u.id=r.recorded_by
            WHERE r.status='paid' AND r.payment_uid IN (""" + marks + """)
            ORDER BY julianday(r.refunded_at) DESC,r.id DESC""",
        tuple(by_uid),
    ):
        payment = by_uid[row["payment_uid"]]
        payment["refund_remaining_cents"] = max(0, payment["refund_remaining_cents"] - row["amount_cents"])
        if row["client_payment_allocation_id"] is None:
            blocked.add(row["payment_uid"])
        else:
            payment["refunds"].append(dict(row))
    for uid in blocked:
        by_uid[uid].update(refund_remaining_cents=0, refund_requires_review=True)


def can_view_refund_evidence(connection, document_uid):
    if not refunds_available(connection):
        return False
    visible = RevenueOperationalVisibility(connection).case_predicate("c")
    return bool(connection.execute(
        """SELECT 1 FROM revenue_refunds r
             JOIN revenue_documents d ON d.id=r.evidence_document_id
             JOIN revenue_client_payment_allocations a ON a.id=r.client_payment_allocation_id
             JOIN revenue_payments p ON p.id=a.payment_id AND p.uid=r.payment_uid
             JOIN revenue_fee_items f ON f.id=a.fee_item_id
             JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id
            WHERE d.uid=? AND d.document_kind=? AND r.status='paid' AND """ + visible,
        (document_uid, EVIDENCE_KIND),
    ).fetchone())


def fee_refund_totals(connection, fee_ids):
    fee_ids = tuple(fee_ids)
    if not fee_ids or not refunds_available(connection):
        return {}
    marks = ",".join("?" for _ in fee_ids)
    return {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            """SELECT a.fee_item_id,SUM(r.refunded_base_cents),SUM(r.refunded_gst_cents)
                 FROM revenue_refunds r
                 JOIN revenue_client_payment_allocations a ON a.id=r.client_payment_allocation_id
                WHERE r.status='paid' AND a.fee_item_id IN (""" + marks + """)
                GROUP BY a.fee_item_id""",
            fee_ids,
        )
    }


def client_cash_totals(connection, *, start=None, end=None, case_uid="", case_predicate="1"):
    return [dict(row) for row in connection.execute(
        """SELECT m.currency_code,
                  SUM(CASE WHEN m.movement_kind='receipt' THEN m.actual_cents ELSE 0 END) AS received_actual_cents,
                  -SUM(CASE WHEN m.movement_kind='refund' THEN m.actual_cents ELSE 0 END) AS refunded_actual_cents,
                  SUM(m.actual_cents) AS net_actual_cents,
                  SUM(m.base_cents) AS net_base_cents,SUM(m.gst_cents) AS net_gst_cents
             FROM revenue_client_cash_movements m
             JOIN revenue_commercial_cases c ON c.id=m.commercial_case_id
            WHERE (? IS NULL OR m.effective_date>=?) AND (? IS NULL OR m.effective_date<=?)
              AND (?='' OR c.uid=?) AND """ + case_predicate + """
            GROUP BY m.currency_code ORDER BY m.currency_code""",
        (start, start, end, end, case_uid, case_uid),
    )]


def _local_refund_time(value, offset):
    if offset not in {"", "+10:00", "+11:00"} or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?", value):
        raise ValueError("请输入有效的墨尔本实际退款日期和时间。")
    try:
        local = datetime.fromisoformat(value)
        possibilities = {
            candidate.isoformat(): candidate
            for fold in (0, 1)
            if (candidate := local.replace(tzinfo=MELBOURNE, fold=fold))
            .astimezone(timezone.utc).astimezone(MELBOURNE).replace(tzinfo=None) == local
        }
    except (ValueError, OverflowError) as exc:
        raise ValueError("请输入有效的墨尔本实际退款日期和时间。") from exc
    matches = [dt for iso, dt in possibilities.items() if not offset or iso.endswith(offset)]
    if len(matches) != 1:
        raise ValueError("该墨尔本时间不存在或处在夏令时切换的重复时段，请核对时间及偏移。")
    return matches[0]


def _proportional_cents(amount, total, share):
    return (2 * amount * share + total) // (2 * total)


class ClientRefundService:
    def __init__(self, repository: RevenueRepository):
        self.repository = repository
        self.connection = repository.connection

    def record(
        self, *, payment_uid, actual_amount_cents, refunded_at, reason, evidence_document_uid,
        actor_user_id, actor_role, idempotency_key, melbourne_offset="",
    ):
        if actor_role != "Admin":
            raise PermissionError("Refunds require Revenue Admin authority")
        if type(actual_amount_cents) is not int or not 0 < actual_amount_cents <= 2**63 - 1:
            raise ValueError("请输入大于零的有效退款金额。")
        reason = str(reason or "").strip()
        if not reason or len(reason) > 1000:
            raise ValueError("请输入退款原因（最多 1000 字）。")
        effective = _local_refund_time(str(refunded_at or ""), melbourne_offset)
        now = datetime.fromisoformat(self.repository.now())
        if effective > now:
            raise ValueError("实际退款时间不能晚于当前时间。")
        payload = {
            "payment_uid": payment_uid, "amount_cents": actual_amount_cents,
            "refunded_at": effective.isoformat(), "reason": reason,
            "evidence_document_uid": evidence_document_uid, "recorded_by": actor_user_id,
        }

        def operation():
            payment = self.connection.execute(
                """SELECT p.*,a.id AS allocation_id,a.allocated_base_cents,a.gst_cents,f.commercial_case_id
                     FROM revenue_payments p
                     JOIN revenue_client_payment_allocations a ON a.payment_id=p.id
                     JOIN revenue_fee_items f ON f.id=a.fee_item_id
                     JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id
                    WHERE p.uid=? AND p.status='confirmed' AND p.payment_direction='received'
                      AND p.counterparty_kind='client' AND """
                + RevenueOperationalVisibility(self.connection).case_predicate("c"),
                (payment_uid,),
            ).fetchone()
            if not payment:
                raise ValueError("请选择已确认到账的客户收款。")
            if effective.date().isoformat() < payment["received_date"]:
                raise ValueError("实际退款日期不能早于原收款日期。")
            if payment["currency_code"] == "AUD" and (
                payment["allocated_base_cents"] + payment["gst_cents"] != payment["amount_cents"]
            ):
                raise ValueError("此笔收款的原澳元分配需要核对。")
            paid, refunded_base, refunded_gst, legacy_paid = paid_refund_totals(self.connection, payment_uid)
            if legacy_paid:
                raise ValueError("此笔收款有缺少明细的历史实退记录，请先核对。")
            cumulative = paid + actual_amount_cents
            if cumulative > payment["amount_cents"]:
                raise ValueError("退款金额超过此笔收款的可退款余额。")
            document = self.connection.execute(
                "SELECT id FROM revenue_documents WHERE uid=? AND document_kind=?",
                (evidence_document_uid, EVIDENCE_KIND),
            ).fetchone()
            if not document:
                raise ValueError("请提供有效的退款凭证。")

            gst = _proportional_cents(cumulative, payment["amount_cents"], payment["gst_cents"]) - refunded_gst
            if payment["currency_code"] == "AUD":
                base = actual_amount_cents - gst
            else:
                base = _proportional_cents(
                    cumulative, payment["amount_cents"], payment["allocated_base_cents"]
                ) - refunded_base
            uid = new_uid("refund")
            self.connection.execute(
                """INSERT INTO revenue_refunds(
                     uid,payment_uid,amount_cents,reason,evidence_document_id,status,created_at,
                     client_payment_allocation_id,currency_code,refunded_base_cents,
                     refunded_gst_cents,refunded_at,recorded_by
                   ) VALUES(?,?,?,?,?,'paid',?,?,?,?,?,?,?)""",
                (uid, payment_uid, actual_amount_cents, reason, document["id"], self.repository.now(),
                 payment["allocation_id"], payment["currency_code"], base, gst, effective.isoformat(), actor_user_id),
            )
            self._ledger(uid, payment, actual_amount_cents, base, gst, effective.isoformat(), actor_user_id)
            return self.repository.require_by_uid("revenue_refunds", uid)

        with self.repository.fee_write_transaction():
            return self.repository.command(
                idempotency_key=idempotency_key, command_type="client_refund_recorded",
                aggregate_type="refund", payload=payload, actor_user_id=actor_user_id,
                operation=operation,
            )

    def _ledger(self, uid, payment, actual, base, gst, effective, actor_user_id):
        components = (
            ("client_refund_actual", actual, payment["currency_code"], "ClientRefund", "Bank"),
            ("client_refund_base", base, "AUD", "ClientFeeRefunds", "ClientRefundsAllocated"),
            ("client_refund_gst", gst, "AUD", "ClientGSTRefunds", "ClientRefundsAllocated"),
        )
        for kind, amount, currency, debit, credit in components:
            if not amount:
                continue
            self.connection.executemany(
                """INSERT INTO revenue_ledger_entries(
                     uid,account_code,event_type,source_kind,source_uid,commercial_case_id,
                     debit_cents,credit_cents,currency_code,occurred_at,created_by
                   ) VALUES(?,?,'refund',?,?,?,?,?,?,?,?)""",
                (
                    (new_uid("ledger"), account, kind, uid, payment["commercial_case_id"],
                     dr, cr, currency, effective, actor_user_id)
                    for account, dr, cr in ((debit, amount, 0), (credit, 0, amount))
                ),
            )
