"""Formal education-authority invoice records and payer registration."""

from hashlib import sha256
from pathlib import Path

from revenue import vic_rcti
from revenue.data_invoice_storage import configured_invoice_store
from revenue.invoice_templates import (
    FORMAL_INVOICE_KIND, current_invoice_template_revisions, render_with_active_template,
)
from revenue.repository import canonical_json, new_uid
from revenue.school_invoice_mail import dispatch_next, enqueue_mail, mail_settings, mail_status, retry_mail
from revenue.secure_storage import StorageUnavailable
from revenue.services import RevenueService
from revenue.state_school_commission import STATE_AUTHORITIES
from revenue.toorak_settlement import ToorakSettlement, _today


ISSUED = "authority_commission_invoice_issued"


def authority_schema_ready(connection):
    return connection.execute(
        """SELECT count(*) FROM sqlite_master
           WHERE (type='table' AND name IN ('revenue_authority_payers','revenue_authority_invoices'))
              OR (type='index' AND name='uq_revenue_authority_invoice_line')"""
    ).fetchone()[0] == 3


def register_authority_payer(repository, state, legal_name, actor, *, rehearsal, confirmed_terms=None):
    if state not in STATE_AUTHORITIES or (not rehearsal and confirmed_terms is None):
        raise PermissionError("请选择已确认合同的州教育局。")
    legal_name = str(legal_name or "").strip()
    if not legal_name or len(legal_name) > 200:
        raise ValueError("请填写教育局的法定付款主体。")
    if confirmed_terms is not None and (confirmed_terms.get("target") != ["authority", state]
                                       or confirmed_terms.get("payer") != legal_name):
        raise ValueError("法定付款主体须与本州已确认合同一致。")
    if not authority_schema_ready(repository.connection):
        raise ValueError("教育局正式配置尚未开放。")
    existing = authority_payer(repository.connection, state)
    if existing and existing["legal_name"] != legal_name:
        raise ValueError("该州已登记其他法定付款主体，请先由财务核对。")

    def operation():
        entity_id = repository.connection.execute(
            """INSERT INTO revenue_entities(uid,entity_type,legal_name,created_by,created_at)
               VALUES(?,'other',?,?,?) RETURNING id""",
            (new_uid("entity"), legal_name, actor, repository.now()),
        ).fetchone()[0]
        repository.connection.execute(
            "INSERT INTO revenue_authority_payers(state_code,revenue_entity_id) VALUES(?,?)", (state, entity_id),
        )
        return {"uid":state, "entity_id":entity_id}

    return repository.command(
        idempotency_key=f"authority-payer:{state}", command_type="authority_commission_payer_registered",
        aggregate_type="authority_payer", payload={"state":state,"legal_name":legal_name},
        actor_user_id=actor, operation=operation,
    )


def authority_payer(connection, state):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='revenue_authority_payers'"
    ).fetchone():
        return None
    return connection.execute(
        """SELECT e.id,e.legal_name FROM revenue_authority_payers p
             JOIN revenue_entities e ON e.id=p.revenue_entity_id
            WHERE p.state_code=? AND e.status='active' AND e.currency_code='AUD'""", (state,),
    ).fetchone()


