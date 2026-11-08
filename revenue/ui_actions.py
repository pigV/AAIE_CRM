"""Application-layer handlers for Revenue UI mutations.

The class validates HTTP-form-shaped input and delegates every financial effect
to ``RevenueService``.  No fee, GST, commission, share, or ledger calculation
lives here.
"""

from __future__ import annotations

from revenue.access_policy import can_operate_revenue

from datetime import date
from pathlib import Path
import sqlite3

from revenue.ai import (
    ContractSuggestion,
    ManualContractSuggestionProvider,
    UnavailableContractSuggestionProvider,
)
from revenue.client_payments import ClientPaymentService
from revenue.repository import RevenueRepository
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.services import RevenueService
from revenue.school_contracts import SchoolCommissionContracts
from revenue.toorak_forecast import ToorakForecast
from revenue.toorak_claims import EVIDENCE_FIELDS, ToorakClaims
from revenue.toorak_settlement import ToorakSettlement
from revenue.uploads import (
    discard_unregistered_upload,
    parse_statement_csv,
    stage_revenue_upload,
)


class RevenueUIActions:
    MANUAL_CONTRACT_FIELDS = frozenset({
        "commission_rate_basis_points",
        "gst_basis_points",
        "fixed_commission_cents",
        "minimum_commission_cents",
        "maximum_commission_cents",
        "tuition_cents",
    })

    def __init__(
        self,
        connection: sqlite3.Connection,
        document_root: Path,
        actor_user_id: int,
        actor_role: str,
        *,
        actor_has_revenue_access: bool = False,
        legacy_document_root: Path | None = None,
        school_pilot=None,
        school_mail_release=None,
    ):
        if not can_operate_revenue(actor_role, actor_has_revenue_access):
            raise PermissionError("Revenue module access is required")
        self.connection = connection
        self.school_pilot = school_pilot
        self.school_mail_release = school_mail_release
        self._legacy_document_root = legacy_document_root
        self._document_root = Path(document_root) if document_root is not None else None
        self.actor_user_id = int(actor_user_id)
        self.actor_role = actor_role
        self.actor_has_revenue_access = bool(actor_has_revenue_access)
        self.repository = RevenueRepository(connection)
        self.service = RevenueService(self.repository)
        self.client_payments = ClientPaymentService(self.repository)

    @property
    def document_root(self):
        from revenue.secure_storage import configured_root, configured_write_root
        configuration = None if self._document_root is None else {'REVENUE_SECURE_STORAGE_ROOT': str(self._document_root)}
        legacy = getattr(self, '_legacy_document_root', None)
        if legacy is not None:
            return configured_write_root(legacy, configuration)
        return configured_root(configuration)

    def upload_contract(self, form: dict, upload) -> dict:
        key = self._required(form, "idempotency_key")
        staged = stage_revenue_upload(
            self.document_root / "contracts", upload, {".pdf", ".docx"}
        )
        try:
            with self.repository.transaction():
                document = self.service.register_document(
                    "commission_contract", str(staged.path), staged.sha256,
                    staged.mime_type, staged.size_bytes, "upload", self.actor_user_id,
                    f"{key}:document",
                )
                contract = self.service.create_contract_version(
                    self._required(form, "entity_uid"), document["uid"],
                    self._required(form, "contract_kind"),
                    self._required(form, "effective_from"), self.actor_user_id,
                    self.actor_role, f"{key}:contract",
                    reference_code=self._optional(form, "reference_code"),
                    actor_has_revenue_access=self.actor_has_revenue_access,
                )
                parse_run = self.service.create_parse_run(
                    contract["uid"], UnavailableContractSuggestionProvider(),
                    self.actor_user_id, self.actor_role, f"{key}:parse",
                    actor_has_revenue_access=self.actor_has_revenue_access,
                )
        except Exception:
            discard_unregistered_upload(staged)
            raise
        return {"document": document, "contract": contract, "parse_run": parse_run}

    def bind_school_commission_entity(self, form: dict) -> dict:
        self._require_admin()
        return self._school_contracts().bind(
            self._positive_integer(form, "school_id"), self._required(form, "legal_name"),
            self._required(form, "effective_from"), self._required(form, "idempotency_key"),
        )

    def upload_school_commission_contract(self, form: dict, upload) -> dict:
        self._require_admin()
        school_id = self._positive_integer(form, "school_id")
        start = date.fromisoformat(self._required(form, "effective_from"))
        end = date.fromisoformat(self._required(form, "effective_to"))
        if end < start:
            raise ValueError("到期日不得早于生效日。")
        reference = self._optional(form, "reference_code")
        key = self._required(form, "idempotency_key")
        staged = stage_revenue_upload(self.document_root / "school-contracts", upload, {".pdf"})
        return self._school_contracts().upload(
            school_id, start.isoformat(), end.isoformat(), reference, staged, key,
        )

    def _school_rule_inputs(self, form: dict) -> dict:
        return dict(
            sample_tuition=self._required(form, "sample_tuition"),
            rate_percent=self._required(form, "rate_percent"),
            gst_mode=self._required(form, "gst_mode"),
            commencement_days=self._non_negative_integer(form, "commencement_days"),
            required_coe=self._optional(form, "required_coe") == "1",
            clause_reference=self._required(form, "clause_reference"),
            evidence_text=self._required(form, "evidence_text"),
        )

    def preview_school_commission_rule(self, form: dict) -> dict:
        self._require_admin()
        preview = self._school_contracts().preview(
            self._positive_integer(form, "school_id"), self._required(form, "contract_version_uid"),
            **self._school_rule_inputs(form),
        )
        fields = (
            "school_id", "contract_version_uid", "sample_tuition", "rate_percent", "gst_mode",
            "commencement_days", "required_coe", "clause_reference", "evidence_text",
        )
        return {**preview, "confirmation_fields": {field: self._value(form, field) for field in fields}}

    def activate_school_commission_rule(self, form: dict) -> dict:
        self._require_admin()
        return self._school_contracts().activate(
            self._positive_integer(form, "school_id"), self._required(form, "contract_version_uid"),
            self._school_rule_inputs(form), self._required(form, "preview_digest"),
            self._required(form, "idempotency_key"),
        )

    def upload_toorak_draft(self, form: dict, upload) -> dict:
        self._require_admin()
        school_id = self._positive_integer(form, "school_id")
        key = self._required(form, "idempotency_key")
        staged = stage_revenue_upload(self.document_root / "school-contracts", upload, {".pdf"})
        return self._school_contracts().upload_toorak_draft(
            school_id,staged,key,signed_confirmed=self._optional(form,"signed_confirmed")=="1",
        )

    def upload_configurable_school_draft(self, form, upload):
        school_id = self._positive_integer(form,"school_id")
        key = self._required(form,"idempotency_key")
        staged = stage_revenue_upload(self.document_root/"school-contracts",upload,{".pdf"})
        try:
            return self._school_contracts().upload_configurable_draft(
                school_id,staged,key,signed_confirmed=self._optional(form,"signed_confirmed")=="1",
            )
        except Exception:
            discard_unregistered_upload(staged)
            raise

    def _configurable_school_inputs(self, form):
        names = ("school_id","draft_uid","effective_from","effective_to","sample_tuition","gst_mode",
                 "commencement_days","payment_days","registration_required","clause_reference",
                 "rate_percent","tuition_kind","visa_subclasses","rate_percent_2","tuition_kind_2","visa_subclasses_2")
        fields = {name:self._optional(form,name) or "" for name in names}
        fields["registration_required"] = fields["registration_required"] or "0"
        tiers = []
        for suffix in ("","_2"):
            if suffix and not fields["rate_percent"+suffix]:
                continue
            tiers.append({
                "rate_percent":fields["rate_percent"+suffix],
                "tuition_kind":fields["tuition_kind"+suffix] or "any",
                "visa_subclasses":[part.strip() for part in fields["visa_subclasses"+suffix].split(",") if part.strip()],
            })
        inputs = {key:fields[key] for key in (
            "effective_from","effective_to","sample_tuition","gst_mode","commencement_days",
            "payment_days","registration_required","clause_reference",
        )}
        return fields,{**inputs,"tiers":tiers}

    def preview_configurable_school_rule(self, form):
        fields,inputs = self._configurable_school_inputs(form)
        preview = self._school_contracts().preview_configurable(fields["draft_uid"],inputs)
        return {**preview,"confirmation_fields":fields}

    def activate_configurable_school_rule(self, form):
        fields,inputs = self._configurable_school_inputs(form)
        return self._school_contracts().activate_configurable(
            fields["draft_uid"],inputs,self._required(form,"preview_digest"),self._required(form,"idempotency_key"),
        )

    def _toorak_inputs(self, form: dict) -> dict:
        required = ("local_tuition", "international_tuition", "local_visa", "international_visa")
        optional = ("gst_mode", "effective_from", "effective_to")
        return {**{key: self._required(form, key) for key in required},
                **{key: self._optional(form, key) or "" for key in optional}}

    def preview_toorak_rule(self, form: dict) -> dict:
        draft_uid = self._required(form, "draft_uid")
        inputs = self._toorak_inputs(form)
        preview = self._school_contracts().preview_toorak(draft_uid, inputs)
        return {**preview, "confirmation_fields": {**inputs, "draft_uid": draft_uid}}

    def activate_toorak_rule(self, form: dict) -> dict:
        return self._school_contracts().activate_toorak(
            self._required(form, "draft_uid"), self._toorak_inputs(form),
            self._required(form, "preview_digest"), self._required(form, "idempotency_key"),
        )

    def preview_toorak_forecast(self, case_uid, form):
        return ToorakForecast(self.service,self.actor_user_id,self.actor_role,**self._school_scope()).preview(
            case_uid, self._required(form, "component_uid"), self._optional(form, "confirm_other") == "1",
        )

    def save_toorak_forecast(self, case_uid, form):
        return ToorakForecast(self.service,self.actor_user_id,self.actor_role,**self._school_scope()).save(
            case_uid, self._required(form, "component_uid"), self._optional(form, "confirm_other") == "1",
            self._required(form, "preview_digest"),
        )

    def _toorak_claim_inputs(self, form):
        if self.school_pilot:
            evidence = {key:value for key,_,_ in EVIDENCE_FIELDS if (value := self._optional(form,key))}
        else:
            evidence = {key:self._positive_integer(form,key) for key,_,_ in EVIDENCE_FIELDS}
        return (
            self._required(form,"line_uid"),
            evidence,
            self._required(form,"recruited"), self._optional(form,"reviewed") == "1",
        )

    def _toorak_claims(self):
        self._require_admin()
        return ToorakClaims(self.service,self.actor_user_id,self.actor_role,lambda: self.document_root,
                            mail_release=self.school_mail_release,**self._school_scope())

    def upload_toorak_claim_evidence(self, case_uid, form, upload):
        if not self.school_pilot:
            raise ValueError("仅正式试点支持上传申领凭证。")
        category = self._required(form,"evidence_kind")
        field = next((key for key,kind,_ in EVIDENCE_FIELDS if kind==category),None)
        if field is None:
            raise ValueError("请选择支持的申领凭证类型。")
        key = self._required(form,"idempotency_key")
        staged = stage_revenue_upload(self.document_root/"school-commission-claim-intake",upload,{".pdf"})
        try:
            return self._toorak_claims().register_evidence(case_uid,field,staged,key)
        except Exception:
            discard_unregistered_upload(staged)
            raise

    def preview_toorak_claim(self, case_uid, form):
        return self._toorak_claims().preview(case_uid,*self._toorak_claim_inputs(form))

    def issue_toorak_claim(self, case_uid, form):
        return self._toorak_claims().issue_claim(
            case_uid,*self._toorak_claim_inputs(form),self._required(form,"preview_digest"),
        )

    def issue_toorak_invoice(self, case_uid, form):
        revisions = None
        if {"template_revision","finance_revision"}.issubset(form):
            revisions = tuple(self._optional(form,key) or "" for key in ("template_revision","finance_revision"))
        return self._toorak_claims().issue_invoice(
            case_uid,self._required(form,"claim_uid"),self._optional(form,"template_confirmed") == "1",revisions,
        )

    def _toorak_settlement(self):
        pilot = self.school_pilot
        kwargs = {"formal":True,"entity_id":pilot.entity_id,"simulation":pilot.simulation} if pilot else {}
        return ToorakSettlement(self.service,self.actor_user_id,self.actor_role,**kwargs)

    def confirm_toorak_delivery(self, case_uid, form, upload):
        self._require_admin()
        invoice_uid = self._required(form,"invoice_uid")
        received_on = self._required(form,"received_on")
        term_days = self._optional(form,"term_days") or "30"
        if term_days not in {"0","7","15","30"}:
            raise ValueError("请选择立即到期、7 天、15 天或 30 天。")
        staged = stage_revenue_upload(self.document_root/"toorak-delivery-evidence",upload,{".pdf"})
        return self._toorak_settlement().delivery(case_uid,invoice_uid,received_on,staged,int(term_days))

    def record_toorak_payment(self, case_uid, form, upload):
        self._require_admin()
        invoice_uid = self._required(form,"invoice_uid")
        received_on = self._required(form,"received_on")
        reference = self._required(form,"bank_reference")
        amount = SchoolCommissionContracts._hundredths(self._required(form,"amount_aud"))
        allocations = {}
        for key in form:
            if key.startswith("allocation_"):
                value = self._optional(form,key)
                if value is not None:
                    cents = SchoolCommissionContracts._hundredths(value)
                    if cents:
                        allocations[key.removeprefix("allocation_")] = cents
        staged = stage_revenue_upload(self.document_root/"toorak-remittance-evidence",upload,{".pdf"})
        return self._toorak_settlement().payment(
            case_uid,invoice_uid,amount,received_on,reference,staged,allocations or None,
        )

    def confirm_toorak_reconciliation(self, case_uid, form):
        return self._toorak_settlement().reconcile(
            case_uid,self._required(form,"invoice_uid"),self._optional(form,"reviewed")=="1",
        )

    def _school_scope(self):
        pilot = self.school_pilot
        return {"formal":True,"school_id":pilot.school_id,"entity_id":pilot.entity_id,
                "simulation":pilot.simulation} if pilot else {}

    def _school_contracts(self):
        return SchoolCommissionContracts(self.service,self.actor_user_id,self.actor_role,**self._school_scope())

    def import_master_agent_statement(self, form: dict, upload) -> dict:
        key = self._required(form, "idempotency_key")
        master_uid = self._optional(form, "master_agent_entity_uid")
        provider_uid = self._optional(form, "provider_entity_uid")
        if not master_uid or not provider_uid:
            master_uid, provider_uid = self._pair(form, "entity_pair")
        items = parse_statement_csv(upload.data)
        staged = stage_revenue_upload(
            self.document_root / "statements", upload, {".csv"}, max_bytes=10 * 1024 * 1024
        )
        try:
            with self.repository.transaction():
                document = self.service.register_document(
                    "master_agent_statement", str(staged.path), staged.sha256,
                    staged.mime_type, staged.size_bytes, "imported", self.actor_user_id,
                    f"{key}:document",
                )
                statement = self.service.import_statement(
                    master_uid, provider_uid, document["uid"],
                    self._required(form, "statement_reference"), items,
                    self.actor_user_id, self.actor_role, f"{key}:statement",
                    period_start=self._optional(form, "period_start"),
                    period_end=self._optional(form, "period_end"),
                    actor_has_revenue_access=self.actor_has_revenue_access,
                )
        except Exception:
            discard_unregistered_upload(staged)
            raise
        return {"document": document, "statement": statement}

    def add_manual_contract_extraction(self, form: dict) -> dict:
        contract_uid = self._required(form, "contract_version_uid")
        self._reject_school_managed_contract(contract_uid)
        field_name = self._required(form, "field_name")
        if field_name not in self.MANUAL_CONTRACT_FIELDS:
            raise ValueError("不支持此合同字段。")
        value = self._required(form, "suggested_value")
        if not value.isdigit():
            raise ValueError("合同金额或比例必须使用非负整数。")
        provider = ManualContractSuggestionProvider([
            ContractSuggestion(
                field_name=field_name,
                suggested_value=value,
                page_number=None,
                clause_reference=self._optional(form, "clause_reference"),
                evidence_text=self._required(form, "evidence_text"),
                confidence_basis_points=10000,
            )
        ])
        return self.service.create_parse_run(
            contract_uid, provider,
            self.actor_user_id, self.actor_role,
            self._required(form, "idempotency_key"),
            actor_has_revenue_access=self.actor_has_revenue_access,
        )

    def review_contract_extraction(self, form: dict) -> dict:
        self._require_admin()
        decision = self._required(form, "decision")
        if decision not in {"accept", "reject"}:
            raise ValueError("审核决定无效。")
        confirmed = self._optional(form, "confirmed_value")
        extraction_uid = self._required(form,"extraction_uid")
        contract = self.connection.execute(
            """SELECT v.uid FROM revenue_contract_extractions e
               JOIN revenue_contract_parse_runs r ON r.id=e.parse_run_id
               JOIN revenue_contract_versions v ON v.id=r.contract_version_id WHERE e.uid=?""",
            (extraction_uid,),
        ).fetchone()
        if contract:
            self._reject_school_managed_contract(contract["uid"])
        return self.service.review_extraction(
            extraction_uid,
            confirmed if decision == "accept" else None,
            decision == "accept", self.actor_user_id, self.actor_role,
            self._required(form, "idempotency_key"),
        )

    def activate_contract(self, form: dict) -> dict:
        self._require_admin()
        contract_uid = self._required(form, "contract_version_uid")
        self._reject_school_managed_contract(contract_uid)
        return self.service.activate_contract_version(
            contract_uid,
            self.actor_user_id, self.actor_role,
            self._required(form, "idempotency_key"),
        )

    def _reject_school_managed_contract(self, contract_uid):
        if self.connection.execute(
            """SELECT 1 FROM revenue_contract_versions v
                 JOIN revenue_documents d ON d.id=v.document_id
                WHERE v.uid=? AND d.document_kind IN ('school_commission_contract','school_commission_signed_contract')""",
            (contract_uid,),
        ).fetchone():
            raise ValueError("请使用学校佣金合同 V1 的审核及激活流程。")

    def issue_direct_claim(self, form: dict) -> dict:
        line_uid = self._optional(form, "commission_line_uid")
        instalment_no = self._optional(form, "instalment_no")
        if not line_uid or not instalment_no:
            line_uid, instalment_no = self._pair(form, "claim_target")
        return self.service.issue_claim(
            line_uid,
            [{
                "instalment_no": self._positive_integer_value(instalment_no, "instalment_no"),
                "claimed_cents": self._positive_integer(form, "claimed_cents"),
            }],
            self._required(form, "claim_date"), self.actor_user_id,
            self.actor_role, self._required(form, "idempotency_key"),
            claim_kind="direct",
            actor_has_revenue_access=self.actor_has_revenue_access,
        )

    def record_client_payment(self, form: dict, upload=None) -> dict:
        key = self._required(form, "idempotency_key")
        staged = None
        document = None
        if upload is not None:
            staged = stage_revenue_upload(
                self.document_root / "client-payments",
                upload,
                {".png", ".jpg", ".jpeg", ".pdf"},
                max_bytes=20 * 1024 * 1024,
            )
        try:
            if staged:
                with self.repository.transaction():
                    document = self.service.register_document(
                        "client_payment_evidence", str(staged.path), staged.sha256,
                        staged.mime_type, staged.size_bytes, "upload",
                        self.actor_user_id, f"{key}:evidence",
                    )
                    result = self.client_payments.record_client_payment(
                        fee_item_uid=self._required(form, "fee_item_uid"),
                        payment_method_code=self._required(form, "payment_method_code"),
                        actual_currency_code=self._required(form, "actual_currency_code"),
                        actual_amount_cents=self._positive_integer(form, "actual_amount_cents"),
                        allocated_base_cents=self._positive_integer(form, "allocated_base_cents"),
                        received_date=self._required(form, "received_date"),
                        external_reference=self._optional(form, "external_reference"),
                        evidence_document_uid=document["uid"],
                        actor_user_id=self.actor_user_id,
                        actor_role=self.actor_role,
                        actor_has_revenue_access=self.actor_has_revenue_access,
                        idempotency_key=key,
                    )
            else:
                result = self.client_payments.record_client_payment(
                    fee_item_uid=self._required(form, "fee_item_uid"),
                    payment_method_code=self._required(form, "payment_method_code"),
                    actual_currency_code=self._required(form, "actual_currency_code"),
                    actual_amount_cents=self._positive_integer(form, "actual_amount_cents"),
                    allocated_base_cents=self._positive_integer(form, "allocated_base_cents"),
                    received_date=self._required(form, "received_date"),
                    external_reference=self._optional(form, "external_reference"),
                    evidence_document_uid=None,
                    actor_user_id=self.actor_user_id,
                    actor_role=self.actor_role,
                    actor_has_revenue_access=self.actor_has_revenue_access,
                    idempotency_key=key,
                )
        except Exception:
            if staged:
                discard_unregistered_upload(staged)
            raise
        return {**result, "evidence_document": document}

    def issue_client_receipt(
        self, form: dict, *, issuer_name: str, issuer_abn: str
    ) -> dict:
        return self.client_payments.issue_client_receipt(
            payment_uid=self._required(form, "payment_uid"),
            document_root=self.document_root / "receipts",
            actor_user_id=self.actor_user_id,
            actor_role=self.actor_role,
            actor_has_revenue_access=self.actor_has_revenue_access,
            idempotency_key=self._required(form, "idempotency_key"),
            issuer_name=issuer_name,
            issuer_abn=issuer_abn,
        )

    def issue_client_invoice(self, form: dict) -> dict:
        fee_item_uids = [
            value.strip()
            for value in self._required(form, "fee_item_uids").split(",")
            if value.strip()
        ]
        if not fee_item_uids or len(fee_item_uids) != len(set(fee_item_uids)):
            raise ValueError("fee_item_uids must contain unique fee items")
        payment_method_code = self._optional(form, "payment_method_code")
        planned = (
            [{"method_code": payment_method_code}]
            if payment_method_code else None
        )
        return self.service.issue_invoice(
            fee_item_uids, self.actor_user_id,
            self._required(form, "idempotency_key"),
            self._required(form, "issue_date"),
            self._optional(form, "due_date"),
            planned_payment_allocations=planned,
        )

    def record_structured_tuition(self, form: dict) -> dict:
        component_uid = self._required(form, "component_uid")
        authority_level = self._required(form, "authority_level")
        if authority_level not in {"official", "manual", "estimated"}:
            raise ValueError("invalid tuition authority")
        orchestrator = RevenueOperationalOrchestrator(
            self.connection, self.actor_user_id, self.actor_role,
            actor_has_revenue_access=self.actor_has_revenue_access,
        )
        result = orchestrator.tuition_confirmed(
            component_uid,
            self._required(form, "academic_period"),
            self._required(form, "fee_component"),
            self._non_negative_integer(form, "tuition_cents"),
            authority_level,
            self._required(form, "source_reference"),
            self._optional_integer(form, "evidence_document_id"),
        )
        return {**result, "commercial_case_uid": self._case_uid(component_uid)}

    def confirm_actual_commencement(self, form: dict) -> dict:
        component_uid = self._required(form, "component_uid")
        orchestrator = RevenueOperationalOrchestrator(
            self.connection, self.actor_user_id, self.actor_role,
            actor_has_revenue_access=self.actor_has_revenue_access,
        )
        result = orchestrator.actual_commencement_confirmed(
            component_uid,
            self._required(form, "actual_commencement_date"),
            self._optional_integer(form, "evidence_document_id"),
        )
        return {**result, "commercial_case_uid": self._case_uid(component_uid)}

    def _case_uid(self, component_uid: str) -> str:
        row = self.connection.execute(
            """SELECT c.uid FROM revenue_enrolment_components e
                 JOIN revenue_commercial_cases c ON c.id=e.commercial_case_id
                WHERE e.uid=?""",
            (component_uid,),
        ).fetchone()
        if not row:
            raise LookupError("Revenue Commercial Case not found")
        return str(row[0])

    def issue_direct_commission_invoice(self, form: dict) -> dict:
        return self.service.issue_direct_commission_invoice(
            self._required(form, "claim_uid"), self.actor_user_id, self.actor_role,
            self._required(form, "idempotency_key"),
            self._required(form, "issue_date"),
            self._optional(form, "due_date"),
            actor_has_revenue_access=self.actor_has_revenue_access,
        )

    def record_direct_commission_payment(self, form: dict) -> dict:
        line_uid, entity_uid = self._pair(form, "payment_target")
        return self.service.reconcile_direct_commission_payment(
            line_uid, self._positive_integer(form, "amount_cents"), entity_uid,
            self._required(form, "received_date"), self.actor_user_id,
            self.actor_role, self._required(form, "idempotency_key"),
            actor_has_revenue_access=self.actor_has_revenue_access,
        )

    def confirm_master_agent_match(self, form: dict) -> dict:
        return self.service.confirm_statement_match(
            self._required(form, "suggestion_uid"), self.actor_user_id,
            self.actor_role, self._required(form, "idempotency_key"),
            actor_has_revenue_access=self.actor_has_revenue_access,
        )

    def issue_master_agent_invoice(self, form: dict) -> dict:
        return self.service.issue_master_agent_invoice(
            self._required(form, "statement_item_uid"), self.actor_user_id,
            self.actor_role, self._required(form, "idempotency_key"),
            self._required(form, "issue_date"), self._optional(form, "due_date"),
            actor_has_revenue_access=self.actor_has_revenue_access,
        )

    def record_master_agent_payment(self, form: dict) -> dict:
        item_uid, master_uid = self._pair(form, "payment_target")
        return self.service.reconcile_statement_payment(
            item_uid, self._positive_integer(form, "amount_cents"), master_uid,
            self._required(form, "received_date"), self.actor_user_id,
            self.actor_role, self._required(form, "idempotency_key"),
            actor_has_revenue_access=self.actor_has_revenue_access,
        )

    def _require_admin(self):
        if self.actor_role != "Admin":
            raise PermissionError("exact Admin role is required")

    @staticmethod
    def _value(form: dict, key: str):
        value = form.get(key, "")
        if isinstance(value, (list, tuple)):
            value = value[-1] if value else ""
        return value

    @classmethod
    def _required(cls, form: dict, key: str) -> str:
        value = str(cls._value(form, key) or "").strip()
        if not value:
            raise ValueError(f"{key} is required")
        return value

    @classmethod
    def _optional(cls, form: dict, key: str) -> str | None:
        value = str(cls._value(form, key) or "").strip()
        return value or None

    @classmethod
    def _positive_integer(cls, form: dict, key: str) -> int:
        raw = cls._required(form, key)
        return cls._positive_integer_value(raw, key)

    @classmethod
    def _non_negative_integer(cls, form: dict, key: str) -> int:
        raw = cls._required(form, key)
        if not raw.isdigit():
            raise ValueError(f"{key} must be non-negative integer cents")
        return int(raw)

    @classmethod
    def _optional_integer(cls, form: dict, key: str) -> int | None:
        raw = cls._optional(form, key)
        if raw is None:
            return None
        if not raw.isdigit() or int(raw) <= 0:
            raise ValueError(f"{key} must be a positive integer")
        return int(raw)

    @staticmethod
    def _positive_integer_value(raw: str, key: str) -> int:
        if not raw.isdigit() or int(raw) <= 0:
            raise ValueError(f"{key} must be positive integer cents")
        return int(raw)

    @classmethod
    def _pair(cls, form: dict, key: str) -> tuple[str, str]:
        value = cls._required(form, key)
        first, separator, second = value.partition("|")
        if not separator or not first.strip() or not second.strip():
            raise ValueError(f"{key} is invalid")
        return first.strip(), second.strip()
