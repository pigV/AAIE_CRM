"""Authoritative client Fee Item payment allocation for Revenue V1."""

from __future__ import annotations

from revenue.access_policy import can_operate_revenue

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from revenue.money import apply_basis_points
from revenue.pdf import render_receipt_pdf
from revenue.repository import RevenueRepository, canonical_json, new_uid


CLIENT_PAYMENT_METHODS = {
    "cash": ("AUD", "现金"),
    "alipay": ("CNY", "支付宝"),
    "wechat": ("CNY", "微信"),
    "au_personal_account": ("AUD", "澳洲私人账号"),
    "au_company_account": ("AUD", "澳洲公司账号"),
}


class ClientPaymentService:
    """Keeps actual money, AUD base settlement, and GST as separate authorities."""

    def __init__(self, repository: RevenueRepository):
        self.repository = repository
        self.connection = repository.connection

    @staticmethod
    def _require_operator(actor_role: str, actor_has_revenue_access: bool) -> None:
        if not can_operate_revenue(actor_role, actor_has_revenue_access):
            raise PermissionError("Revenue module access is required")

    @staticmethod
    def _positive_integer(value: int, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must be positive integer cents")
        return value

    @contextmanager
    def _immediate_transaction(self):
        """Serialize the read-check-write allocation boundary in SQLite."""

        if self.connection.in_transaction:
            with self.repository.transaction():
                yield
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _fee_context(self, fee_item_uid: str) -> sqlite3.Row:
        row = self.connection.execute(
            """SELECT f.*,c.application_cycle_id,c.student_id AS case_student_id,
                      s.canonical_json AS rule_snapshot_json
                 FROM revenue_fee_items f
                 JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id
                 JOIN revenue_rule_snapshots s ON s.id=f.rule_snapshot_id
                WHERE f.uid=?""",
            (str(fee_item_uid),),
        ).fetchone()
        if not row:
            raise ValueError("Revenue Fee Item not found")
        if row["currency_code"] != "AUD":
            raise ValueError("client Fee Item final base must be AUD")
        if row["status"] == "cancelled":
            raise ValueError("cancelled Fee Item cannot receive Payment")
        if row["student_id"] != row["case_student_id"]:
            raise ValueError("Revenue Fee Item Student authority mismatch")
        return row

    @staticmethod
    def _snapshot_contract_identity(fee: sqlite3.Row) -> tuple[int, int]:
        try:
            snapshot = json.loads(str(fee["rule_snapshot_json"] or ""))
        except (TypeError, ValueError) as exc:
            raise ValueError("immutable Fee Item rule snapshot is invalid") from exc
        items = snapshot.get("items") if isinstance(snapshot, dict) else None
        if not isinstance(items, list) or not items:
            raise ValueError("immutable Fee Item rule snapshot Contract is missing")
        identities = {
            (int(item["contract_id"]), int(item["application_cycle_id"]))
            for item in items
            if isinstance(item, dict)
            and item.get("contract_id") is not None
            and item.get("application_cycle_id") is not None
        }
        if len(identities) != 1:
            raise ValueError("immutable Fee Item rule snapshot Contract is missing or ambiguous")
        return next(iter(identities))

    def _ensure_fee_item_contract_link(self, fee: sqlite3.Row, actor_user_id: int) -> dict:
        contract_id, snapshot_cycle_id = self._snapshot_contract_identity(fee)
        if snapshot_cycle_id != int(fee["application_cycle_id"]):
            raise ValueError("immutable Fee Item rule snapshot Contract/Application Cycle mismatch")
        contract = self.connection.execute(
            """SELECT id,student_id,application_cycle_id,contract_status
                 FROM contracts WHERE id=?""",
            (contract_id,),
        ).fetchone()
        normalized_status = (
            str(contract["contract_status"] or "").replace("_", "").replace(" ", "").lower()
            if contract else ""
        )
        if (
            not contract
            or contract["student_id"] != fee["student_id"]
            or int(contract["application_cycle_id"] or 0) != int(fee["application_cycle_id"])
            or normalized_status != "fullyexecuted"
        ):
            raise ValueError("immutable Fee Item rule snapshot Contract does not match authoritative Student/Cycle")
        prior = self.connection.execute(
            "SELECT * FROM revenue_fee_item_contract_links WHERE fee_item_id=?",
            (fee["id"],),
        ).fetchone()
        if prior:
            if (
                int(prior["contract_id"]) != contract_id
                or int(prior["commercial_case_id"]) != int(fee["commercial_case_id"])
            ):
                raise ValueError("Revenue Fee Item already has a conflicting Contract link")
            return dict(prior)
        uid = new_uid("fee-contract-link")
        self.connection.execute(
            """INSERT INTO revenue_fee_item_contract_links(
                 uid,fee_item_id,contract_id,commercial_case_id,created_by,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                uid,
                fee["id"],
                contract_id,
                fee["commercial_case_id"],
                int(actor_user_id),
                self.repository.now(),
            ),
        )
        return self.repository.require_by_uid("revenue_fee_item_contract_links", uid)

    def _payment_authority(self, fee, actor_user_id):
        """School fees require a signed contract; independent Visa fees an exact case."""
        from revenue.visa_fee_policy import VISA_FEE_KINDS, visa_subclass
        snapshot = json.loads(str(fee["rule_snapshot_json"] or "{}"))
        items = snapshot.get("items", []) if isinstance(snapshot, dict) else []
        if any(isinstance(item, dict) and item.get("contract_id") is not None for item in items):
            return self._ensure_fee_item_contract_link(fee, actor_user_id)
        if len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("收费依据缺失，请核对合同或签证案件。")
        item = items[0]
        kind = VISA_FEE_KINDS.get(str(fee["milestone_code"]))
        if (not kind or not item.get("visa_case_id")
                or item.get("application_cycle_id") != fee["application_cycle_id"]
                or item.get("milestone_code") != fee["milestone_code"]
                or item.get("net_cents") != fee["net_cents"]
                or item.get("gst_cents", 0) != fee["gst_cents"]):
            # Preserve the established fail-closed contract diagnostic.
            return self._ensure_fee_item_contract_link(fee, actor_user_id)
        visa = self.connection.execute("SELECT * FROM visa_cases WHERE id=?", (item["visa_case_id"],)).fetchone()
        expected = {"guardian_590": "590", "visitor_600": "600", "student_visa": "500"}.get(kind)
        actual_subclass = visa_subclass(dict(visa)) if visa else None
        legacy_renewal = bool(visa and actual_subclass is None
            and fee["milestone_code"] == "student-visa-renewal"
            and str(dict(visa).get("case_type") or "").casefold() == "renewal")
        if (not visa or visa["student_id"] != fee["student_id"]
                or visa["application_cycle_id"] != fee["application_cycle_id"]
                or ("archived_at" in visa.keys() and visa["archived_at"])
                or (actual_subclass != expected and not legacy_renewal)):
            raise ValueError("签证收费与申请人或申请周期不一致，请核对后重试。")
        prior = self.connection.execute("SELECT * FROM revenue_fee_item_visa_links WHERE fee_item_id=?", (fee["id"],)).fetchone()
        if prior and (prior["visa_case_id"] != visa["id"] or prior["commercial_case_id"] != fee["commercial_case_id"]):
            raise ValueError("签证收费依据冲突，请先核对。")
        if not prior:
            self.connection.execute(
                "INSERT INTO revenue_fee_item_visa_links(uid,fee_item_id,visa_case_id,commercial_case_id,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (new_uid("fee-visa-link"), fee["id"], visa["id"], fee["commercial_case_id"], actor_user_id, self.repository.now()),
            )
        return None

    def ensure_fee_item_contract_link(
        self, fee_item_uid: str, *, actor_user_id: int, idempotency_key: str
    ) -> dict:
        payload = {"fee_item_uid": str(fee_item_uid)}

        def operation():
            return self._ensure_fee_item_contract_link(
                self._fee_context(fee_item_uid), int(actor_user_id)
            )

        return self.repository.command(
            idempotency_key=idempotency_key,
            command_type="client_fee_item_contract_linked",
            aggregate_type="fee_item_contract_link",
            payload=payload,
            actor_user_id=int(actor_user_id),
            operation=operation,
        )

    def _payment_method(self, method_code: str, effective_date: str) -> sqlite3.Row:
        code = str(method_code or "").strip()
        if code not in CLIENT_PAYMENT_METHODS:
            raise ValueError("unsupported client Payment method")
        rows = self.connection.execute(
            """SELECT m.id AS payment_method_id,m.method_code,m.display_name,
                      r.id AS rule_version_id,r.uid AS rule_version_uid,
                      r.gst_rate_basis_points
                 FROM revenue_payment_methods m
                 JOIN revenue_payment_method_rule_versions r ON r.payment_method_id=m.id
                WHERE m.method_code=? AND m.status='active' AND r.status='active'
                  AND r.effective_from<=?
                  AND (r.effective_to IS NULL OR r.effective_to>=?)
                ORDER BY r.effective_from DESC,r.id DESC""",
            (code, effective_date, effective_date),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("client Payment method rule is missing or ambiguous")
        if code == "cash" and rows[0]["gst_rate_basis_points"] != 0:
            raise ValueError("Cash GST must be zero")
        return rows[0]

    def _validate_actual_payment(
        self,
        method_code: str,
        actual_currency_code: str,
        actual_amount_cents: int,
        base_cents: int,
        gst_cents: int,
    ) -> str:
        currency = str(actual_currency_code or "").strip().upper()
        expected_currency, _ = CLIENT_PAYMENT_METHODS[method_code]
        if currency != expected_currency:
            raise ValueError(f"{method_code} actual currency must be {expected_currency}")
        if method_code in {"alipay", "wechat"}:
            if gst_cents != 0:
                raise ValueError("Alipay/WeChat GST must be zero")
            return currency
        expected_actual = base_cents + gst_cents
        if actual_amount_cents != expected_actual:
            raise ValueError("actual AUD amount must equal allocated base plus GST")
        return currency

    def fee_payment_summary(self, fee_item_uid: str) -> dict:
        fee = self._fee_context(fee_item_uid)
        totals = self.connection.execute(
            """SELECT COALESCE(SUM(allocated_base_cents),0) AS base_received,
                      COALESCE(SUM(gst_cents),0) AS gst_collected
                 FROM revenue_client_payment_allocations
                WHERE fee_item_id=?""",
            (fee["id"],),
        ).fetchone()
        discount_cents = int(self.connection.execute(
            """SELECT COALESCE(SUM(allocated_cents),0)
                 FROM revenue_discount_allocations WHERE fee_item_id=?""",
            (fee["id"],),
        ).fetchone()[0])
        final_base = int(fee["net_cents"])
        from revenue.receipt_display import legacy_fee_receipt_totals
        case_uid = self.connection.execute("SELECT uid FROM revenue_commercial_cases WHERE id=?", (fee["commercial_case_id"],)).fetchone()[0]
        legacy_base, legacy_gst, unverified = legacy_fee_receipt_totals(self.connection, case_uid).get(fee["id"], (0, 0, 0))
        base_received = int(totals["base_received"]) + legacy_base
        outstanding = max(0, final_base - base_received)
        status = (
            "pending" if base_received == 0
            else "settled" if outstanding == 0
            else "partially_received"
        )
        return {
            "fee_item_uid": fee["uid"],
            "standard_base_cents": final_base + discount_cents,
            "discount_cents": discount_cents,
            "final_base_cents": final_base,
            "base_received_cents": base_received,
            "gst_collected_cents": None if unverified else int(totals["gst_collected"]) + legacy_gst,
            "outstanding_base_cents": None if unverified else outstanding,
            "status": "needs_review" if unverified else status,
        }

    def _append_ledger_pair(
        self,
        *,
        source_uid: str,
        source_kind: str,
        amount_cents: int,
        currency_code: str,
        commercial_case_id: int,
        actor_user_id: int,
        debit_account: str,
        credit_account: str,
    ) -> None:
        now = self.repository.now()
        for account, debit, credit in (
            (debit_account, amount_cents, 0),
            (credit_account, 0, amount_cents),
        ):
            self.connection.execute(
                """INSERT INTO revenue_ledger_entries(
                     uid,account_code,event_type,source_kind,source_uid,commercial_case_id,
                     debit_cents,credit_cents,currency_code,occurred_at,created_by
                   ) VALUES(?,?,'received',?,?,?,?,?,?,?,?)""",
                (
                    new_uid("ledger"), account, source_kind, source_uid,
                    commercial_case_id, debit, credit, currency_code, now,
                    int(actor_user_id),
                ),
            )

    def record_client_payment(
        self,
        *,
        fee_item_uid: str,
        payment_method_code: str,
        actual_currency_code: str,
        actual_amount_cents: int,
        allocated_base_cents: int,
        received_date: str,
        external_reference: str | None,
        evidence_document_uid: str | None,
        actor_user_id: int,
        actor_role: str,
        actor_has_revenue_access: bool,
        idempotency_key: str,
    ) -> dict:
        self._require_operator(actor_role, actor_has_revenue_access)
        actual = self._positive_integer(actual_amount_cents, "actual_amount_cents")
        base = self._positive_integer(allocated_base_cents, "allocated_base_cents")
        method_code = str(payment_method_code or "").strip()
        payload = {
            "fee_item_uid": str(fee_item_uid),
            "payment_method_code": method_code,
            "actual_currency_code": str(actual_currency_code or "").strip().upper(),
            "actual_amount_cents": actual,
            "allocated_base_cents": base,
            "received_date": str(received_date),
            "external_reference": str(external_reference or "").strip() or None,
            "evidence_document_uid": evidence_document_uid,
        }

        def operation():
            fee = self._fee_context(fee_item_uid)
            from revenue.receipt_display import legacy_fee_receipt_totals
            case_uid = self.connection.execute("SELECT uid FROM revenue_commercial_cases WHERE id=?", (fee["commercial_case_id"],)).fetchone()[0]
            legacy_base, _, unverified = legacy_fee_receipt_totals(self.connection, case_uid).get(fee["id"], (0, 0, 0))
            if unverified:
                raise ValueError("该收费项目已有历史收款，GST分摊尚未明确，请先核对历史记录。")
            prior_base = int(self.connection.execute(
                "SELECT COALESCE(SUM(allocated_base_cents),0) FROM revenue_client_payment_allocations WHERE fee_item_id=?",
                (fee["id"],)).fetchone()[0]) + legacy_base
            if legacy_base and prior_base + base > int(fee["net_cents"]):
                raise ValueError("该项目已有历史收款，本次收款超过未收基础费，不能重复收款。")
            link = self._payment_authority(fee, int(actor_user_id))
            method = self._payment_method(method_code, str(received_date))
            gst = apply_basis_points(base, int(method["gst_rate_basis_points"]))
            gross = base + gst
            currency = self._validate_actual_payment(
                method_code, actual_currency_code, actual, base, gst
            )
            if prior_base + base > int(fee["net_cents"]):
                raise ValueError("client Payment allocation exceeds Fee Item final base")
            document_id = None
            if evidence_document_uid:
                document = self.repository.require_by_uid(
                    "revenue_documents", str(evidence_document_uid)
                )
                if document["mime_type"] not in {
                    "application/pdf", "image/png", "image/jpeg"
                }:
                    raise ValueError("client Payment evidence type is not allowed")
                document_id = document["id"]
            now = self.repository.now()
            payment_uid = new_uid("payment")
            self.connection.execute(
                """INSERT INTO revenue_payments(
                     uid,payment_direction,counterparty_kind,billing_account_id,payment_method_id,
                     external_reference,received_date,amount_cents,allocated_cents,currency_code,
                     status,evidence_document_id,confirmed_by,confirmed_at
                   ) VALUES(?,'received','client',?,?,?,?,?,0,?,'confirmed',?,?,?)""",
                (
                    payment_uid, fee["billing_account_id"], method["payment_method_id"],
                    payload["external_reference"], received_date, actual, currency,
                    document_id, int(actor_user_id), now,
                ),
            )
            payment = self.repository.require_by_uid("revenue_payments", payment_uid)
            allocation_uid = new_uid("client-allocation")
            self.connection.execute(
                """INSERT INTO revenue_client_payment_allocations(
                     uid,payment_id,fee_item_id,allocated_base_cents,gst_cents,
                     gross_charge_aud_cents,payment_method_rule_version_id,
                     method_code_snapshot,gst_rate_basis_points,created_by,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    allocation_uid, payment["id"], fee["id"], base, gst, gross,
                    method["rule_version_id"], method_code,
                    method["gst_rate_basis_points"], int(actor_user_id), now,
                ),
            )
            allocation = self.repository.require_by_uid(
                "revenue_client_payment_allocations", allocation_uid
            )
            self._append_ledger_pair(
                source_uid=payment_uid,
                source_kind="client_payment_actual",
                amount_cents=actual,
                currency_code=currency,
                commercial_case_id=fee["commercial_case_id"],
                actor_user_id=actor_user_id,
                debit_account="Bank",
                credit_account="ClientFundsReceived",
            )
            self._append_ledger_pair(
                source_uid=payment_uid,
                source_kind="client_fee_base_settlement",
                amount_cents=base,
                currency_code="AUD",
                commercial_case_id=fee["commercial_case_id"],
                actor_user_id=actor_user_id,
                debit_account="ClientFundsAllocated",
                credit_account="ClientFeeReceivable",
            )
            summary = self.fee_payment_summary(fee_item_uid)
            return {
                "uid": payment_uid,
                "payment": payment,
                "allocation": allocation,
                "contract_link": link,
                "fee_status": summary["status"],
                "fee_summary": summary,
            }

        with self._immediate_transaction():
            return self.repository.command(
                idempotency_key=idempotency_key,
                command_type="client_payment_recorded",
                aggregate_type="payment",
                payload=payload,
                actor_user_id=int(actor_user_id),
                operation=operation,
            )

    def _next_receipt_number(self, received_date: str) -> str:
        year = int(str(received_date)[:4])
        row = self.connection.execute(
            """SELECT * FROM revenue_number_sequences
                WHERE document_kind='receipt' AND financial_year=?""",
            (year,),
        ).fetchone()
        if row:
            value = int(row["next_number"])
            self.connection.execute(
                "UPDATE revenue_number_sequences SET next_number=?,updated_at=? WHERE id=?",
                (value + 1, self.repository.now(), row["id"]),
            )
        else:
            value = 1
            self.connection.execute(
                """INSERT INTO revenue_number_sequences(
                     uid,document_kind,financial_year,next_number,updated_at
                   ) VALUES(?,'receipt',?,2,?)""",
                (new_uid("sequence"), year, self.repository.now()),
            )
        return f"RCT-{year}-{value:06d}"

    @staticmethod
    def _student_display_name(student: sqlite3.Row) -> str:
        keys = set(student.keys())
        parts = [
            str(student[name] or "").strip()
            for name in ("passport_first_name", "passport_last_name")
            if name in keys and str(student[name] or "").strip()
        ]
        return " ".join(parts) or "姓名待关联"

    def _receipt_authority(self, payment_uid: str) -> sqlite3.Row:
        visa_ready = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='revenue_fee_item_visa_links'"
        ).fetchone() is not None
        visa_join = "LEFT JOIN revenue_fee_item_visa_links vl ON vl.fee_item_id=f.id" if visa_ready else ""
        visa_id = "vl.visa_case_id" if visa_ready else "NULL"
        row = self.connection.execute(
            f"""SELECT p.*,a.id AS client_allocation_id,a.fee_item_id,
                      a.allocated_base_cents,a.gst_cents,a.gross_charge_aud_cents,
                      a.method_code_snapshot,f.uid AS fee_item_uid,f.description AS fee_type,
                      f.commercial_case_id,l.contract_id,{visa_id} AS visa_case_id,c.application_cycle_id,c.student_id,
                      m.display_name AS payment_method_authority_label
                 FROM revenue_payments p
                 JOIN revenue_client_payment_allocations a ON a.payment_id=p.id
                 JOIN revenue_fee_items f ON f.id=a.fee_item_id
                 LEFT JOIN revenue_fee_item_contract_links l ON l.fee_item_id=f.id
                 {visa_join}
                 JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id
                 JOIN revenue_payment_methods m ON m.id=p.payment_method_id
                WHERE p.uid=? AND p.payment_direction='received'
                  AND p.counterparty_kind='client' AND p.status='confirmed'
                  AND p.billing_account_id=f.billing_account_id""",
            (str(payment_uid),),
        ).fetchone()
        if not row:
            raise ValueError("client Payment allocation not found")
        if (row["contract_id"] is None) == (row["visa_case_id"] is None):
            raise ValueError("收据必须有唯一且明确的合同或签证案件依据。")
        return row

    @staticmethod
    def _write_immutable_pdf(root: Path, snapshot: dict) -> tuple[Path, str, int, bool]:
        from revenue.secure_storage import write_immutable
        payload = render_receipt_pdf(snapshot, None)
        digest = hashlib.sha256(payload).hexdigest()
        target, created = write_immutable(root, f"{digest}.pdf", payload)
        return target, digest, len(payload), created

    def issue_client_receipt(
        self,
        *,
        payment_uid: str,
        document_root: Path,
        actor_user_id: int,
        actor_role: str,
        actor_has_revenue_access: bool,
        idempotency_key: str,
        issuer_name: str,
        issuer_abn: str,
    ) -> dict:
        self._require_operator(actor_role, actor_has_revenue_access)
        payload = {
            "payment_uid": str(payment_uid),
            "issuer_name": str(issuer_name).strip(),
            "issuer_abn": str(issuer_abn).strip(),
        }
        if not payload["issuer_name"] or not payload["issuer_abn"]:
            raise ValueError("Receipt issuer identity is required")
        created_file: Path | None = None

        def operation():
            nonlocal created_file
            authority = self._receipt_authority(payment_uid)
            visa_receipt = authority["contract_id"] is None
            snapshot_table = "revenue_visa_receipt_snapshots" if visa_receipt else "revenue_receipt_snapshots"
            if visa_receipt and not self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (snapshot_table,),
            ).fetchone():
                raise ValueError("独立签证收据结构尚未升级，操作已停止。")
            prior = self.connection.execute(
                "SELECT * FROM revenue_receipts WHERE payment_id=?",
                (authority["id"],),
            ).fetchone()
            if prior:
                snapshot = self.connection.execute(
                    f"SELECT * FROM {snapshot_table} WHERE receipt_id=?",
                    (prior["id"],),
                ).fetchone()
                if not snapshot:
                    raise ValueError("existing Receipt has no immutable snapshot")
                document = self.connection.execute(
                    "SELECT * FROM revenue_documents WHERE id=?",
                    (prior["document_id"],),
                ).fetchone()
                return {
                    "uid": prior["uid"],
                    "receipt": dict(prior),
                    "snapshot": dict(snapshot),
                    "document": dict(document),
                }
            student = self.connection.execute(
                "SELECT * FROM students WHERE student_id=?",
                (authority["student_id"],),
            ).fetchone()
            cycle = self.connection.execute(
                "SELECT * FROM application_cycles WHERE id=?",
                (authority["application_cycle_id"],),
            ).fetchone()
            contract = self.connection.execute(
                "SELECT * FROM contracts WHERE id=?",
                (authority["contract_id"],),
            ).fetchone()
            visa = None
            if visa_receipt:
                self._payment_authority(self._fee_context(authority["fee_item_uid"]), actor_user_id)
                self._validate_actual_payment(
                    authority["method_code_snapshot"], authority["currency_code"],
                    int(authority["amount_cents"]), int(authority["allocated_base_cents"]),
                    int(authority["gst_cents"]),
                )
                visa = self.connection.execute("SELECT * FROM visa_cases WHERE id=?", (authority["visa_case_id"],)).fetchone()
                if not visa or not str(visa["applicant_name"] or "").strip():
                    raise ValueError("请先核实签证案件的实际申请人姓名。")
                from revenue.visa_fee_policy import visa_subclass
                if (visa_subclass(dict(visa)) in {"590", "600"}
                        and str(visa["applicant_name"]).strip().casefold()
                        == self._student_display_name(student).strip().casefold()):
                    raise ValueError("家长签证案件仍使用学生姓名，请先核实并更正实际家长申请人后再生成收据。")
            if not student or not cycle or (not visa_receipt and not contract):
                raise ValueError("Receipt authoritative Student/Cycle/Contract is missing")
            contract_reference = (
                str(contract["contract_uid"] or "").strip()
                if contract and "contract_uid" in contract.keys() else ""
            ) or (f"CONTRACT-{contract['id']}" if contract else None)
            cycle_label = (
                str(cycle["cycle_uid"] or "").strip()
                if "cycle_uid" in cycle.keys() else ""
            ) or f"CYCLE-{cycle['id']}"
            receipt_number = self._next_receipt_number(authority["received_date"])
            issued_at = self.repository.now()
            method_label = CLIENT_PAYMENT_METHODS.get(
                authority["method_code_snapshot"],
                (authority["currency_code"], authority["payment_method_authority_label"]),
            )[1]
            snapshot_value = {
                "issuer_name": payload["issuer_name"],
                "issuer_abn": payload["issuer_abn"],
                "receipt_number": receipt_number,
                "student_name": self._student_display_name(student),
                "student_number": authority["student_id"],
                "application_cycle_id": int(authority["application_cycle_id"]),
                "application_cycle_label": cycle_label,
                "fee_item_id": int(authority["fee_item_id"]),
                "fee_type": authority["fee_type"],
                "payment_date": authority["received_date"],
                "payment_method_code": authority["method_code_snapshot"],
                "payment_method_label": method_label,
                "actual_currency_code": authority["currency_code"],
                "actual_amount_cents": int(authority["amount_cents"]),
                "allocated_base_cents": int(authority["allocated_base_cents"]),
                "gst_cents": int(authority["gst_cents"]),
                "gross_charge_aud_cents": int(authority["gross_charge_aud_cents"]),
                "transaction_reference": authority["external_reference"],
                "issued_by": int(actor_user_id),
                "issued_at": issued_at,
                "created_at": issued_at,
            }
            if visa_receipt:
                from revenue.visa_fee_policy import visa_subclass, visa_fee_description
                snapshot_value.update(
                    visa_case_id=int(visa["id"]),
                    visa_case_reference=f"VISA-{visa['id']}",
                    visa_subclass=visa_subclass(dict(visa)) or str(dict(visa).get("visa_type") or dict(visa).get("case_type") or "类别待核实"),
                    applicant_name=str(visa["applicant_name"]).strip(),
                    fee_type=visa_fee_description(visa),
                )
            else:
                snapshot_value.update(contract_id=int(authority["contract_id"]), contract_reference=contract_reference)
            snapshot_sha256 = hashlib.sha256(
                canonical_json(snapshot_value).encode("utf-8")
            ).hexdigest()
            document_path, document_sha256, size_bytes, was_created = self._write_immutable_pdf(
                Path(document_root), snapshot_value
            )
            if was_created:
                created_file = document_path
            document_uid = new_uid("document")
            self.connection.execute(
                """INSERT INTO revenue_documents(
                     uid,document_kind,storage_path,sha256,mime_type,size_bytes,
                     source_kind,created_by,created_at
                   ) VALUES(?,'client_payment_receipt',?,?,'application/pdf',?,'generated',?,?)""",
                (
                    document_uid, str(document_path.resolve()), document_sha256,
                    size_bytes, int(actor_user_id), issued_at,
                ),
            )
            document = self.repository.require_by_uid("revenue_documents", document_uid)
            receipt_uid = new_uid("receipt")
            self.connection.execute(
                """INSERT INTO revenue_receipts(
                     uid,receipt_number,payment_id,amount_cents,currency_code,document_id,
                     issued_by,issued_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    receipt_uid, receipt_number, authority["id"],
                    authority["amount_cents"], authority["currency_code"],
                    document["id"], int(actor_user_id), issued_at,
                ),
            )
            receipt = self.repository.require_by_uid("revenue_receipts", receipt_uid)
            snapshot_uid = new_uid("receipt-snapshot")
            stored = {"uid": snapshot_uid, "receipt_id": receipt["id"],
                      **snapshot_value, "snapshot_sha256": snapshot_sha256}
            columns = ",".join(stored)
            placeholders = ",".join("?" for _ in stored)
            self.connection.execute(
                f"INSERT INTO {snapshot_table}({columns}) VALUES({placeholders})",
                tuple(stored.values()),
            )
            snapshot = self.repository.require_by_uid(snapshot_table, snapshot_uid)
            return {
                "uid": receipt_uid,
                "receipt": receipt,
                "snapshot": snapshot,
                "document": document,
            }

        try:
            with self._immediate_transaction():
                return self.repository.command(
                    idempotency_key=idempotency_key,
                    command_type="client_receipt_issued",
                    aggregate_type="receipt",
                    payload=payload,
                    actor_user_id=int(actor_user_id),
                    operation=operation,
                )
        except Exception:
            # Published secure objects may already be shared by another request.
            # Retain an inert object on rollback; never delete another receipt evidence.
            raise
