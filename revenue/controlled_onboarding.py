"""Immutable Preview -> exact Apply for current-active Revenue onboarding."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
from typing import Any, Callable

from revenue.client_fee_rules import (
    ClientFeeRuleFacts,
    DatabaseClientFeeRuleResolver,
    MissingFeeRule,
)
from revenue.facts import RevenueFactAdapter
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.repository import RevenueRepository, canonical_json, new_uid
from revenue.services import RevenueService


class InvalidOnboardingProposal(ValueError):
    pass


class OnboardingProposalDrift(RuntimeError):
    pass


EXCEPTION_MESSAGES = {
    "missing_student_authority": "Application Cycle has no authoritative Student.",
    "ambiguous_engagement_contract": "Multiple FullyExecuted contracts have no authoritative supersession.",
    "missing_engagement_contract": "No FullyExecuted engagement contract is authoritative for client fee evaluation.",
    "missing_fee_rule": "No active DB-backed Fee Rule matches the authoritative facts.",
    "unresolved_fee_classification": "Client fee classification requires Admin review.",
    "missing_he_application": "Active Higher Education Cycle has no authoritative HE Application.",
    "missing_revenue_entity": "Accepted institution has no active Revenue Entity mapping.",
    "missing_institution_contract": "Accepted institution has no active Revenue Contract.",
    "missing_commission_rule": "Accepted institution has no active Commission Rule.",
    "missing_tuition": "No authoritative structured Tuition is available.",
    "payment_method_ambiguity": "Historical payment method is blank or cannot determine Australian personal/company GST authority.",
}


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _safe_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    return value


class ControlledOnboardingService:
    def __init__(
        self,
        repository: RevenueRepository,
        *,
        uid_factory: Callable[[str], str] = new_uid,
    ):
        self.repository = repository
        self.connection = repository.connection
        self.service = RevenueService(repository)
        self.fee_rules = DatabaseClientFeeRuleResolver(self.connection)
        self.uid_factory = uid_factory

    @staticmethod
    def _require_admin(actor_role: str):
        if actor_role != "Admin":
            raise PermissionError("Controlled Revenue onboarding requires exact Admin role")

    def _table_exists(self, table: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    def _columns(self, table: str) -> set[str]:
        if not self._table_exists(table):
            return set()
        return {row[1] for row in self.connection.execute(f'PRAGMA table_info("{table}")')}

    def _rows(self, table: str, where: str = "", params: tuple = ()) -> list[dict[str, Any]]:
        if not self._table_exists(table):
            return []
        rows = self.connection.execute(
            f'SELECT * FROM "{table}" {where}', params
        ).fetchall()
        return [
            {key: _safe_value(row[key]) for key in row.keys()}
            for row in rows
        ]

    def _schema_fingerprint(self) -> str:
        rows = self.connection.execute(
            """SELECT type,name,tbl_name,sql FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"""
        ).fetchall()
        return _digest([tuple(row) for row in rows])

    def _migration_registry(self) -> list[dict[str, Any]]:
        return self._rows("schema_migrations", "ORDER BY rowid")

    def _active_cycles(self):
        return self.connection.execute(
            """SELECT * FROM application_cycles
                WHERE lower(trim(status))='active' ORDER BY id"""
        ).fetchall()

    def _active_cycle_set_fingerprint(self) -> str:
        return _digest(
            [
                {
                    "id": row["id"],
                    "student_id": row["student_id"],
                    "cycle_uid": row["cycle_uid"],
                    "cycle_type": row["cycle_type"],
                    "education_stream": row["education_stream"],
                    "status": row["status"],
                    "version": self._cycle_version(row),
                }
                for row in self._active_cycles()
            ]
        )

    @staticmethod
    def _cycle_version(row) -> str:
        for name in ("updated_at", "created_at", "opened_at"):
            if name in row.keys() and row[name] not in (None, ""):
                return str(row[name])
        return str(row["id"])

    def _fee_authority_fingerprint(self) -> str:
        return _digest(
            {
                "sets": self._rows("revenue_fee_rule_sets", "ORDER BY id"),
                "versions": self._rows(
                    "revenue_fee_rule_versions", "WHERE status='active' ORDER BY id"
                ),
                "conditions": self._rows("revenue_fee_rule_conditions", "ORDER BY id"),
                "milestones": self._rows("revenue_fee_rule_milestones", "ORDER BY id"),
                "payment_methods": self._rows("revenue_payment_methods", "ORDER BY id"),
                "payment_rules": self._rows(
                    "revenue_payment_method_rule_versions", "WHERE status='active' ORDER BY id"
                ),
            }
        )

    def _commission_authority_fingerprint(self) -> str:
        return _digest({
            table: self._rows(table, "ORDER BY id")
            for table in (
                "revenue_entities",
                "revenue_entity_provider_links",
                "revenue_contract_groups",
                "revenue_contract_versions",
                "revenue_commission_rule_versions",
                "revenue_commission_rule_scopes",
                "revenue_commission_rate_instalments",
                "revenue_enrolment_components",
                "revenue_tuition_facts",
            )
        })

    def _cycle_authority(self, cycle_id: int) -> dict[str, Any]:
        cycle = self._rows("application_cycles", "WHERE id=?", (cycle_id,))
        if not cycle:
            raise OnboardingProposalDrift(f"Application Cycle {cycle_id} no longer exists")
        student_id = cycle[0].get("student_id")
        student = self._rows("students", "WHERE student_id=?", (student_id,)) if student_id else []
        sources: dict[str, Any] = {"cycle": cycle, "student": student}
        for table, column in (
            ("contracts", "application_cycle_id"),
            ("visa_cases", "application_cycle_id"),
            ("offer_received_events", "application_cycle_id"),
            ("cycle_final_acceptances", "application_cycle_id"),
            ("coe_oshc", "application_cycle_id"),
            ("student_school_enrolments", "application_cycle_id"),
            ("higher_education_applications", "application_cycle_id"),
            ("higher_education_offers", "application_cycle_id"),
            ("revenue_commercial_cases", "application_cycle_id"),
        ):
            if column in self._columns(table):
                sources[table] = self._rows(table, f"WHERE {column}=? ORDER BY id", (cycle_id,))
        case_rows = sources.get("revenue_commercial_cases", [])
        if case_rows:
            case_id = case_rows[0]["id"]
            sources["revenue_fee_items"] = self._rows(
                "revenue_fee_items", "WHERE commercial_case_id=? ORDER BY id", (case_id,)
            )
            sources["revenue_enrolment_components"] = self._rows(
                "revenue_enrolment_components", "WHERE commercial_case_id=? ORDER BY id", (case_id,)
            )
        if student_id:
            account_links = self._rows(
                "revenue_billing_account_students",
                "WHERE student_id=? ORDER BY id",
                (student_id,),
            )
            sources["revenue_billing_account_students"] = account_links
            account_ids = sorted({int(row["billing_account_id"]) for row in account_links})
            sources["revenue_billing_accounts"] = [
                dict(self.connection.execute(
                    "SELECT * FROM revenue_billing_accounts WHERE id=?", (account_id,)
                ).fetchone())
                for account_id in account_ids
            ]
        return sources

    def _cycle_authority_fingerprint(self, cycle_id: int) -> str:
        return _digest(self._cycle_authority(cycle_id))

    @staticmethod
    def _fully_executed(contracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for contract in contracts:
            status = str(contract.get("contract_status") or "")
            normalized = status.replace("_", "").replace(" ", "").lower()
            if normalized != "fullyexecuted":
                continue
            if contract.get("deleted_at") or contract.get("voided_at"):
                continue
            if contract.get("superseded_by_contract_id"):
                continue
            result.append(contract)
        return result

    @staticmethod
    def _he_category(applications: list[dict[str, Any]]) -> str | None:
        current = [row for row in applications if row.get("is_current", 1) not in (0, "0")]
        if len(current) != 1:
            return None
        course_type = str(current[0].get("course_type") or "").strip().lower()
        if not course_type:
            return None
        if any(token in course_type for token in ("language", "elicos", "english")):
            return "language_school"
        return "university_foundation"

    def _existing_aaie(self, cycle_id: int) -> tuple[bool, dict[str, Any]]:
        decision = RevenueFactAdapter(self.connection).resolve_existing_aaie_k12(cycle_id)
        return bool(decision.is_existing), decision.evidence()

    def _fee_proposal(
        self,
        cycle,
        authority: dict[str, Any],
        existing_aaie: bool,
    ) -> tuple[dict[str, Any], list[str]]:
        cycle_id = int(cycle["id"])
        if cycle["education_stream"] == "school_education" and cycle["cycle_type"] == "progression":
            return {
                "status": "blocked",
                "reason": "unresolved_fee_classification",
                "rule_version_uid": None,
                "net_cents": None,
            }, ["unresolved_fee_classification"]
        he_category = None
        if cycle["education_stream"] == "higher_education":
            he_category = self._he_category(authority.get("higher_education_applications", []))
            if he_category is None:
                return {
                    "status": "blocked",
                    "reason": "missing_he_application",
                    "rule_version_uid": None,
                    "net_cents": None,
                }, ["missing_he_application"]
        contracts = self._fully_executed(authority.get("contracts", []))
        if len(contracts) > 1:
            return {
                "status": "blocked",
                "reason": "ambiguous_engagement_contract",
                "contract_ids": [row["id"] for row in contracts],
                "rule_version_uid": None,
                "net_cents": None,
            }, ["ambiguous_engagement_contract"]
        if not contracts:
            return {
                "status": "blocked",
                "reason": "missing_engagement_contract",
                "rule_version_uid": None,
                "net_cents": None,
            }, ["missing_engagement_contract"]
        contract = contracts[0]
        engagement_date = str(
            contract.get("contract_date")
            or contract.get("fully_executed_at")
            or self._cycle_version(cycle)
        )[:10]
        facts = ClientFeeRuleFacts(
            service_kind="base",
            education_stream=cycle["education_stream"],
            cycle_type=cycle["cycle_type"],
            existing_aaie_k12=existing_aaie,
            he_category=he_category,
            onshore=cycle["cycle_type"] in {"transfer", "progression"},
        )
        try:
            resolved = self.fee_rules.resolve(facts, engagement_date)
        except MissingFeeRule:
            return {
                "status": "blocked",
                "reason": "missing_fee_rule",
                "contract_id": contract["id"],
                "rule_version_uid": None,
                "net_cents": None,
            }, ["missing_fee_rule"]
        existing_case = authority.get("revenue_commercial_cases", [])
        existing_fees = authority.get("revenue_fee_items", [])
        if existing_case and existing_fees:
            return {
                "status": "existing",
                "reason": "existing_revenue_fee_item",
                "contract_id": contract["id"],
                "rule_version_uid": resolved.rule_uid,
                "net_cents": None,
            }, []
        historical_payment_method = (
            str(contract.get("service_fee_currency") or "").strip()
            if "service_fee_currency" in contract else None
        )
        payment_ambiguous = historical_payment_method in {"", "国内银行"}
        return {
            "status": "proposed",
            "reason": "controlled_onboarding",
            "source": "CONTROLLED_ONBOARDING",
            "contract_id": contract["id"],
            "engagement_source": "fully_executed_contract",
            "source_engagement_date": engagement_date,
            "rule_version_uid": resolved.rule_uid,
            "policy_code": resolved.policy_code,
            "milestone_code": resolved.milestone_code,
            "net_cents": resolved.net_cents,
            "gst_cents": 0,
            "payment_method_status": (
                "needs_review" if payment_ambiguous else "pending_invoice_selection"
            ),
            "historical_payment_method": historical_payment_method,
            "rule_snapshot": resolved.snapshot(),
            "facts": asdict(facts),
        }, (["payment_method_ambiguity"] if payment_ambiguous else [])

    def _visa_fee_proposals(
        self,
        cycle,
        authority: dict[str, Any],
        existing_aaie: bool,
        base_fee: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        visa_rows = authority.get("visa_cases", [])
        if not visa_rows or base_fee["status"] not in {"proposed", "existing"}:
            return [], []
        contracts = self._fully_executed(authority.get("contracts", []))
        if len(contracts) != 1:
            return [], []
        contract = contracts[0]
        engagement_date = str(
            contract.get("contract_date")
            or contract.get("fully_executed_at")
            or self._cycle_version(cycle)
        )[:10]
        proposals = []
        exception_codes = []
        for visa in visa_rows:
            normalized = " ".join(
                str(visa.get(name) or "").strip().lower()
                for name in (
                    "case_type", "visa_case_name", "visa_subclass", "visa_type",
                    "application_type",
                )
            )
            is_590 = "590" in normalized
            renewal = any(token in normalized for token in ("renewal", "renew", "extension"))
            independent = renewal or "standalone" in normalized or "independent" in normalized
            facts = ClientFeeRuleFacts(
                service_kind="guardian_590" if is_590 else "student_visa",
                education_stream=cycle["education_stream"],
                cycle_type=cycle["cycle_type"],
                existing_aaie_k12=existing_aaie,
                application_type="renewal" if renewal else "new",
                independent_service=independent,
            )
            try:
                resolved = self.fee_rules.resolve(facts, engagement_date)
            except MissingFeeRule:
                proposals.append(
                    {
                        "status": "blocked",
                        "reason": "missing_fee_rule",
                        "visa_case_id": visa["id"],
                        "rule_version_uid": None,
                        "net_cents": None,
                    }
                )
                exception_codes.append("missing_fee_rule")
                continue
            status = "included" if resolved.net_cents is None else "proposed"
            proposals.append(
                {
                    "status": status,
                    "reason": "included_in_base_fee" if status == "included" else "controlled_onboarding",
                    "source": "CONTROLLED_ONBOARDING",
                    "visa_case_id": visa["id"],
                    "contract_id": contract["id"],
                    "engagement_source": "fully_executed_contract",
                    "source_engagement_date": engagement_date,
                    "rule_version_uid": resolved.rule_uid,
                    "policy_code": resolved.policy_code,
                    "milestone_code": resolved.milestone_code,
                    "net_cents": resolved.net_cents,
                    "gst_cents": 0,
                    "payment_method_status": "pending_invoice_selection",
                    "rule_snapshot": resolved.snapshot(),
                    "facts": asdict(facts),
                }
            )
        return proposals, exception_codes

    def _accepted_institution(
        self, authority: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        acceptances = authority.get("cycle_final_acceptances", [])
        active = [
            row for row in acceptances
            if str(row.get("acceptance_status") or "active").lower() == "active"
        ]
        if not active:
            return None, None
        acceptance = active[-1]
        school_id = acceptance.get("school_id")
        if school_id is None:
            offer_id = acceptance.get("offer_received_event_id")
            offers = authority.get("offer_received_events", [])
            offer = next((row for row in offers if row.get("id") == offer_id), None)
            school_id = offer.get("school_id") if offer else None
        if school_id is not None:
            return acceptance, {"kind": "school", "school_id": school_id}
        offer_id = acceptance.get("offer_received_event_id")
        offer = next(
            (row for row in authority.get("offer_received_events", []) if row.get("id") == offer_id),
            None,
        )
        higher_offer_id = offer.get("higher_education_offer_id") if offer else None
        higher_offer = next(
            (row for row in authority.get("higher_education_offers", []) if row.get("id") == higher_offer_id),
            None,
        )
        application_id = higher_offer.get("application_id") if higher_offer else None
        application = next(
            (row for row in authority.get("higher_education_applications", []) if row.get("id") == application_id),
            None,
        )
        if application and application.get("provider_id") is not None:
            return acceptance, {
                "kind": "higher_education",
                "provider_id": application["provider_id"],
                "higher_education_application_id": application["id"],
            }
        return acceptance, None

    def _commission_proposal(
        self, cycle, authority: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        acceptance, institution = self._accepted_institution(authority)
        if acceptance is None:
            return {"status": "not_applicable", "reason": "no_final_acceptance"}, []
        base = {
            "status": "needs_attention",
            "final_acceptance_id": acceptance["id"],
            "institution": institution,
            "amount_cents": None,
        }
        if institution is None:
            return {**base, "reason": "missing_revenue_entity"}, ["missing_revenue_entity"]
        link_column = (
            "school_id" if institution["kind"] == "school"
            else "higher_education_provider_id"
        )
        source_id = (
            institution["school_id"] if institution["kind"] == "school"
            else institution["provider_id"]
        )
        entity = self.connection.execute(
            """SELECT e.* FROM revenue_entities e
                JOIN revenue_entity_provider_links l ON l.revenue_entity_id=e.id
               WHERE """ + link_column + """=? AND l.effective_to IS NULL AND e.status='active'
               ORDER BY l.effective_from DESC,l.id DESC LIMIT 1""",
            (source_id,),
        ).fetchone()
        if not entity:
            return {**base, "reason": "missing_revenue_entity"}, ["missing_revenue_entity"]
        contract = self.connection.execute(
            """SELECT v.* FROM revenue_contract_versions v
                JOIN revenue_contract_groups g ON g.id=v.contract_group_id
               WHERE g.revenue_entity_id=? AND v.status='active'
               ORDER BY v.effective_from DESC,v.id DESC LIMIT 1""",
            (entity["id"],),
        ).fetchone()
        if not contract:
            return {
                **base,
                "revenue_entity_uid": entity["uid"],
                "reason": "missing_institution_contract",
            }, ["missing_institution_contract"]
        rule = self.connection.execute(
            """SELECT * FROM revenue_commission_rule_versions
                WHERE contract_version_id=? AND status='active'
                ORDER BY priority DESC,id DESC LIMIT 1""",
            (contract["id"],),
        ).fetchone()
        if not rule:
            return {
                **base,
                "revenue_entity_uid": entity["uid"],
                "contract_version_uid": contract["uid"],
                "reason": "missing_commission_rule",
            }, ["missing_commission_rule"]
        if cycle["cycle_type"] == "transfer":
            return {
                **base,
                "status": "explicit_no_commission",
                "revenue_entity_uid": entity["uid"],
                "contract_version_uid": contract["uid"],
                "commission_rule_uid": rule["uid"],
                "reason": "onshore_transfer",
                "amount_cents": 0,
            }, []
        return {
            **base,
            "revenue_entity_uid": entity["uid"],
            "contract_version_uid": contract["uid"],
            "commission_rule_uid": rule["uid"],
            "reason": "missing_tuition",
        }, ["missing_tuition"]

    def preview(
        self,
        *,
        actor_user_id: int,
        actor_role: str,
        source_snapshot_metadata: dict[str, Any],
        created_at: str | None = None,
    ) -> dict[str, Any]:
        self._require_admin(actor_role)
        created_at = created_at or self.repository.now()
        cycles = []
        active = self._active_cycles()
        for cycle in active:
            authority = self._cycle_authority(cycle["id"])
            fingerprint = _digest(authority)
            student_id = cycle["student_id"]
            student_exists = bool(authority["student"])
            exception_codes: list[str] = []
            if not student_id or not student_exists:
                case = {"status": "blocked", "reason": "missing_student_authority"}
                fee = {"status": "not_evaluated", "reason": "missing_student_authority"}
                commission = {"status": "not_evaluated", "reason": "missing_student_authority"}
                existing = False
                existing_evidence = {"reason": "missing_student_authority"}
                exception_codes.append("missing_student_authority")
            else:
                existing, existing_evidence = self._existing_aaie(cycle["id"])
                existing_case = authority.get("revenue_commercial_cases", [])
                case = {
                    "status": "existing" if existing_case else "proposed",
                    "reason": "already_onboarded" if existing_case else "active_cycle_with_student_authority",
                    "commercial_case_uid": existing_case[0]["uid"] if existing_case else None,
                }
                fee, fee_exceptions = self._fee_proposal(cycle, authority, existing)
                additional_fees, visa_fee_exceptions = self._visa_fee_proposals(
                    cycle, authority, existing, fee
                )
                commission, commission_exceptions = self._commission_proposal(cycle, authority)
                exception_codes.extend(fee_exceptions)
                exception_codes.extend(visa_fee_exceptions)
                exception_codes.extend(commission_exceptions)
            cycles.append(
                {
                    "application_cycle_id": int(cycle["id"]),
                    "cycle_uid": cycle["cycle_uid"],
                    "cycle_version": self._cycle_version(cycle),
                    "authoritative_facts_fingerprint": fingerprint,
                    "student_id": student_id,
                    "classification": {
                        "education_stream": cycle["education_stream"],
                        "cycle_type": cycle["cycle_type"],
                        "existing_aaie_k12": existing,
                        "existing_aaie_evidence": existing_evidence,
                    },
                    "case": case,
                    "fee": fee,
                    "additional_fees": additional_fees if student_exists else [],
                    "commission": commission,
                    "exceptions": [
                        {"code": code, "message": EXCEPTION_MESSAGES[code]}
                        for code in dict.fromkeys(exception_codes)
                    ],
                }
            )
        summary = {
            "active_cycles": len(cycles),
            "active_students": len({item["student_id"] for item in cycles if item["student_id"]}),
            "commercial_cases_to_create": sum(item["case"]["status"] == "proposed" for item in cycles),
            "blocked_commercial_cases": sum(item["case"]["status"] == "blocked" for item in cycles),
            "fee_items_to_create": sum(
                int(item["fee"]["status"] == "proposed")
                + sum(fee["status"] == "proposed" for fee in item.get("additional_fees", []))
                for item in cycles
            ),
            "blocked_fee_items": sum(
                int(item["fee"]["status"] == "blocked")
                + sum(fee["status"] == "blocked" for fee in item.get("additional_fees", []))
                for item in cycles
            ),
            "commission_contexts": sum(
                item["commission"]["status"] not in {"not_applicable", "not_evaluated"}
                for item in cycles
            ),
            "exceptions_to_create": sum(len(item["exceptions"]) for item in cycles),
            "invoices_to_create": 0,
            "payments_to_create": 0,
            "receipts_to_create": 0,
            "claims_to_create": 0,
        }
        body = {
            "proposal_uid": self.uid_factory("onboarding-proposal"),
            "proposal_version": "controlled-onboarding-v1",
            "created_at": created_at,
            "created_by": actor_user_id,
            "source": {
                **source_snapshot_metadata,
                "schema_fingerprint": self._schema_fingerprint(),
                "migration_registry": self._migration_registry(),
                "migration_registry_fingerprint": _digest(self._migration_registry()),
                "active_cycle_set_fingerprint": self._active_cycle_set_fingerprint(),
                "fee_authority_fingerprint": self._fee_authority_fingerprint(),
                "commission_authority_fingerprint": self._commission_authority_fingerprint(),
            },
            "cycles": cycles,
            "summary": summary,
        }
        return {**body, "proposal_sha256": _digest(body)}

    def _verify_proposal(self, proposal: dict[str, Any]):
        supplied_sha = proposal.get("proposal_sha256")
        body = {key: value for key, value in proposal.items() if key != "proposal_sha256"}
        if not supplied_sha or _digest(body) != supplied_sha:
            raise InvalidOnboardingProposal("proposal SHA-256 does not match its immutable content")
        source = proposal.get("source") or {}
        comparisons = {
            "schema_fingerprint": self._schema_fingerprint(),
            "migration_registry_fingerprint": _digest(self._migration_registry()),
            "active_cycle_set_fingerprint": self._active_cycle_set_fingerprint(),
            "fee_authority_fingerprint": self._fee_authority_fingerprint(),
            "commission_authority_fingerprint": self._commission_authority_fingerprint(),
        }
        drift = [name for name, current in comparisons.items() if source.get(name) != current]
        for cycle in proposal.get("cycles", []):
            current = self._cycle_authority_fingerprint(cycle["application_cycle_id"])
            if current != cycle.get("authoritative_facts_fingerprint"):
                drift.append(f"application_cycle:{cycle['application_cycle_id']}")
        if drift:
            raise OnboardingProposalDrift(
                "authoritative source drift requires a new Preview: " + ", ".join(drift)
            )

    def apply(
        self,
        proposal: dict[str, Any],
        *,
        actor_user_id: int,
        actor_role: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_admin(actor_role)
        supplied_sha = proposal.get("proposal_sha256")
        body = {key: value for key, value in proposal.items() if key != "proposal_sha256"}
        if not supplied_sha or _digest(body) != supplied_sha:
            raise InvalidOnboardingProposal("proposal SHA-256 does not match its immutable content")
        payload = {"proposal_uid": proposal["proposal_uid"], "proposal_sha256": supplied_sha}

        def operation():
            self._verify_proposal(proposal)
            cases: dict[int, dict[str, Any]] = {}
            case_count = fee_count = exception_count = context_count = 0
            orchestrator = RevenueOperationalOrchestrator(
                self.connection, actor_user_id, actor_role
            )
            for item in proposal["cycles"]:
                cycle_id = item["application_cycle_id"]
                if item["case"]["status"] == "proposed":
                    case = self.service.open_case(
                        cycle_id,
                        actor_user_id,
                        f"controlled-onboarding:{proposal['proposal_uid']}:case:{cycle_id}",
                    )
                    cases[cycle_id] = case
                    case_count += 1
                elif item["case"]["status"] == "existing":
                    cases[cycle_id] = self.repository.require_by_uid(
                        "revenue_commercial_cases", item["case"]["commercial_case_uid"]
                    )

                for exception in item["exceptions"]:
                    aggregate_type = "commercial_case" if cycle_id in cases else "application_cycle"
                    aggregate_uid = cases[cycle_id]["uid"] if cycle_id in cases else f"cycle:{cycle_id}"
                    before = self.connection.execute(
                        """SELECT 1 FROM revenue_exceptions
                            WHERE exception_code=? AND aggregate_type=? AND aggregate_uid=? AND status='open'""",
                        (exception["code"], aggregate_type, aggregate_uid),
                    ).fetchone()
                    orchestrator.open_exception(
                        exception["code"], aggregate_type, aggregate_uid, exception["message"]
                    )
                    if not before:
                        exception_count += 1

                fees = [item["fee"], *item.get("additional_fees", [])]
                for fee in fees:
                    if fee["status"] != "proposed":
                        continue
                    case = cases[cycle_id]
                    account = self.service.ensure_student_billing_account(
                        item["student_id"],
                        actor_user_id,
                        f"controlled-onboarding:{proposal['proposal_uid']}:billing:{item['student_id']}",
                        fee["source_engagement_date"],
                    )
                    fee_item = {
                        "milestone_code": fee["milestone_code"],
                        "description": "Controlled onboarding client service fee",
                        "net_cents": fee["net_cents"],
                        "gst_cents": fee["gst_cents"],
                        "source": fee["source"],
                        "proposal_uid": proposal["proposal_uid"],
                        "proposal_sha256": supplied_sha,
                        "application_cycle_id": cycle_id,
                        "contract_id": fee["contract_id"],
                        "visa_case_id": fee.get("visa_case_id"),
                        **fee["rule_snapshot"],
                    }
                    created = self.service.create_fee_items(
                        case["uid"],
                        account["uid"],
                        [fee_item],
                        actor_user_id,
                        "controlled-onboarding:"
                        f"{proposal['proposal_uid']}:fee:{cycle_id}:"
                        f"{fee.get('visa_case_id') or 'base'}",
                        fee["rule_version_uid"],
                    )
                    fee_count += len(created)

                commission = item["commission"]
                if (
                    commission["status"] not in {"not_applicable", "not_evaluated"}
                    and commission.get("institution") is not None
                ):
                    case = cases[cycle_id]
                    self.service.link_enrolment_component(
                        case["uid"],
                        commission["institution"]["kind"],
                        "final_acceptance",
                        commission["final_acceptance_id"],
                        actor_user_id,
                        f"controlled-onboarding:{proposal['proposal_uid']}:acceptance:{commission['final_acceptance_id']}",
                        final_acceptance_id=commission["final_acceptance_id"],
                        higher_education_application_id=(
                            (commission.get("institution") or {}).get(
                                "higher_education_application_id"
                            )
                        ),
                    )
                    context_count += 1

            return {
                "uid": proposal["proposal_uid"],
                "proposal_sha256": supplied_sha,
                "status": "applied",
                "commercial_cases_created": case_count,
                "fee_items_created": fee_count,
                "exceptions_created": exception_count,
                "commission_contexts_created": context_count,
                "invoices_created": 0,
                "payments_created": 0,
                "receipts_created": 0,
                "claims_created": 0,
            }

        def replay_result(original: dict[str, Any]) -> dict[str, Any]:
            result = dict(original)
            result["original_status"] = original.get("status")
            result["status"] = "replay_noop"
            for key in (
                "commercial_cases_created",
                "fee_items_created",
                "exceptions_created",
                "commission_contexts_created",
                "invoices_created",
                "payments_created",
                "receipts_created",
                "claims_created",
            ):
                result[key] = 0
            return result

        return self.repository.command(
            idempotency_key=idempotency_key,
            command_type="controlled_onboarding_proposal_applied",
            aggregate_type="controlled_onboarding_proposal",
            payload=payload,
            actor_user_id=actor_user_id,
            operation=operation,
            replay_action="controlled_onboarding_proposal_replayed",
            replay_result=replay_result,
        )
