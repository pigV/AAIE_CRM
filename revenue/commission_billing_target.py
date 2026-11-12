"""Resolve confirmed commission addressees for the shared billing editor."""

from revenue import vic_rcti
from revenue.authority_formal import authority_payer, register_authority_payer
from revenue.institution_commission_draft import NSW_CLAIMS_ONLY, InstitutionCommissionDraft
from revenue.invoice_billing_details import bill_to_fields, billing_details, save_billing_details
from revenue.invoice_templates import (
    CommissionInvoiceTemplates, invoice_layout_selection, invoice_template_context,
    save_school_template_selection, school_template_state,
)
from revenue.services import RevenueService


class CommissionBillingTarget:
    def __init__(self, repository, intake, target, school, actor, *, scope=None, terms=None, mail_mode=None):
        self.repository, self.intake, self.target, self.actor = repository, intake, target, actor
        self.connection = repository.connection
        self.scope, self.terms, self.workflow = scope, terms, None
        self.template_context = invoice_template_context(intake, target)
        self.allow_auto_mail = False
        if target[0] == "authority":
            if not terms or terms["target"] != list(target):
                raise LookupError
            self.name = terms["payer"]
            self.allow_auto_mail = terms.get("payment_mode") not in {NSW_CLAIMS_ONLY, vic_rcti.PAYMENT}
        elif scope and scope.school_id == target[1] and school["active"]:
            self.name = school["name"]
            self.allow_auto_mail = True
        else:
            self.scope = None
            uid = next((uid for _, _, uid in school["confirmed"] if uid), None)
            if not uid:
                if not scope or scope.school_id != target[1] or not scope.entity_id:
                    raise LookupError
                self.scope = scope
                self.name = school["name"]
                return
            self.workflow = InstitutionCommissionDraft(repository, intake, uid, actor)
            if self.workflow.target != target or not self.workflow.terms():
                raise LookupError
            self.name = self.workflow.terms()["payer"]
            self.allow_auto_mail = mail_mode in {"mock", "production"} and intake.stage in {"invoicing", "settlement"}

    def payer_id(self, *, create=False, key=""):
        if self.workflow:
            payer = self.workflow._billing_payer(create=create, key=key)
            return payer["id"] if payer else None
        if self.scope:
            return self.scope.entity_id
        payer = authority_payer(self.connection, self.target[1])
        if payer:
            if payer["legal_name"] != self.name:
                raise ValueError("合同与已登记的付款主体不一致，请核对。")
            return payer["id"]
        if create:
            return register_authority_payer(self.repository, self.target[1], self.name, self.actor,
                                            rehearsal=self.intake.simulation, confirmed_terms=self.terms)["entity_id"]
        return None

    def settings(self):
        payer = self.payer_id()
        if payer:
            return billing_details(self.connection, payer, self.intake.simulation)
        return dict(name=self.name, address="", phone="", recipient="", revision="",
                    settings_revision="", enabled=False, verified=False)

    def save(self, form, key):
        if not key:
            raise ValueError("请刷新页面后保存开票资料。")
        with self.repository.transaction():
            payer = self.payer_id(create=True, key=key)
            if not self.allow_auto_mail:
                current = billing_details(self.connection, payer, self.intake.simulation)
                if form.get("enabled") == "1" and not current["enabled"]:
                    raise ValueError("当前阶段未开放自动发信。")
                form = {**form, "enabled": "1" if current["enabled"] else "0"}
            return save_billing_details(self.repository, payer, self.actor, key, form,
                                        simulation=self.intake.simulation)

    def template_settings(self):
        return school_template_state(self.connection, self.target, self.template_context["library_entity_id"],
                                     self.intake.simulation)

    def save_template(self, form, key):
        return save_school_template_selection(
            self.repository, self.target, self.template_context["library_entity_id"], self.intake.simulation,
            form.get("template_uid"), form.get("template_revision"), self.actor, key,
        )

    def preview(self):
        payer = self.payer_id()
        fields = bill_to_fields(self.connection, payer, self.intake.simulation) if payer else {}
        entity = payer or self.template_context["library_entity_id"]
        if not entity:
            raise ValueError("请先保存学校开票资料，再预览样张。")
        uid, source = invoice_layout_selection(self.connection, formal=True, entity_id=entity,
                                               simulation=self.intake.simulation, **self.template_context)
        if not uid and not fields:
            raise ValueError("请先保存学校开票资料或选择模板，再预览样张。")
        templates = CommissionInvoiceTemplates(RevenueService(self.repository), self.actor, "Admin",
                                                formal=True, simulation=self.intake.simulation, entity_id=entity)
        return templates.preview_sample(uid, authority_name=self.name, billing_sample=fields,
                                        layout_source=source, sample_finance=not bool(payer))