class AuthorityInvoice(ToorakSettlement):
    def __init__(self, workflow, draft, *, rehearsal, production=False, payer_mail=False):
        simulation = workflow.pilot.simulation
        if (not (rehearsal if simulation else production) or draft.get("simulation") is not simulation
                or draft["target"] != list(workflow.target) or workflow.target[0] != "authority"
                or workflow.invoice(draft["uid"]) != draft):
            raise PermissionError("请选择隔离环境中已确认的教育局发票草稿。")
        if (workflow.terms() or {}).get("payment_mode") == vic_rcti.PAYMENT:
            raise PermissionError(vic_rcti.BLOCK_INVOICE)
        self.payer_mail = payer_mail
        self.draft = draft
        self.contract = workflow
        self.state = workflow.target[1]
        payer = workflow.db.execute(
            """SELECT e.id,e.legal_name FROM revenue_authority_payers p
                JOIN revenue_entities e ON e.id=p.revenue_entity_id
                WHERE p.state_code=? AND e.status='active' AND e.currency_code='AUD'""", (self.state,),
        ).fetchone()
        if not payer:
            raise ValueError("请先配置该州的正式付款主体。")
        self.payer = payer["legal_name"]
        super().__init__(RevenueService(workflow.repo), workflow.actor, "Admin",
                         formal=True, entity_id=payer["id"], simulation=simulation)
        self.workflow = "authority-commission"

    def issued_uid(self):
        row = self.connection.execute(
            """SELECT i.uid FROM revenue_authority_invoices a JOIN revenue_invoices i ON i.id=a.invoice_id
                WHERE a.draft_uid=? AND a.state_code=? AND i.revenue_entity_id=?
                  AND a.contract_draft_uid=? AND a.terms_revision=?""",
            (self.draft["uid"],self.state,self.entity_id,self.contract.uid,self.draft["terms_revision"]),
        ).fetchone()
        return row[0] if row else None

    def preview_token(self):
        revisions = current_invoice_template_revisions(self.connection, formal=True, entity_id=self.entity_id,
                                                       simulation=self.simulation)
        return sha256(canonical_json([self.draft, self.entity_id, self.payer, revisions]).encode()).hexdigest()

    def _render(self, number, issue_date):
        draft = self.draft
        totals = {key:draft[key] for key in ("net_cents","gst_cents","gross_cents")}
        lines = [{**line,"description":" | ".join((line["student_name"],line["student_id"],
                                                   line["school_name"],line["academic_period"]))}
                 for line in draft["lines"]]
        snapshot = {"bill_to":self.payer,"document_number":number,"issue_date":issue_date,"due_date":None,
                    "test_only":self.simulation,"payment_terms":f'Within {draft["payment_days"]} days of valid invoice receipt',
                    **totals,"lines":lines}
        rendered = render_with_active_template(self.connection, snapshot, formal=True, simulation=self.simulation,
                                               entity_id=self.entity_id)
        return totals, lines, rendered

    def preview_pdf(self):
        return self._render("DRAFT",_today())[2][0]

    def issue(self, token):
        draft = self.draft
        payload = {"draft_uid":draft["uid"],"preview":token,"state":self.state}

        def operation():
            if token != self.preview_token():
                raise ValueError("草稿或正式财务资料已更新，请重新核对。")
            terms = self.contract.terms()
            if (terms or {}).get("payment_mode") == vic_rcti.PAYMENT:
                raise PermissionError(vic_rcti.BLOCK_INVOICE)
            if not terms or terms["revision"] != draft["terms_revision"] or draft["payer"] != self.payer:
                raise ValueError("合同版本或法定付款主体不一致。")
            current = {row["uid"]:row for row in self.contract.candidates(terms, excluding_draft=draft["uid"])
                       if row.get("uid")}
            if not draft["lines"] or any(current.get(line["uid"]) != line for line in draft["lines"]):
                raise ValueError("学生归属、凭证或佣金金额已变更，请重新复核。")
            issue_date = _today()
            uid = new_uid("invoice")
            number = self.service._next_number("school_commission_invoice","COM",int(issue_date[:4]))
            while self.connection.execute("SELECT 1 FROM revenue_invoices WHERE invoice_number=?", (number,)).fetchone():
                number = self.service._next_number("school_commission_invoice","COM",int(issue_date[:4]))
            totals, lines, (pdf, template_uid, profile_uid) = self._render(number, issue_date)
            store = configured_invoice_store()
            if store is None:
                raise StorageUnavailable()
            document = self.service.register_document(
                FORMAL_INVOICE_KIND,str(store.write(pdf)),sha256(pdf).hexdigest(),"application/pdf",len(pdf),
                "generated",self.actor,f"authority-invoice-pdf:{draft['uid']}",
            )
            now = self.repository.now()
            invoice_id = self.connection.execute(
                """INSERT INTO revenue_invoices(uid,document_kind,invoice_number,revenue_entity_id,issue_date,
                      currency_code,net_cents,gst_cents,gross_cents,status,document_id,issued_by,issued_at,created_at)
                   VALUES(?,'direct_commission_invoice',?,?,?,'AUD',?,?,?,'issued',?,?,?,?) RETURNING id""",
                (uid,number,self.entity_id,issue_date,*totals.values(),document["id"],self.actor,now,now),
            ).fetchone()[0]
            self.connection.executemany(
                """INSERT INTO revenue_invoice_lines(uid,invoice_id,source_kind,source_uid,student_id,
                       description,net_cents,gst_cents,gross_cents,sort_order)
                   VALUES(?,?,'authority_draft_line',?,?,?,?,?,?,?)""",
                [(new_uid("invoice-line"),invoice_id,line["uid"],line["student_id"],line["description"],
                  line["net_cents"],line["gst_cents"],line["gross_cents"],index) for index,line in enumerate(lines)],
            )
            self.connection.execute(
                """INSERT INTO revenue_authority_invoices(draft_uid,invoice_id,state_code,contract_draft_uid,
                          terms_revision,payment_days) VALUES(?,?,?,?,?,?)""",
                (draft["uid"],invoice_id,self.state,self.contract.uid,draft["terms_revision"],draft["payment_days"]),
            )
            for field,account in (("net_cents","CommissionRevenue"),("gst_cents","GSTPayable")):
                if totals[field]:
                    self.service.append_balanced_ledger_event(
                        "invoiced","school_commission_invoice",uid,totals[field],"CommissionReceivable",account,
                        self.actor,commercial_case_id=None,
                    )
            if self.payer_mail:
                invoice = self.repository.require_by_uid("revenue_invoices", uid)
                enqueue_mail(self.repository, invoice, document, self.entity_id, self.actor,
                             payer_routing=True, simulation=self.simulation,
                             authority_draft=[self.contract.uid, draft["uid"]])
            return {"uid":uid,"state":self.state,"draft_uid":draft["uid"],"simulation":self.simulation,"number":number,
                    "document_uid":document["uid"],"sha256":document["sha256"],"template_uid":template_uid,
                    "profile_uid":profile_uid,"terms_revision":draft["terms_revision"],**totals}

        return self.repository.command(
            idempotency_key=f"authority-issue:{draft['uid']}", command_type=ISSUED, aggregate_type="invoice",
            payload=payload, actor_user_id=self.actor, operation=operation,
        )

    def _invoice(self, case_uid, invoice_uid):
        if case_uid is not None or self.issued_uid() != invoice_uid:
            raise PermissionError("请选择本州教育局已签发的发票。")
        row = self.connection.execute(
            """SELECT i.*, a.payment_days, d.uid AS document_uid, NULL AS case_uid
                FROM revenue_invoices i JOIN revenue_authority_invoices a ON a.invoice_id=i.id
                JOIN revenue_documents d ON d.id=i.document_id
                WHERE i.uid=? AND i.status='issued' AND i.document_kind='direct_commission_invoice'
                  AND i.currency_code='AUD' AND d.document_kind=?""", (invoice_uid,FORMAL_INVOICE_KIND),
        ).fetchone()
        if not row:
            raise PermissionError("找不到本州教育局已签发的发票。")
        lines = [dict(line) for line in self.connection.execute(
            """SELECT id AS invoice_line_id, uid AS invoice_line_uid, source_uid, student_id,
                      description, net_cents, gst_cents, gross_cents
                FROM revenue_invoice_lines WHERE invoice_id=? AND source_kind='authority_draft_line'
                ORDER BY sort_order,id""", (row["id"],),
        )]
        expected = self.draft["lines"]
        if (len(lines) != len(expected) or not all(
                line["source_uid"] == original["uid"] and all(line[key] == original[key]
                    for key in ("student_id","net_cents","gst_cents","gross_cents"))
                for line,original in zip(lines,expected))
                or any(sum(line[key] for line in lines) != row[key] for key in ("net_cents","gst_cents","gross_cents"))):
            raise ValueError("正式发票明细与签发快照不一致。")
        return {**dict(row),"lines":lines}

    def _valid_payment_days(self, term_days):
        return type(term_days) is int and 0 <= term_days <= 365

    def _payment_command_key(self, invoice, token):
        return f"authority-bank:{token}"

    def _reconciliation_lines_valid(self, lines):
        return bool(lines)

    def _mark_reconciled_lines(self, lines):
        # Authority invoice lines are reserved by the signed draft, with no legacy Claim state to advance.
        pass

    def record_payment(self, invoice_uid, state, amount, received_on, reference, staged, allocations=None):
        if state != self.state:
            raise ValueError("银行流水付款方与发票所属教育局不一致。")
        return self.payment(None,invoice_uid,amount,received_on,reference,staged,allocations)

    def queue_mock_mail(self, invoice_uid):
        invoice = self._invoice(None,invoice_uid)
        if self.payer_mail or not self.simulation:
            if mail_status(self.connection, invoice_uid):
                return
            raise ValueError("签发时未生成发送任务。")
        settings = mail_settings(self.connection,self.entity_id)
        if (not settings["enabled"] or not settings["verified"] or settings.get("transport") != "mock"
                or not settings["recipient"].lower().endswith(".test")):
            raise ValueError("请先核对隔离模拟收票邮箱。")
        document = self.repository.require_by_uid("revenue_documents",invoice["document_uid"])
        enqueue_mail(self.repository,invoice,document,self.entity_id,self.actor)

    def retry_mock_mail(self, invoice_uid, attempts):
        self._invoice(None,invoice_uid)
        event = mail_status(self.connection,invoice_uid)
        if not event or event.get("transport") != ("mock" if self.simulation else "production"):
            raise ValueError("找不到该发票的发送记录。")
        return retry_mail(self.repository,invoice_uid,self.entity_id,self.actor,
                          f"authority:{invoice_uid}:{attempts}",attempts)

    def dispatch_mock_mail(self, invoice_uid, mailbox_root):
        self._invoice(None,invoice_uid)
        if not self.simulation:
            raise PermissionError("正式发票只由受管发信进程投递。")
        data = Path("/Volumes/Data")
        stage = Path(__file__).resolve().parents[1] / "STAGING_DATA"
        if mailbox_root is not None and not Path(mailbox_root).resolve().is_relative_to(stage):
            raise PermissionError("模拟邮件目录须位于隔离环境。")
        mount = data if mailbox_root is not None and stage.is_relative_to(data) else None
        return dispatch_next(self.repository,mailbox_root,lambda event: (
            event["uid"] == invoice_uid and event["entity_id"] == self.entity_id
            and event.get("transport") == "mock" and event["recipient"].lower().endswith(".test")
        ),mock_writable_mount=mount)

    def pdf(self, invoice_uid):
        invoice = self._invoice(None,invoice_uid)
        document = self.repository.require_by_uid("revenue_documents",invoice["document_uid"])
        store = configured_invoice_store()
        if store is None:
            raise StorageUnavailable()
        return store.read(Path(document["storage_path"]),document["sha256"],document["size_bytes"])

    def view(self):
        uid = self.issued_uid()
        return {"detail": self.detail(uid) if uid else None, "preview_token":self.preview_token(),
                "mail":mail_status(self.connection,uid) if uid else None, "simulation":self.simulation,
                "payer_mail":self.payer_mail}
