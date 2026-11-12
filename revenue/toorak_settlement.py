"""Audited receipts and settlement for school commission invoices."""

from collections import defaultdict
from datetime import date, datetime, timedelta
import hashlib
import json
import unicodedata
from zoneinfo import ZoneInfo

from revenue.invoice_templates import FORMAL_INVOICE_KIND, TEST_INVOICE_KIND
from revenue.repository import canonical_json, new_uid


FORMAL_DELIVERY = "school_commission_delivery_confirmed"
FORMAL_PAYMENT = "school_commission_payment_allocated"
FORMAL_RECONCILIATION = "school_commission_reconciliation_confirmed"
TEST_DELIVERY = "toorak_invoice_delivery_confirmed"
TEST_PAYMENT = "toorak_school_payment_allocated"
TEST_RECONCILIATION = "toorak_invoice_reconciliation_confirmed"


def _today():
    return datetime.now(ZoneInfo("Australia/Melbourne")).date().isoformat()


def _audit_record(connection, action, invoice_uid):
    row = connection.execute(
        """SELECT after_json FROM revenue_audit_events
            WHERE action=? AND json_extract(after_json,'$.invoice_uid')=? ORDER BY id DESC LIMIT 1""",
        (action,invoice_uid),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _allocated_payments(connection, invoice_line_ids):
    if isinstance(invoice_line_ids,int):
        invoice_line_ids = (invoice_line_ids,)
    marks = ",".join("?" for _ in invoice_line_ids)
    return [dict(row) for row in connection.execute(
        f"""SELECT p.uid,p.external_reference,p.received_date,p.amount_cents,p.allocated_cents,
                   p.evidence_document_id,p.revenue_entity_id,p.currency_code,p.counterparty_kind,
                   p.payment_direction,a.uid AS allocation_uid,a.allocated_cents AS invoice_cents,
                   il.id AS invoice_line_id,il.uid AS invoice_line_uid,d.uid AS document_uid,d.document_kind
              FROM revenue_payment_allocations a JOIN revenue_payments p ON p.id=a.payment_id
              JOIN revenue_invoice_lines il ON il.id=a.invoice_line_id
              LEFT JOIN revenue_documents d ON d.id=p.evidence_document_id
             WHERE il.id IN ({marks}) AND p.status='confirmed' ORDER BY p.received_date,p.id,il.sort_order,il.id""",
        invoice_line_ids,
    )]


def settlement_summary(connection, invoice_uid, invoice_line_id, gross_cents, *, formal=False, due_date=None):
    delivery = _audit_record(connection,FORMAL_DELIVERY if formal else TEST_DELIVERY,invoice_uid)
    reconciliation = _audit_record(connection,FORMAL_RECONCILIATION if formal else TEST_RECONCILIATION,invoice_uid)
    payments = _allocated_payments(connection,invoice_line_id)
    received = sum(payment["invoice_cents"] for payment in payments)
    outstanding = gross_cents-received
    due = due_date or (delivery["due_date"] if delivery else None)
    full_date = payments[-1]["received_date"] if payments and outstanding == 0 else None
    if reconciliation:
        status = "reconciled"
    elif outstanding == 0:
        status = "awaiting_reconciliation"
    elif received:
        status = "partial"
    else:
        status = "unpaid"
    timing = None
    overdue_days = 0
    if due and full_date:
        timing = "late" if full_date > due else "due_day" if full_date == due else "early"
    elif due and outstanding > 0:
        overdue_days = max(0, (date.fromisoformat(_today()) - date.fromisoformat(due)).days)
        if overdue_days:
            timing = "overdue"
    return {"delivery":delivery,"reconciliation":reconciliation,"payments":payments,
            "received_cents":received,"outstanding_cents":outstanding,"status":status,
            "timing":timing,"fully_received_on":full_date,"overdue_days":overdue_days,"due_date":due}


class ToorakSettlement:
    def __init__(self, service, actor, role, *, formal=False, entity_id=None, simulation=True):
        if role != "Admin":
            raise PermissionError("School commission settlement requires Admin")
        self.service = service
        self.repository = service.repository
        self.connection = service.connection
        self.actor = actor
        self.formal = formal
        self.entity_id = entity_id
        self.simulation = simulation
        self.workflow = "school-commission" if formal else "toorak"
        self.invoice_kind = FORMAL_INVOICE_KIND if formal else TEST_INVOICE_KIND
        self.delivery_kind = "school_commission_delivery_evidence" if formal else "toorak_invoice_delivery_evidence"
        self.remittance_kind = "school_commission_remittance_evidence" if formal else "toorak_school_remittance_evidence"

    def _invoice(self, case_uid, invoice_uid):
        invoice = self.connection.execute(
            """SELECT i.*,d.uid AS document_uid,entity.legal_name,c.uid AS case_uid
                 FROM revenue_invoices i
                 JOIN revenue_documents d ON d.id=i.document_id
                 JOIN revenue_entities entity ON entity.id=i.revenue_entity_id
                 LEFT JOIN revenue_commercial_cases c ON c.id=i.commercial_case_id
                WHERE i.uid=? AND i.document_kind='direct_commission_invoice' AND i.status='issued'
                  AND i.currency_code='AUD' AND d.document_kind=?
                  AND ((?=1 AND i.revenue_entity_id=?) OR (?=0 AND lower(trim(entity.legal_name))='toorak college'))
                  AND (? IS NULL OR i.revenue_entity_id=?)""",
            (invoice_uid,self.invoice_kind,self.formal,self.entity_id,self.formal,self.entity_id,self.entity_id),
        ).fetchone()
        error = "请选择已签发的学校佣金发票。"
        if not invoice or (case_uid is not None and case_uid != invoice["case_uid"]):
            raise ValueError(error)
        lines = [dict(row) for row in self.connection.execute(
            """SELECT il.id AS invoice_line_id,il.uid AS invoice_line_uid,il.description,il.student_id,
                      il.net_cents,il.gst_cents,il.gross_cents,claim.claim_number,c.id AS commercial_case_id,
                      l.id AS commission_line_id,l.status AS commission_status,
                      ins.id AS instalment_id,ins.status AS instalment_status,
                      json_extract(ev.authoritative_facts_json,'$.payment_days') AS payment_days
                 FROM revenue_invoice_lines il
                 JOIN revenue_claims claim ON claim.uid=il.source_uid AND claim.status='issued'
                 JOIN revenue_claim_allocations ca ON ca.claim_id=claim.id
                 JOIN revenue_commission_instalments ins ON ins.id=ca.commission_instalment_id
                 JOIN revenue_commission_lines l ON l.id=ins.commission_line_id
                 JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id
                 JOIN revenue_claim_eligibility_evaluations ev ON ev.claim_id=claim.id
                WHERE il.invoice_id=? AND il.source_kind='claim'
                  AND ev.eligible=1 AND json_extract(ev.authoritative_facts_json,'$.toorak_claim')=1
                  AND COALESCE(json_extract(ev.authoritative_facts_json,'$.school_commission_formal'),0)=?
                  AND (?=0 OR json_extract(ev.authoritative_facts_json,'$.school_commission_simulation')=?)
                  AND ev.commission_line_id=l.id
                  AND claim.revenue_entity_id=? AND l.revenue_entity_id=?
                  AND il.student_id=c.student_id AND il.net_cents=claim.net_cents
                  AND il.gst_cents=claim.gst_cents AND il.gross_cents=claim.gross_cents
                  AND il.gross_cents=ca.claimed_cents
                ORDER BY il.sort_order,il.id""",
            (invoice["id"],self.formal,self.formal,self.simulation,
             invoice["revenue_entity_id"],invoice["revenue_entity_id"]),
        )]
        expected = self.connection.execute(
            "SELECT count(*) FROM revenue_invoice_lines WHERE invoice_id=?",(invoice["id"],),
        ).fetchone()[0]
        if (not lines or len(lines)!=expected
                or len({line["invoice_line_id"] for line in lines})!=expected
                or len({line["instalment_id"] for line in lines})!=expected
                or (invoice["commercial_case_id"] is not None
                    and (len(lines)!=1 or lines[0]["commercial_case_id"]!=invoice["commercial_case_id"]))
                or any(sum(line[key] for line in lines)!=invoice[key]
                       for key in ("net_cents","gst_cents","gross_cents"))):
            raise ValueError(error)
        result = {**dict(invoice),"lines":lines}
        if self.formal:
            terms = {line["payment_days"] for line in lines}
            if len(terms)!=1 or not terms <= {0,7,15,30}:
                raise ValueError("正式发票缺少统一的合同付款期限。")
            result["payment_days"] = terms.pop()
        return result

    def detail(self, invoice_uid):
        invoice = self._invoice(None,invoice_uid)
        return self._with_settlement(invoice)

    def _with_settlement(self, invoice):
        lines = invoice["lines"]
        summary = settlement_summary(self.connection,invoice["uid"],
                                     [line["invoice_line_id"] for line in lines],invoice["gross_cents"],
                                     formal=self.formal, due_date=invoice.get("due_date"))
        paid = defaultdict(int)
        for payment in summary["payments"]:
            paid[payment["invoice_line_id"]] += payment["invoice_cents"]
        for line in lines:
            line["received_cents"] = paid[line["invoice_line_id"]]
            line["outstanding_cents"] = line["gross_cents"]-line["received_cents"]
        return {**invoice,"settlement":summary}

    @staticmethod
    def _date(value):
        day = date.fromisoformat(value).isoformat()
        if day != value or day > _today():
            raise ValueError("日期须为已发生的有效日期。")
        return day

    def _document(self, staged, kind, key):
        existing = self.connection.execute(
            """SELECT * FROM revenue_documents WHERE document_kind=? AND storage_path=?
                AND sha256=? AND size_bytes=?""",(kind,str(staged.path),staged.sha256,staged.size_bytes),
        ).fetchone()
        return dict(existing) if existing else self.service.register_document(
            kind,str(staged.path),staged.sha256,staged.mime_type,staged.size_bytes,"upload",self.actor,key,
        )

    def _valid_payment_days(self, term_days):
        return type(term_days) is int and term_days in (0, 7, 15, 30)

    def _payment_command_key(self, invoice, token):
        return f'{self.workflow}-school-bank:{invoice["revenue_entity_id"]}:{token}'

    def _reconciliation_lines_valid(self, lines):
        return all(line["commission_status"] == "claimed" and line["instalment_status"] == "claimed"
                   for line in lines)

    def _mark_reconciled_lines(self, lines):
        self.connection.executemany(
            "UPDATE revenue_commission_instalments SET status='paid' WHERE id=?",
            [(line["instalment_id"],) for line in lines],
        )
        self.connection.executemany(
            "UPDATE revenue_commission_lines SET status='paid' WHERE id=?",
            [(line["commission_line_id"],) for line in lines],
        )

    def delivery(self, case_uid, invoice_uid, received_on, staged, term_days=30):
        received_on = self._date(received_on)
        if not self._valid_payment_days(term_days):
            raise ValueError("请选择立即到期、7 天、15 天或 30 天。")
        invoice = self._invoice(case_uid,invoice_uid)
        payload = dict(case_uid=invoice["case_uid"],invoice_uid=invoice_uid,received_on=received_on,sha256=staged.sha256)
        # Default to the historical request shape so existing 30-day confirmations can be replayed.
        if self.formal or term_days != 30:
            payload["term_days"] = term_days

        def operation():
            invoice = self._invoice(case_uid,invoice_uid)
            if received_on < invoice["issue_date"]:
                raise ValueError("学校收件日不得早于发票签发日。")
            if self.formal and term_days!=invoice["payment_days"]:
                raise ValueError("付款期限必须与已签发发票引用的学校合同一致。")
            document = self._document(staged,self.delivery_kind,f"{self.workflow}-delivery-document:{invoice_uid}")
            return {"uid":invoice_uid,"invoice_uid":invoice_uid,"received_on":received_on,
                    "due_date":invoice.get("due_date") or (date.fromisoformat(received_on)+timedelta(days=term_days)).isoformat(),
                    "term_days":term_days,"document_uid":document["uid"],
                    **({"simulation":self.simulation} if self.formal else {})}

        return self.repository.command(
            idempotency_key=f"{self.workflow}-delivery:{invoice_uid}",command_type=FORMAL_DELIVERY if self.formal else TEST_DELIVERY,
            aggregate_type="invoice",payload=payload,actor_user_id=self.actor,operation=operation,
        )

    def payment(self, case_uid, invoice_uid, amount, received_on, reference, staged, allocations=None):
        received_on = self._date(received_on)
        reference = " ".join(unicodedata.normalize("NFKC",reference).upper().split())
        if amount <= 0 or not reference or len(reference)>100:
            raise ValueError("请填写正数 AUD 金额和有效银行流水参考号。")
        invoice = self._invoice(case_uid,invoice_uid)
        token = hashlib.sha256(reference.encode()).hexdigest()
        lines = {line["invoice_line_uid"]:line for line in invoice["lines"]}
        if allocations is None:
            if len(lines)!=1:
                raise ValueError("请逐条填写学生明细的到账分配。")
            allocations = {next(iter(lines)):amount}
        if (not allocations or not allocations.keys()<=lines.keys()
                or any(type(cents) is not int or cents<=0 for cents in allocations.values())
                or sum(allocations.values())!=amount):
            raise ValueError("到账分配必须对应本发票明细，且合计等于本次到账金额。")
        payload = dict(case_uid=invoice["case_uid"],invoice_uid=invoice_uid,amount_cents=amount,
                       received_on=received_on,bank_reference=reference,sha256=staged.sha256)
        if len(lines)>1:
            payload["allocations"] = allocations

        def operation():
            current = self._with_settlement(self._invoice(case_uid,invoice_uid))
            summary = current["settlement"]
            current_lines = {line["invoice_line_uid"]:line for line in current["lines"]}
            if not summary["delivery"]:
                raise ValueError("须先确认学校收到有效发票。")
            if received_on < summary["delivery"]["received_on"]:
                raise ValueError("到账日不得早于已确认的学校收件日。")
            if summary["reconciliation"] or amount > summary["outstanding_cents"]:
                raise ValueError("发票已核销或本次到账超出未收余额，请复核。")
            if any(uid not in current_lines or cents>current_lines[uid]["outstanding_cents"]
                   for uid,cents in allocations.items()):
                raise ValueError("本次分配超出学生明细未收余额，请复核。")
            if self.connection.execute(
                """SELECT 1 FROM revenue_payments WHERE counterparty_kind='institution'
                    AND revenue_entity_id=? AND upper(trim(external_reference))=?""",
                (current["revenue_entity_id"],reference),
            ).fetchone():
                raise ValueError("银行流水参考号已存在，请核对发票和金额。")
            document = self._document(staged,self.remittance_kind,f"{self.workflow}-payment-document:{invoice_uid}:{token}")
            now,uid = self.repository.now(),new_uid("payment")
            payment_id, = self.connection.execute(
                """INSERT INTO revenue_payments(uid,payment_direction,counterparty_kind,revenue_entity_id,
                       external_reference,received_date,amount_cents,allocated_cents,currency_code,
                       status,evidence_document_id,confirmed_by,confirmed_at)
                   VALUES(?,'received','institution',?,?,?,?,?,'AUD','confirmed',?,?,?) RETURNING id""",
                (uid,current["revenue_entity_id"],reference,received_on,amount,amount,document["id"],self.actor,now),
            ).fetchone()
            allocation_uids = []
            for line_uid,cents in sorted(allocations.items()):
                allocation_uid = new_uid("allocation")
                self.connection.execute(
                    """INSERT INTO revenue_payment_allocations(uid,payment_id,invoice_line_id,
                          allocated_cents,confirmed_by,confirmed_at) VALUES(?,?,?,?,?,?)""",
                    (allocation_uid,payment_id,current_lines[line_uid]["invoice_line_id"],cents,self.actor,now),
                )
                allocation_uids.append(allocation_uid)
            if self.formal:
                self.service.append_balanced_ledger_event(
                    "received","school_commission_payment",uid,amount,"Bank","CommissionReceivable",
                    self.actor,commercial_case_id=current["commercial_case_id"],
                )
            result = {"uid":uid,"invoice_uid":invoice_uid,
                      "outstanding_cents":summary["outstanding_cents"]-amount,"bank_reference":reference,
                      **({"simulation":self.simulation} if self.formal else {})}
            if len(current_lines)==1:
                result["allocation_uid"] = allocation_uids[0]
            else:
                result["allocation_uids"] = allocation_uids
            return result

        return self.repository.command(
            idempotency_key=self._payment_command_key(invoice,token),
            command_type=FORMAL_PAYMENT if self.formal else TEST_PAYMENT,aggregate_type="payment",
            payload=payload,actor_user_id=self.actor,operation=operation,
        )

    def reconcile(self,case_uid,invoice_uid,reviewed):
        if not reviewed:
            raise ValueError("请确认已核对发票与全部学校银行流水。")

        invoice = self._invoice(case_uid,invoice_uid)

        def operation():
            current = self._with_settlement(self._invoice(case_uid,invoice_uid))
            summary = current["settlement"]
            lines = current["lines"]
            if not summary["delivery"] or any(line["outstanding_cents"]!=0 for line in lines) or not summary["payments"]:
                raise ValueError("学校收件及全部实收核对完成后才能核销。")
            payment_totals = defaultdict(int)
            for payment in summary["payments"]:
                payment_totals[payment["uid"]] += payment["invoice_cents"]
            if (not self._reconciliation_lines_valid(lines)
                    or any(payment["counterparty_kind"]!="institution" or payment["payment_direction"]!="received"
                           or payment["revenue_entity_id"]!=current["revenue_entity_id"] or payment["currency_code"]!="AUD"
                           or payment_totals[payment["uid"]]!=payment["amount_cents"]
                           or payment_totals[payment["uid"]]!=payment["allocated_cents"]
                           or payment["document_kind"]!=self.remittance_kind
                           or payment["received_date"]<summary["delivery"]["received_on"]
                           for payment in summary["payments"])):
                raise ValueError("佣金状态或学校流水归属有冲突，请复核。")
            now,uid = self.repository.now(),new_uid("reconciliation")
            run_id, = self.connection.execute(
                """INSERT INTO revenue_reconciliation_runs(uid,reconciliation_kind,source_document_id,
                      status,created_by,created_at,confirmed_by,confirmed_at)
                   VALUES(?,'payment',?,'confirmed',?,?,?,?) RETURNING id""",
                (uid,current["document_id"],self.actor,now,self.actor,now),
            ).fetchone()
            for payment in summary["payments"]:
                evidence = {key:payment[key] for key in ("external_reference","received_date","document_uid","allocation_uid")}
                self.connection.execute(
                    """INSERT INTO revenue_reconciliation_suggestions(uid,reconciliation_run_id,source_item_uid,
                          suggested_target_kind,suggested_target_uid,suggested_amount_cents,confidence_basis_points,
                          evidence_json,review_status,reviewed_by,reviewed_at)
                       VALUES(?,?,?,?,?,?,10000,?,'confirmed',?,?)""",
                    (new_uid("reconciliation-item"),run_id,payment["uid"],
                     "school_commission_invoice_line" if self.formal else "toorak_test_invoice_line",payment["invoice_line_uid"],
                     payment["invoice_cents"],canonical_json(evidence),self.actor,now),
                )
            self._mark_reconciled_lines(lines)
            return {"uid":uid,"invoice_uid":invoice_uid,"status":"confirmed","confirmed_by":self.actor,
                    "confirmed_at":now,"payment_uids":list(payment_totals),
                    "fully_received_on":summary["fully_received_on"],"timing":summary["timing"],
                    **({"simulation":self.simulation} if self.formal else {})}

        return self.repository.command(
            idempotency_key=f"{self.workflow}-reconcile:{invoice_uid}",command_type=FORMAL_RECONCILIATION if self.formal else TEST_RECONCILIATION,
            aggregate_type="reconciliation",payload=dict(case_uid=invoice["case_uid"],invoice_uid=invoice_uid),
            actor_user_id=self.actor,operation=operation,
        )
