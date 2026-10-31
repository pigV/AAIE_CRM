"""Single synchronous boundary from authoritative CRM events into Revenue."""

from __future__ import annotations

from revenue.access_policy import can_operate_revenue

from dataclasses import asdict

from revenue.client_fee_rules import (
    ClientFeeRuleFacts,
    DatabaseClientFeeRuleResolver,
    MissingFeeRule,
)
from revenue.client_payments import ClientPaymentService
from revenue.operational_facts import RevenueOperationalFacts
from revenue.repository import RevenueRepository, new_uid
from revenue.services import RevenueService


class RevenueOperationalOrchestrator:
    def __init__(
        self, connection, actor_user_id: int, actor_role: str,
        *, trusted_authoritative_event: bool = False,
        actor_has_revenue_access: bool = False,
    ):
        self.connection = connection
        self.actor_user_id = int(actor_user_id)
        self.actor_role = str(actor_role)
        self.trusted_authoritative_event = bool(trusted_authoritative_event)
        self.actor_has_revenue_access = bool(actor_has_revenue_access)
        self.repository = RevenueRepository(connection)
        self.service = RevenueService(self.repository)
        self.client_payments = ClientPaymentService(self.repository)
        self.facts = RevenueOperationalFacts(connection)
        self.client_fee_rules = DatabaseClientFeeRuleResolver(connection)

    def _require_admin(self):
        if self.actor_role != "Admin" and not self.trusted_authoritative_event:
            raise PermissionError("exact Admin role is required")

    def _require_operator(self):
        if (
            not can_operate_revenue(self.actor_role, self.actor_has_revenue_access)
            and not self.trusted_authoritative_event
        ):
            raise PermissionError("Revenue module access is required")

    def _case_for_cycle(self, cycle_id: int):
        return self.service.open_case(
            cycle_id, self.actor_user_id, f"revenue-open-case:{cycle_id}"
        )

    def _active_fully_executed_contracts(self, cycle_id: int):
        rows = self.connection.execute(
            "SELECT * FROM contracts WHERE application_cycle_id=? ORDER BY id",
            (cycle_id,),
        ).fetchall()
        result = []
        for row in rows:
            normalized = str(row["contract_status"] or "").replace("_", "").replace(" ", "").lower()
            if normalized != "fullyexecuted":
                continue
            if "deleted_at" in row.keys() and row["deleted_at"]:
                continue
            if "voided_at" in row.keys() and row["voided_at"]:
                continue
            if "superseded_by_contract_id" in row.keys() and row["superseded_by_contract_id"]:
                continue
            result.append(row)
        return result

    def _record(self, event_name: str, authoritative_id: int, fact, cycle_id=None):
        self._require_admin()
        fact_payload = asdict(fact)
        version = str(fact_payload.get("version") or authoritative_id)
        idempotency_key = (
            f"revenue-operational:{event_name}:{int(authoritative_id)}:{version}"
        )
        payload = {
            "event_name": event_name,
            "authoritative_id": int(authoritative_id),
            "authoritative_version": version,
        }

        def operation():
            case = self._case_for_cycle(cycle_id) if cycle_id is not None else None
            return {
                "uid": case["uid"] if case else f"{event_name}:{authoritative_id}",
                "aggregate_uid": case["uid"] if case else f"{event_name}:{authoritative_id}",
                "operational_status": "recorded",
                "event": event_name,
                "authoritative_fact": fact_payload,
                "commercial_case_uid": case["uid"] if case else None,
            }

        return self.repository.command(
            idempotency_key=idempotency_key,
            command_type=f"revenue_operational_{event_name}",
            aggregate_type="commercial_case" if cycle_id is not None else event_name,
            payload=payload,
            actor_user_id=self.actor_user_id,
            operation=operation,
        )

    def cycle_materialized(self, application_cycle_id: int):
        self._require_admin()
        fact = self.facts.cycle(application_cycle_id)
        return self._record("cycle", fact.id, fact, fact.id)

    def contract_fully_executed(self, contract_id: int):
        self._require_admin()
        fact = self.facts.contract(contract_id)
        if not fact.fully_executed or not fact.fully_executed_at:
            raise ValueError("contract is not FullyExecuted")
        if fact.application_cycle_id is None:
            raise ValueError("FullyExecuted contract has no authoritative application cycle")
        is_transfer = (
            fact.contract_type == "TransferApplicationContract"
            and self.facts.cycle(fact.application_cycle_id).cycle_type == "transfer"
        )

        def operation():
            event = self._record("contract", fact.id, fact, fact.application_cycle_id)
            cycle = self.facts.cycle(fact.application_cycle_id)
            case = self.repository.require_by_uid(
                "revenue_commercial_cases", event["commercial_case_uid"]
            )
            legacy_transfer = False
            if is_transfer:
                import json
                from student_lifecycle.transfer_source_authority import (
                    SourceAuthorityError, resolve_for_contract, schema_ready,
                )
                definition = json.loads(case["classification_evidence_json"] or "{}")
                if definition.get("definition") == "existing_aaie_k12_v1":
                    has_ledger = self.connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                    ).fetchone()
                    marker = has_ledger and self.connection.execute(
                        "SELECT applied_at FROM schema_migrations WHERE version='20260920_041'"
                    ).fetchone()
                    if marker:
                        old_cycle = bool(self.connection.execute(
                            """SELECT 1 FROM application_cycles WHERE id=?
                                 AND julianday(created_at)<julianday(?)""",
                            (cycle.id, marker[0]),
                        ).fetchone())
                        old_case = bool(self.connection.execute(
                            "SELECT 1 WHERE julianday(?)<julianday(?)",
                            (case["opened_at"], marker[0]),
                        ).fetchone())
                        contract_pre_cutover = self.connection.execute(
                            """SELECT 1 FROM contracts WHERE id=?
                                 AND generated_at IS NOT NULL AND fully_executed_at IS NOT NULL
                                 AND julianday(generated_at)<julianday(?)
                                 AND julianday(fully_executed_at)<julianday(?)""",
                            (fact.id, marker[0], marker[0]),
                        ).fetchone()
                        projected = self.connection.execute(
                            "SELECT 1 FROM transfer_cases WHERE case_id=?", (cycle.id,)
                        ).fetchone()
                        source_seen = self.connection.execute(
                            """SELECT 1 FROM transfer_source_authorities
                                 WHERE application_cycle_id=? OR transfer_contract_id IN
                                   (SELECT id FROM contracts WHERE application_cycle_id=?)""",
                            (cycle.id, cycle.id),
                        ).fetchone()
                        legacy_transfer = bool(old_cycle and contract_pre_cutover
                            and (old_case or definition.get("transfer_legacy_cutover") == "20260920_041")
                            and not projected and not source_seen)
                    else:
                        projection = self.connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transfer_cases'"
                        ).fetchone()
                        legacy_transfer = not schema_ready(self.connection) and not projection
                    if not legacy_transfer:
                        raise SourceAuthorityError('Unverified historical Transfer case; HOLD')
                elif definition.get("definition") == "transfer-source-v1":
                    resolve_for_contract(self.connection, cycle.student_id, fact.id)
                else:
                    raise SourceAuthorityError('Unknown Transfer case source definition; HOLD')
            authoritative_contracts = self._active_fully_executed_contracts(
                fact.application_cycle_id
            )
            if len(authoritative_contracts) > 1:
                exception = self.open_exception(
                    "ambiguous_engagement_contract",
                    "commercial_case",
                    case["uid"],
                    "Multiple FullyExecuted contracts have no authoritative supersession.",
                )
                return {
                    **event,
                    "fee_status": "NEEDS_ATTENTION",
                    "reason": "ambiguous_engagement_contract",
                    "exception": exception,
                    "fee_items": [],
                }
            source_engagement_date = fact.contract_date or cycle.version[:10]
            try:
                if is_transfer and not legacy_transfer:
                    from revenue.transfer_pricing import resolve_transfer_base_fee
                    resolved = resolve_transfer_base_fee(
                        self.connection, student_id=cycle.student_id,
                        transfer_contract_id=fact.id,
                        source_engagement_date=source_engagement_date,
                    )
                else:
                    facts = self._client_fee_facts_for_contract(cycle, case)
                    resolved = self.client_fee_rules.resolve(facts, source_engagement_date)
            except MissingFeeRule as exc:
                exception = self.open_exception(
                    "missing_fee_rule",
                    "commercial_case",
                    case["uid"],
                    str(exc),
                )
                return {
                    **event,
                    "fee_status": "NEEDS_ATTENTION",
                    "reason": "missing_fee_rule",
                    "exception": exception,
                    "fee_items": [],
                }
            decision = resolved.snapshot()
            account = self.service.ensure_student_billing_account(
                cycle.student_id, self.actor_user_id,
                f"revenue-billing-account:{cycle.student_id}",
                source_engagement_date,
            )
            item = {
                "milestone_code": resolved.milestone_code,
                "description": "Client service fee",
                "net_cents": resolved.net_cents,
                "gst_cents": 0,
                **resolved.snapshot(),
                "contract_id": fact.id,
                "application_cycle_id": cycle.id,
                "contract_fully_executed_at": fact.fully_executed_at,
            }
            fees = self.service.create_fee_items(
                case["uid"], account["uid"], [item], self.actor_user_id,
                f"revenue-fee:contract:{fact.id}:{resolved.rule_uid}",
                resolved.rule_uid,
            )
            contract_links = [
                self.client_payments.ensure_fee_item_contract_link(
                    fee["uid"],
                    actor_user_id=self.actor_user_id,
                    idempotency_key=f"revenue-fee-contract-link:{fee['uid']}:{fact.id}",
                )
                for fee in fees
            ]
            return {
                **event,
                "fee_status": "CREATED",
                "fee_policy": decision,
                "fee_items": fees,
                "contract_links": contract_links,
            }

        if is_transfer:
            return self.repository.command(
                idempotency_key=f"transfer-contract-fully-executed:{fact.id}",
                command_type="transfer_contract_fully_executed",
                aggregate_type="transfer_contract",
                payload={"contract_id": fact.id, "application_cycle_id": fact.application_cycle_id},
                actor_user_id=self.actor_user_id,
                operation=operation,
            )
        return operation()

    def visa_case_changed(self, visa_case_id: int):
        self._require_admin()
        fact = self.facts.visa_case(visa_case_id)
        event = self._record("visa", fact.id, fact, fact.application_cycle_id)
        if fact.application_cycle_id is None:
            return {**event, "visa_fee_cents": None, "fee_items": []}
        cycle = self.facts.cycle(fact.application_cycle_id)
        case = self.repository.require_by_uid(
            "revenue_commercial_cases", event["commercial_case_uid"]
        )
        fee_facts = self._client_fee_facts_for_visa(cycle, case, fact)
        authoritative_contracts = self._active_fully_executed_contracts(
            fact.application_cycle_id
        )
        if len(authoritative_contracts) > 1:
            exception = self.open_exception(
                "ambiguous_engagement_contract",
                "commercial_case",
                case["uid"],
                "Multiple FullyExecuted contracts have no authoritative supersession.",
            )
            return {
                **event,
                "fee_status": "NEEDS_ATTENTION",
                "reason": "ambiguous_engagement_contract",
                "exception": exception,
                "visa_fee_cents": None,
                "fee_items": [],
            }
        if authoritative_contracts:
            contract = authoritative_contracts[0]
            source_engagement_date = str(
                contract["contract_date"]
                or contract["fully_executed_at"]
                or cycle.version
            )[:10]
        else:
            source_engagement_date = str(fact.version or cycle.version)[:10]
        try:
            resolved = self.client_fee_rules.resolve(fee_facts, source_engagement_date)
        except MissingFeeRule as exc:
            exception = self.open_exception(
                "missing_fee_rule",
                "commercial_case",
                case["uid"],
                str(exc),
            )
            return {
                **event,
                "fee_status": "NEEDS_ATTENTION",
                "reason": "missing_fee_rule",
                "exception": exception,
                "visa_fee_cents": None,
                "fee_items": [],
            }
        decision = resolved.snapshot()
        if resolved.net_cents is None:
            return {
                **event,
                "fee_status": "INCLUDED",
                "visa_fee_policy": decision,
                "visa_fee_cents": None, "fee_items": [],
            }
        account = self.service.ensure_student_billing_account(
            cycle.student_id, self.actor_user_id,
            f"revenue-billing-account:{cycle.student_id}", cycle.version[:10],
        )
        item = {
            "milestone_code": resolved.milestone_code,
            "description": "Visa service fee",
            "net_cents": resolved.net_cents,
            "gst_cents": 0,
            **resolved.snapshot(),
            "visa_case_id": fact.id,
            "application_cycle_id": cycle.id,
        }
        fees = self.service.create_fee_items(
            case["uid"], account["uid"], [item], self.actor_user_id,
            f"revenue-fee:visa:{fact.id}:{resolved.rule_uid}",
            resolved.rule_uid,
        )
        return {
            **event,
            "fee_status": "CREATED",
            "visa_fee_policy": decision,
            "visa_fee_cents": resolved.net_cents,
            "fee_items": fees,
        }

    def _client_fee_facts_for_contract(self, cycle, case) -> ClientFeeRuleFacts:
        he_category = None
        if cycle.education_stream == "higher_education":
            application_columns = {
                str(row[1])
                for row in self.connection.execute(
                    "PRAGMA table_info(higher_education_applications)"
                )
            }
            current_clause = (
                " AND COALESCE(is_current,1)=1" if "is_current" in application_columns else ""
            )
            applications = self.connection.execute(
                "SELECT * FROM higher_education_applications WHERE application_cycle_id=?"
                + current_clause
                + " ORDER BY id",
                (cycle.id,),
            ).fetchall()
            if len(applications) == 1:
                row = applications[0]
                course_type = (
                    str(row["course_type"] or "").strip().lower()
                    if "course_type" in row.keys()
                    else ""
                )
                if course_type:
                    he_category = (
                        "language_school"
                        if any(token in course_type for token in ("language", "elicos", "english"))
                        else "university_foundation"
                    )
        return ClientFeeRuleFacts(
            service_kind="base",
            education_stream=cycle.education_stream,
            cycle_type=cycle.cycle_type,
            existing_aaie_k12=bool(case["existing_aaie_k12"]),
            he_category=he_category,
            onshore=cycle.cycle_type in {"transfer", "progression"},
        )

    @staticmethod
    def _client_fee_facts_for_visa(cycle, case, fact) -> ClientFeeRuleFacts:
        normalized = " ".join(
            str(value or "").strip().lower()
            for value in (fact.case_type, fact.visa_subclass, fact.application_type)
        )
        is_590 = "590" in normalized
        renewal = any(token in normalized for token in ("renewal", "renew", "extension"))
        independent = renewal or "standalone" in normalized or "independent" in normalized
        return ClientFeeRuleFacts(
            service_kind="guardian_590" if is_590 else "student_visa",
            education_stream=cycle.education_stream,
            cycle_type=cycle.cycle_type,
            existing_aaie_k12=bool(case["existing_aaie_k12"]),
            application_type="renewal" if renewal else "new",
            independent_service=independent,
        )

    def offer_received(self, offer_event_id: int):
        self._require_admin()
        fact = self.facts.offer(offer_event_id)
        event = self._record("offer", fact.id, fact, fact.application_cycle_id)
        return {**event, "projection_status": "projection_only"}

    def final_acceptance_selected(self, acceptance_id: int):
        self._require_admin()
        fact = self.facts.final_acceptance(acceptance_id)
        if fact.acceptance_status != "active":
            raise ValueError("final acceptance is not active")
        event = self._record("acceptance", fact.id, fact, fact.application_cycle_id)
        case = self.repository.require_by_uid(
            "revenue_commercial_cases", event["commercial_case_uid"]
        )
        component_row = self.connection.execute(
            "SELECT * FROM revenue_enrolment_components WHERE final_acceptance_id=?",
            (fact.id,),
        ).fetchone()
        if component_row:
            component = dict(component_row)
        else:
            component = self.service.link_enrolment_component(
                case["uid"], "school", "final_acceptance", fact.id,
                self.actor_user_id, f"revenue-component:acceptance:{fact.id}",
                final_acceptance_id=fact.id,
            )
        entity, rule = self._school_entity_and_rule(fact.school_id)
        if not entity:
            self.open_exception(
                "missing_revenue_entity", "enrolment_component", component["uid"],
                "Final institution has no Revenue entity link.",
            )
            return {
                **event, "component": component,
                "commission_status": "NEEDS_ATTENTION",
                "reason": "missing_revenue_entity",
            }
        if not rule:
            self.open_exception(
                "missing_commission_rule", "enrolment_component", component["uid"],
                "Final institution has no active Admin-approved commission rule.",
            )
            return {
                **event, "component": component,
                "commission_status": "NEEDS_ATTENTION",
                "reason": "missing_commission_rule",
            }
        prior = self.connection.execute(
            "SELECT * FROM revenue_commission_lines WHERE enrolment_component_id=?",
            (component["id"],),
        ).fetchone()
        cycle = self.facts.cycle(fact.application_cycle_id)
        if prior:
            line = {
                **dict(prior),
                "instalments": [dict(row) for row in self.connection.execute(
                    """SELECT * FROM revenue_commission_instalments
                        WHERE commission_line_id=? ORDER BY instalment_no""",
                    (prior["id"],),
                ).fetchall()],
            }
            if cycle.cycle_type == "transfer":
                line["reason_code"] = "onshore_transfer"
        elif cycle.cycle_type == "transfer":
            line = self.service.calculate_zero_commission(
                case["uid"], component["uid"], entity["uid"], rule["uid"],
                "onshore_transfer", self.actor_user_id, "Admin",
                f"revenue-zero-commission:acceptance:{fact.id}",
            )
        else:
            tuition = self.connection.execute(
                """SELECT 1 FROM revenue_tuition_facts
                    WHERE enrolment_component_id=? AND status='active' LIMIT 1""",
                (component["id"],),
            ).fetchone()
            if not tuition:
                self.open_exception(
                    "missing_structured_tuition", "enrolment_component", component["uid"],
                    "Structured tuition is required before commission calculation.",
                )
                return {
                    **event, "component": component,
                    "commission_status": "NEEDS_ATTENTION",
                    "reason": "missing_structured_tuition",
                }
            line = self.service.calculate_commission(
                case["uid"], component["uid"], entity["uid"], rule["uid"],
                self.actor_user_id, "Admin",
                f"revenue-commission:acceptance:{fact.id}",
            )
        if cycle.cycle_type == "transfer":
            return {
                **event, "component": component,
                "commission_status": "PROJECTED_ZERO", "reason": "onshore_transfer",
                "commission_line": line,
            }
        return {
            **event, "component": component,
            "commission_status": "PROJECTED", "reason": None,
            "commission_line": line,
        }

    def coe_changed(self, coe_id: int):
        self._require_admin()
        fact = self.facts.coe(coe_id)
        event = self._record("coe", fact.id, fact, fact.application_cycle_id)
        if fact.application_cycle_id is None:
            return {**event, "coe_status": "unlinked"}
        case = self.connection.execute(
            "SELECT id FROM revenue_commercial_cases WHERE application_cycle_id=?",
            (fact.application_cycle_id,),
        ).fetchone()
        components = self.connection.execute(
            """SELECT * FROM revenue_enrolment_components
                WHERE commercial_case_id=? AND status='active' ORDER BY id""",
            (case["id"],),
        ).fetchall() if case else []
        if fact.language_course_name and fact.main_course_name and case:
            acceptance = self.connection.execute(
                """SELECT * FROM cycle_final_acceptances
                    WHERE application_cycle_id=? AND acceptance_status='active'
                    ORDER BY id DESC LIMIT 1""",
                (fact.application_cycle_id,),
            ).fetchone()
            language = self.connection.execute(
                """SELECT * FROM revenue_enrolment_components
                    WHERE commercial_case_id=? AND source_kind='coe_language' AND source_id=?""",
                (case["id"], fact.id),
            ).fetchone()
            if acceptance and not language:
                case_record = self.connection.execute(
                    "SELECT uid FROM revenue_commercial_cases WHERE id=?", (case["id"],)
                ).fetchone()
                self.service.link_enrolment_component(
                    case_record["uid"], "school", "coe_language", fact.id,
                    self.actor_user_id, f"revenue-component:coe-language:{fact.id}",
                    final_acceptance_id=acceptance["id"], coe_oshc_id=fact.id,
                )
                components = self.connection.execute(
                    """SELECT * FROM revenue_enrolment_components
                        WHERE commercial_case_id=? AND status='active' ORDER BY id""",
                    (case["id"],),
                ).fetchall()
            for component in components:
                if component["coe_oshc_id"] != fact.id:
                    self.connection.execute(
                        "UPDATE revenue_enrolment_components SET coe_oshc_id=? WHERE id=?",
                        (fact.id, component["id"]),
                    )
            return {
                **event, "coe_status": "package_context_linked",
                "components": [dict(row) for row in components],
            }
        if len(components) != 1:
            return {**event, "coe_status": "needs_component_selection"}
        component = components[0]
        key = f"revenue-coe-link:{fact.id}:{component['id']}"

        def operation():
            self.connection.execute(
                "UPDATE revenue_enrolment_components SET coe_oshc_id=? WHERE id=?",
                (fact.id, component["id"]),
            )
            return self.repository.require_by_uid(
                "revenue_enrolment_components", component["uid"]
            )

        linked = self.repository.command(
            idempotency_key=key, command_type="coe_context_linked",
            aggregate_type="enrolment_component",
            payload={"coe_id": fact.id, "component_uid": component["uid"]},
            actor_user_id=self.actor_user_id, operation=operation,
        )
        return {**event, "coe_status": "context_linked", "component": linked}

    def tuition_confirmed(
        self, component_uid: str, academic_period: str, fee_component: str,
        tuition_cents: int, authority_level: str, source_reference: str,
        evidence_document_id: int | None = None,
    ):
        self._require_operator()
        fact = self.service.record_tuition(
            component_uid, academic_period, fee_component, tuition_cents,
            authority_level, self.actor_user_id,
            f"revenue-tuition:{component_uid}:{academic_period}:{fee_component}:{source_reference}",
            evidence_document_id,
        )
        return {"tuition_status": "confirmed", "tuition": fact}

    def actual_commencement_confirmed(
        self, component_uid: str, actual_date: str,
        evidence_document_id: int | None,
    ):
        self._require_operator()
        fact = self.service.confirm_commencement(
            component_uid, actual_date, self.actor_user_id,
            f"revenue-commencement:{component_uid}:{actual_date}",
            evidence_document_id,
        )
        return {"commencement_status": "confirmed", "commencement": fact}

    def project_component_commission(
        self, component_uid: str, school_id: int, authoritative_key: str,
    ):
        self._require_admin()
        component = self.repository.require_by_uid(
            "revenue_enrolment_components", component_uid
        )
        case = self.connection.execute(
            "SELECT * FROM revenue_commercial_cases WHERE id=?",
            (component["commercial_case_id"],),
        ).fetchone()
        entity, rule = self._school_entity_and_rule(school_id)
        if not entity or not rule:
            return {"commission_status": "NEEDS_ATTENTION", "reason": "missing_commission_rule"}
        tuition = self.connection.execute(
            """SELECT 1 FROM revenue_tuition_facts
                WHERE enrolment_component_id=? AND status='active' LIMIT 1""",
            (component["id"],),
        ).fetchone()
        if not tuition:
            return {"commission_status": "NEEDS_ATTENTION", "reason": "missing_structured_tuition"}
        prior = self.connection.execute(
            "SELECT * FROM revenue_commission_lines WHERE enrolment_component_id=?",
            (component["id"],),
        ).fetchone()
        if prior:
            line = dict(prior)
        else:
            line = self.service.calculate_commission(
                case["uid"], component_uid, entity["uid"], rule["uid"],
                self.actor_user_id, "Admin",
                f"revenue-component-commission:{authoritative_key}",
            )
        return {"commission_status": "PROJECTED", "commission_line": line}

    def project_higher_education_commission(
        self, component_uid: str, provider_id: int, authoritative_key: str,
    ):
        self._require_admin()
        component = self.repository.require_by_uid(
            "revenue_enrolment_components", component_uid
        )
        if component["component_kind"] not in {"higher_education", "package_segment"}:
            raise ValueError("higher-education component is required")
        case = self.connection.execute(
            "SELECT * FROM revenue_commercial_cases WHERE id=?",
            (component["commercial_case_id"],),
        ).fetchone()
        entity, rule = self._higher_education_entity_and_rule(provider_id)
        if not entity or not rule:
            return {"commission_status": "NEEDS_ATTENTION", "reason": "missing_commission_rule"}
        tuition = self.connection.execute(
            """SELECT 1 FROM revenue_tuition_facts
                WHERE enrolment_component_id=? AND status='active' LIMIT 1""",
            (component["id"],),
        ).fetchone()
        if not tuition:
            return {"commission_status": "NEEDS_ATTENTION", "reason": "missing_structured_tuition"}
        prior = self.connection.execute(
            "SELECT * FROM revenue_commission_lines WHERE enrolment_component_id=?",
            (component["id"],),
        ).fetchone()
        line = dict(prior) if prior else self.service.calculate_commission(
            case["uid"], component_uid, entity["uid"], rule["uid"],
            self.actor_user_id, "Admin",
            f"revenue-he-commission:{authoritative_key}",
        )
        return {"commission_status": "PROJECTED", "commission_line": line}

    def higher_education_changed(self, application_id: int):
        self._require_admin()
        fact = self.facts.higher_education_application(application_id)
        event = self._record(
            "higher_education", fact.id, fact, fact.application_cycle_id
        )
        case = self.repository.require_by_uid(
            "revenue_commercial_cases", event["commercial_case_uid"]
        )
        component = self.service.link_enrolment_component(
            case["uid"], "higher_education", "higher_education_application",
            fact.id, self.actor_user_id,
            f"revenue-he-component:{fact.id}",
            higher_education_application_id=fact.id,
        )
        entity, rule = self._higher_education_entity_and_rule(fact.provider_id)
        context = {
            "application_id": fact.id,
            "provider_id": fact.provider_id,
            "course_type": fact.course_type,
            "course_name": fact.course_name,
            "intake": fact.intake,
            "submission_channel": fact.submission_channel,
            "master_agent_id": fact.master_agent_id,
            "status": fact.status,
        }
        if not entity:
            self.open_exception(
                "MISSING_PROVIDER_ENTITY", "commercial_case", case["uid"],
                f"HE provider {fact.provider_id} has no active Revenue entity link.",
            )
            return {
                **event, "component": component,
                "authoritative_context": context,
                "provider_status": "NEEDS_ATTENTION",
                "institution_status": "NEEDS_ATTENTION",
                "reason": "missing_provider_entity",
                "provider_entity": None,
            }
        if not rule:
            self.open_exception(
                "MISSING_COMMISSION_RULE", "commercial_case", case["uid"],
                f"HE provider {fact.provider_id} has no active commission rule.",
            )
            return {
                **event, "component": component,
                "authoritative_context": context,
                "provider_status": "READY",
                "institution_status": "NEEDS_ATTENTION",
                "reason": "missing_commission_rule",
                "provider_entity": entity,
                "commission_rule": None,
            }
        master_agent = None
        if fact.submission_channel == "Master Agent":
            master_agent = self._master_agent_entity(fact.master_agent_id)
            if not master_agent:
                self.open_exception(
                    "MISSING_MASTER_AGENT_ENTITY", "commercial_case", case["uid"],
                    "HE Master Agent submission has no active Revenue master-agent entity.",
                )
                return {
                    **event, "component": component,
                    "authoritative_context": context,
                    "provider_status": "READY",
                    "institution_status": "NEEDS_ATTENTION",
                    "reason": "missing_master_agent_entity",
                    "provider_entity": entity,
                    "commission_rule": rule,
                    "master_agent_entity": None,
                }
        return {
            **event, "component": component,
            "authoritative_context": context,
            "provider_status": "READY", "institution_status": "READY",
            "reason": None, "provider_entity": entity,
            "commission_rule": rule, "master_agent_entity": master_agent,
        }

    def open_exception(
        self, exception_code: str, aggregate_type: str, aggregate_uid: str,
        message: str, severity: str = "blocking",
    ):
        self._require_admin()
        # Finish this write when called on a request-scoped connection, while
        # preserving rollback ownership when a caller already has a transaction.
        with self.repository.transaction():
            prior = self.connection.execute(
                """SELECT * FROM revenue_exceptions
                    WHERE exception_code=? AND aggregate_type=? AND aggregate_uid=?
                      AND status='open'""",
                (exception_code, aggregate_type, aggregate_uid),
            ).fetchone()
            if prior:
                return dict(prior)
            uid = new_uid("exception")
            self.connection.execute(
                """INSERT INTO revenue_exceptions(
                     uid,exception_code,severity,aggregate_type,aggregate_uid,message,status,opened_at
                   ) VALUES(?,?,?,?,?,?,'open',?)""",
                (uid, exception_code, severity, aggregate_type, aggregate_uid,
                 message, self.repository.now()),
            )
            return self.repository.require_by_uid("revenue_exceptions", uid)


    def _school_entity_and_rule(self, school_id):
        if school_id is None:
            return None, None
        entity_row = self.connection.execute(
            """SELECT e.* FROM revenue_entities e
                JOIN revenue_entity_provider_links l ON l.revenue_entity_id=e.id
               WHERE l.school_id=? AND l.effective_to IS NULL AND e.status='active'
               ORDER BY l.effective_from DESC,l.id DESC LIMIT 1""",
            (school_id,),
        ).fetchone()
        if not entity_row:
            return None, None
        rule_row = self.connection.execute(
            """SELECT r.* FROM revenue_commission_rule_versions r
                JOIN revenue_contract_versions v ON v.id=r.contract_version_id
                JOIN revenue_contract_groups g ON g.id=v.contract_group_id
               WHERE g.revenue_entity_id=? AND v.status='active' AND r.status='active'
               ORDER BY r.priority DESC,r.id DESC LIMIT 1""",
            (entity_row["id"],),
        ).fetchone()
        return dict(entity_row), dict(rule_row) if rule_row else None

    def _higher_education_entity_and_rule(self, provider_id):
        if provider_id is None:
            return None, None
        entity_row = self.connection.execute(
            """SELECT e.* FROM revenue_entities e
                JOIN revenue_entity_provider_links l ON l.revenue_entity_id=e.id
               WHERE l.higher_education_provider_id=? AND l.effective_to IS NULL
                 AND e.entity_type='higher_education_provider' AND e.status='active'
               ORDER BY l.effective_from DESC,l.id DESC LIMIT 1""",
            (provider_id,),
        ).fetchone()
        if not entity_row:
            return None, None
        rule_row = self.connection.execute(
            """SELECT r.* FROM revenue_commission_rule_versions r
                JOIN revenue_contract_versions v ON v.id=r.contract_version_id
                JOIN revenue_contract_groups g ON g.id=v.contract_group_id
               WHERE g.revenue_entity_id=? AND v.status='active' AND r.status='active'
               ORDER BY r.priority DESC,r.id DESC LIMIT 1""",
            (entity_row["id"],),
        ).fetchone()
        return dict(entity_row), dict(rule_row) if rule_row else None

    def _master_agent_entity(self, master_agent_id):
        if master_agent_id is None:
            return None
        row = self.connection.execute(
            """SELECT * FROM revenue_entities
                WHERE id=? AND entity_type='master_agent' AND status='active'""",
            (master_agent_id,),
        ).fetchone()
        return dict(row) if row else None
