"""Application-layer handlers for Revenue UI mutations.

The class validates HTTP-form-shaped input and delegates every financial effect
to ``RevenueService``.  No fee, GST, commission, share, or ledger calculation
lives here.
"""

from __future__ import annotations

from revenue.access_policy import can_operate_revenue

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
    ):
        if not can_operate_revenue(actor_role, actor_has_revenue_access):
            raise PermissionError("Revenue module access is required")
        self.connection = connection
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
            self._required(form, "contract_version_uid"), provider,
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
        return self.service.review_extraction(
            self._required(form, "extraction_uid"),
            confirmed if decision == "accept" else None,
            decision == "accept", self.actor_user_id, self.actor_role,
            self._required(form, "idempotency_key"),
        )

    def activate_contract(self, form: dict) -> dict:
        self._require_admin()
        return self.service.activate_contract_version(
            self._required(form, "contract_version_uid"),
            self.actor_user_id, self.actor_role,
            self._required(form, "idempotency_key"),
        )

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
