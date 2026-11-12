"""Issue and settle invoices from a confirmed school or higher-education contract."""

from urllib.parse import quote

from revenue.authority_formal import DraftInvoice
from revenue.toorak_settlement import _today
from revenue.institution_commission_draft import INVOICE_ISSUED
from revenue.invoice_templates import FORMAL_INVOICE_KIND, financial_profile
from revenue.services import RevenueService


ISSUED = INVOICE_ISSUED


class InstitutionInvoice(DraftInvoice):
    issue_action = ISSUED
    command_prefix = "institution"
    line_kind = "institution_draft_line"
    invoice_date_terms = True

    def __init__(self, workflow, draft, *, rehearsal, production=False, payer_mail=False):
        simulation = workflow.pilot.simulation
        if (not (rehearsal if simulation else production)
                or workflow.target[0] not in {"school", "provider"}
                or workflow.invoice(draft["uid"]) != draft):
            raise PermissionError("请选择该学校已确认合同下的发票草稿。")
        payer = workflow._billing_payer()
        if not payer:
            raise ValueError("请先保存学校开票资料。")
        self.payer_mail = payer_mail
        self.draft = draft
        self.contract = workflow
        self.payer = payer["legal_name"]
        super().__init__(RevenueService(workflow.repo), workflow.actor, "Admin",
                         formal=True, entity_id=payer["id"], simulation=simulation)
        self.workflow = "institution-commission"

    @property
    def template_entity_id(self):
        _, profile = financial_profile(
            self.connection, formal=True, entity_id=self.entity_id, simulation=self.simulation,
        )
        if profile:
            return self.entity_id
        # Use the organisation's approved issuer layout and bank details. The
        # addressee always comes from the draft's payer.
        return self.contract.pilot.entity_id or self.entity_id

    def issue_scope(self):
        return {"target": list(self.contract.target), "entity_id": self.entity_id,
                "contract_draft_uid": self.contract.uid}

    def link_invoice(self, invoice_id):
        # The immutable issuance event links this draft and its confirmed terms.
        pass

    def issued_uid(self):
        row = self.connection.execute(
            """SELECT i.uid FROM revenue_audit_events a
                 JOIN revenue_invoices i ON i.uid=a.aggregate_uid
                WHERE a.action=? AND i.revenue_entity_id=?
                  AND json_extract(a.after_json,'$.draft_uid')=?
                  AND json_extract(a.after_json,'$.contract_draft_uid')=?
                  AND json_extract(a.after_json,'$.terms_revision')=?
                  AND json_extract(a.after_json,'$.simulation')=?""",
            (ISSUED, self.entity_id, self.draft["uid"], self.contract.uid,
             self.draft["terms_revision"], int(self.simulation)),
        ).fetchone()
        return row[0] if row else None

    def invoice_row(self, invoice_uid):
        return self.connection.execute(
            """SELECT i.*, d.uid AS document_uid, NULL AS case_uid, ? AS payment_days
                 FROM revenue_invoices i JOIN revenue_documents d ON d.id=i.document_id
                WHERE i.uid=? AND i.revenue_entity_id=? AND i.status='issued'
                  AND i.document_kind='direct_commission_invoice'
                  AND i.currency_code='AUD' AND d.document_kind=?""",
            (self.draft["payment_days"], invoice_uid, self.entity_id, FORMAL_INVOICE_KIND),
        ).fetchone()

    def _payment_command_key(self, invoice, token):
        return f"institution-bank:{self.entity_id}:{token}"

    def record_payment(self, invoice_uid, payer_id, amount, received_on, reference, staged, allocations=None):
        if str(payer_id) != str(self.entity_id):
            raise ValueError("银行流水付款方与发票所属学校不一致。")
        return self.payment(None, invoice_uid, amount, received_on, reference, staged, allocations)

    def proof_pdf(self, invoice_uid, document_uid):
        summary = self.detail(invoice_uid)["settlement"]
        documents = {row["document_uid"] for row in summary["payments"]}
        if summary["delivery"]:
            documents.add(summary["delivery"]["document_uid"])
        if not document_uid or document_uid not in documents:
            raise LookupError("找不到该发票的凭证。")
        return self._document_pdf(document_uid)

    def view(self):
        from revenue.invoice_billing_details import billing_details
        from revenue.school_invoice_reminders import draft_reminder

        view = super().view()
        details = billing_details(self.connection, self.entity_id, self.simulation)
        view.update(school=True, recipient=details["recipient"], cc=details["cc"], bcc=details["bcc"],
                    verified=details["verified"], auto_mail=details["enabled"] and self.payer_mail)
        if invoice := view["detail"]:
            invoice["legal_name"] = self.payer
            if invoice["settlement"]["overdue_days"] and details["verified"]:
                recipient = details["recipient"].split(",")[0].strip()
                view["reminder"] = draft_reminder(invoice, recipient)
        return view


def overdue_institution_invoices(repository, pilot, actor):
    """List issued, visible invoices with a remaining balance."""
    from revenue.institution_commission_draft import InstitutionCommissionDraft
    from revenue.institution_commission_draft_views import ROOT

    rows = repository.connection.execute(
        """SELECT json_extract(a.after_json,'$.contract_draft_uid') AS contract_uid,
                  json_extract(a.after_json,'$.draft_uid') AS draft_uid
             FROM revenue_invoices i JOIN revenue_audit_events a ON a.aggregate_uid=i.uid
            WHERE i.status='issued' AND i.issued_by=? AND i.due_date<?
              AND a.action=? AND json_extract(a.after_json,'$.simulation')=?""",
        (actor, _today(), ISSUED, int(pilot.simulation)),
    )
    for row in rows:
        try:
            workflow = InstitutionCommissionDraft(repository, pilot, row["contract_uid"], actor)
            draft = workflow.invoice(row["draft_uid"])
            if not draft:
                continue
            invoice = InstitutionInvoice(workflow, draft, rehearsal=pilot.simulation, production=not pilot.simulation)
            detail = invoice.detail(invoice.issued_uid())
        except (LookupError, PermissionError, ValueError):
            continue
        if detail["settlement"]["overdue_days"]:
            yield {**detail, "legal_name": invoice.payer,
                   "href": f'{ROOT}/{quote(workflow.uid,safe="")}?invoice={quote(draft["uid"],safe="")}'}
