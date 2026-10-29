"""Revenue domain services. Routes and templates must delegate calculations here."""

from __future__ import annotations

from revenue.access_policy import can_operate_revenue

import hashlib
import json

from revenue.ai import ProviderUnavailable
from revenue.claim_eligibility import ClaimEligibilityResolver
from revenue.client_fee_rules import DatabaseClientFeeRuleResolver
from revenue.facts import RevenueFactAdapter
from revenue.money import allocate_cents, apply_basis_points, gst_from_inclusive
from revenue.repository import RevenueRepository, canonical_json, new_uid
from revenue.rules import CommissionRuleEvaluator


AUTHORITY_PRIORITY = {"estimated": 1, "manual": 2, "official": 3}


class RevenueService:
    def __init__(self, repository: RevenueRepository):
        self.repository = repository
        self.connection = repository.connection

    def open_case(self, application_cycle_id: int, actor_user_id: int, idempotency_key: str):
        payload = {"application_cycle_id": int(application_cycle_id)}

        def operation():
            existing = self.connection.execute(
                "SELECT * FROM revenue_commercial_cases WHERE application_cycle_id=?",
                (application_cycle_id,),
            ).fetchone()
            if existing:
                return dict(existing)
            cycle = self.connection.execute(
                "SELECT id,student_id,cycle_type FROM application_cycles WHERE id=?",
                (application_cycle_id,),
            ).fetchone()
            if not cycle:
                raise ValueError("application cycle not found")
            if cycle["cycle_type"] == "transfer":
                from student_lifecycle.transfer_source_authority import (
                    SourceAuthorityError, resolve_for_cycle, schema_ready,
                )
                if schema_ready(self.connection):
                    marker = self.connection.execute(
                        "SELECT applied_at FROM schema_migrations WHERE version='20260920_041'"
                    ).fetchone() if self.connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                    ).fetchone() else None
                    projected = self.connection.execute(
                        "SELECT 1 FROM transfer_cases WHERE case_id=?",
                        (application_cycle_id,),
                    ).fetchone() if self.connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transfer_cases'"
                    ).fetchone() else None
                    source_seen = self.connection.execute(
                        """SELECT 1 FROM transfer_source_authorities
                             WHERE application_cycle_id=? OR transfer_contract_id IN
                               (SELECT id FROM contracts WHERE application_cycle_id=?)""",
                        (application_cycle_id, application_cycle_id),
                    ).fetchone()
                    later_contract = self.connection.execute(
                        """SELECT 1 FROM contracts WHERE application_cycle_id=?
                             AND contract_type='TransferApplicationContract'
                             AND deleted_at IS NULL AND voided_at IS NULL
                             AND (generated_at IS NULL OR fully_executed_at IS NULL
                                  OR julianday(generated_at) IS NULL
                                  OR julianday(fully_executed_at) IS NULL
                                  OR julianday(generated_at)>=julianday(?)
                                  OR julianday(fully_executed_at)>=julianday(?))""",
                        (application_cycle_id, marker[0], marker[0]),
                    ).fetchone() if marker else None
                    historical = bool(marker and not projected and not source_seen and not later_contract and
                        self.connection.execute(
                            """SELECT 1 FROM application_cycles WHERE id=?
                                 AND julianday(created_at)<julianday(?)""",
                            (application_cycle_id, marker[0]),
                        ).fetchone())
                    if historical:
                        decision = RevenueFactAdapter(self.connection).resolve_existing_aaie_k12(
                            application_cycle_id
                        )
                        is_existing = decision.is_existing
                        classification_evidence = {
                            **decision.evidence(),
                            "transfer_legacy_cutover": "20260920_041",
                        }
                    else:
                        source = resolve_for_cycle(
                            self.connection, cycle["student_id"], application_cycle_id
                        )
                        is_existing = source["source_classification"] == "aaie_existing"
                        classification_evidence = {
                            "definition": "transfer-source-v1",
                            "source_classification": source["source_classification"],
                            "transfer_contract_id": source["transfer_contract_id"],
                            "application_cycle_id": application_cycle_id,
                        }
                else:
                    # Only a pre-source schema may retain the old Revenue path.
                    # A broken 041 schema or an already-projected Transfer is not legacy.
                    has_ledger = self.connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                    ).fetchone()
                    applied_041 = has_ledger and self.connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE version='20260920_041'"
                    ).fetchone()
                    has_transfer_projection = self.connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transfer_cases'"
                    ).fetchone()
                    if applied_041 or has_transfer_projection:
                        raise SourceAuthorityError('Transfer source schema unavailable; HOLD')
                    decision = RevenueFactAdapter(self.connection).resolve_existing_aaie_k12(
                        application_cycle_id
                    )
                    is_existing = decision.is_existing
                    classification_evidence = decision.evidence()
            else:
                decision = RevenueFactAdapter(self.connection).resolve_existing_aaie_k12(
                    application_cycle_id
                )
                is_existing = decision.is_existing
                classification_evidence = decision.evidence()
            uid = new_uid("case")
            self.connection.execute(
                """INSERT INTO revenue_commercial_cases(
                     uid,application_cycle_id,student_id,status,existing_aaie_k12,
                     classification_evidence_json,opened_by,opened_at
                   ) VALUES(?,?,?,'open',?,?,?,?)""",
                (
                    uid, application_cycle_id, cycle["student_id"],
                    int(is_existing), canonical_json(classification_evidence),
                    actor_user_id, self.repository.now(),
                ),
            )
            return self.repository.require_by_uid("revenue_commercial_cases", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="commercial_case_opened",
            aggregate_type="commercial_case", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def link_enrolment_component(
        self, commercial_case_uid: str, component_kind: str, source_kind: str,
        source_id: int, actor_user_id: int, idempotency_key: str,
        final_acceptance_id: int | None = None,
        school_enrolment_id: int | None = None,
        higher_education_application_id: int | None = None,
        higher_education_package_segment_id: int | None = None,
        coe_oshc_id: int | None = None,
    ):
        payload = {
            "commercial_case_uid": commercial_case_uid, "component_kind": component_kind,
            "source_kind": source_kind, "source_id": int(source_id),
            "final_acceptance_id": final_acceptance_id,
            "school_enrolment_id": school_enrolment_id,
            "higher_education_application_id": higher_education_application_id,
            "higher_education_package_segment_id": higher_education_package_segment_id,
            "coe_oshc_id": coe_oshc_id,
        }

        def operation():
            case = self.repository.require_by_uid("revenue_commercial_cases", commercial_case_uid)
            prior = self.connection.execute(
                """SELECT * FROM revenue_enrolment_components
                    WHERE commercial_case_id=? AND source_kind=? AND source_id=?""",
                (case["id"], source_kind, source_id),
            ).fetchone()
            if prior:
                return dict(prior)
            uid = new_uid("component")
            self.connection.execute(
                """INSERT INTO revenue_enrolment_components(
                     uid,commercial_case_id,component_kind,source_kind,source_id,
                     final_acceptance_id,school_enrolment_id,higher_education_application_id,
                     higher_education_package_segment_id,coe_oshc_id,status,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,'active',?)""",
                (
                    uid, case["id"], component_kind, source_kind, source_id,
                    final_acceptance_id, school_enrolment_id,
                    higher_education_application_id, higher_education_package_segment_id,
                    coe_oshc_id, self.repository.now(),
                ),
            )
            return self.repository.require_by_uid("revenue_enrolment_components", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="enrolment_component_linked",
            aggregate_type="enrolment_component", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def propose_commencement(
        self, component_uid: str, actual_date: str, actor_user_id: int,
        idempotency_key: str, evidence_document_id: int | None = None,
        source_kind: str = "institution",
    ):
        return self._record_commencement(
            component_uid, actual_date, "proposed", source_kind,
            actor_user_id, idempotency_key, evidence_document_id,
        )

    def confirm_commencement(
        self, component_uid: str, actual_date: str, actor_user_id: int,
        idempotency_key: str, evidence_document_id: int | None = None,
        source_kind: str = "admin_confirmed",
    ):
        return self._record_commencement(
            component_uid, actual_date, "confirmed", source_kind,
            actor_user_id, idempotency_key, evidence_document_id,
        )

    def _record_commencement(
        self, component_uid, actual_date, status, source_kind, actor_user_id,
        idempotency_key, evidence_document_id,
    ):
        payload = {
            "component_uid": component_uid, "actual_date": actual_date,
            "status": status, "source_kind": source_kind,
            "evidence_document_id": evidence_document_id,
        }

        def operation():
            component = self.repository.require_by_uid(
                "revenue_enrolment_components", component_uid
            )
            supersedes_id = None
            if status == "confirmed":
                prior = self.connection.execute(
                    "SELECT * FROM revenue_commencement_facts WHERE enrolment_component_id=? AND status='confirmed'",
                    (component["id"],),
                ).fetchone()
                if prior:
                    supersedes_id = prior["id"]
                    self.connection.execute(
                        "UPDATE revenue_commencement_facts SET status='superseded' WHERE id=?",
                        (prior["id"],),
                    )
            uid = new_uid("commencement")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_commencement_facts(
                     uid,enrolment_component_id,actual_commencement_date,evidence_document_id,
                     source_kind,status,supersedes_id,confirmed_by,confirmed_at,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    uid, component["id"], actual_date, evidence_document_id,
                    source_kind, status, supersedes_id,
                    actor_user_id if status == "confirmed" else None,
                    now if status == "confirmed" else None, now,
                ),
            )
            return self.repository.require_by_uid("revenue_commencement_facts", uid)

        return self.repository.command(
            idempotency_key=idempotency_key,
            command_type=f"commencement_{status}", aggregate_type="commencement_fact",
            payload=payload, actor_user_id=actor_user_id, operation=operation,
        )

    def record_tuition(
        self, component_uid: str, academic_period: str, fee_component: str,
        tuition_cents: int, authority_level: str, actor_user_id: int,
        idempotency_key: str, evidence_document_id: int | None = None,
        currency_code: str = "AUD",
    ):
        if authority_level not in AUTHORITY_PRIORITY:
            raise ValueError("invalid tuition authority")
        if isinstance(tuition_cents, bool) or not isinstance(tuition_cents, int) or tuition_cents < 0:
            raise ValueError("tuition_cents must be a non-negative integer")
        payload = {
            "component_uid": component_uid, "academic_period": academic_period,
            "fee_component": fee_component, "tuition_cents": tuition_cents,
            "authority_level": authority_level, "evidence_document_id": evidence_document_id,
            "currency_code": currency_code,
        }

        def operation():
            component = self.repository.require_by_uid(
                "revenue_enrolment_components", component_uid
            )
            prior = self.connection.execute(
                """SELECT * FROM revenue_tuition_facts
                    WHERE enrolment_component_id=? AND academic_period=?
                      AND fee_component=? AND status='active'""",
                (component["id"], academic_period, fee_component),
            ).fetchone()
            if prior and AUTHORITY_PRIORITY[authority_level] < AUTHORITY_PRIORITY[prior["authority_level"]]:
                return dict(prior)
            supersedes_id = prior["id"] if prior else None
            if prior:
                self.connection.execute(
                    "UPDATE revenue_tuition_facts SET status='superseded' WHERE id=?",
                    (prior["id"],),
                )
            uid = new_uid("tuition")
            self.connection.execute(
                """INSERT INTO revenue_tuition_facts(
                     uid,enrolment_component_id,academic_period,fee_component,tuition_cents,
                     currency_code,authority_level,evidence_document_id,status,supersedes_id,
                     confirmed_by,created_at
                   ) VALUES(?,?,?,?,?,?,?,?, 'active',?,?,?)""",
                (
                    uid, component["id"], academic_period, fee_component,
                    tuition_cents, currency_code, authority_level, evidence_document_id,
                    supersedes_id, actor_user_id, self.repository.now(),
                ),
            )
            return self.repository.require_by_uid("revenue_tuition_facts", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="tuition_recorded",
            aggregate_type="tuition_fact", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def commercial_summary(self, component_uid: str) -> dict:
        component = self.repository.require_by_uid(
            "revenue_enrolment_components", component_uid
        )
        commencement = self.connection.execute(
            "SELECT * FROM revenue_commencement_facts WHERE enrolment_component_id=? AND status='confirmed'",
            (component["id"],),
        ).fetchone()
        tuition = self.connection.execute(
            """SELECT * FROM revenue_tuition_facts
               WHERE enrolment_component_id=? AND status='active'
               ORDER BY CASE authority_level WHEN 'official' THEN 3 WHEN 'manual' THEN 2 ELSE 1 END DESC,
                        academic_period DESC,id DESC LIMIT 1""",
            (component["id"],),
        ).fetchone()
        exceptions = []
        if not commencement:
            exceptions.append("missing_actual_commencement")
        if not tuition:
            exceptions.append("missing_structured_tuition")
        return {
            "component_uid": component_uid,
            "actual_commencement_date": commencement["actual_commencement_date"] if commencement else None,
            "tuition_cents": tuition["tuition_cents"] if tuition else None,
            "tuition_authority_level": tuition["authority_level"] if tuition else None,
            "exceptions": exceptions,
        }

    def create_billing_account(
        self, account_type: str, display_name: str, actor_user_id: int,
        idempotency_key: str, currency_code: str = "AUD",
    ):
        payload = {"account_type": account_type, "display_name": display_name,
                   "currency_code": currency_code}

        def operation():
            uid = new_uid("billing-account")
            self.connection.execute(
                """INSERT INTO revenue_billing_accounts(
                     uid,account_type,display_name,currency_code,status,created_by,created_at
                   ) VALUES(?,?,?,?,'active',?,?)""",
                (uid, account_type, display_name, currency_code, actor_user_id, self.repository.now()),
            )
            return self.repository.require_by_uid("revenue_billing_accounts", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="billing_account_created",
            aggregate_type="billing_account", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def ensure_student_billing_account(
        self, student_id: str, actor_user_id: int, idempotency_key: str,
        effective_from: str,
    ):
        # One current account per student is the stable command identity; later
        # events must reuse it even when their own authority dates differ.
        payload = {"student_id": student_id}

        def operation():
            prior = self.connection.execute(
                """SELECT a.* FROM revenue_billing_accounts a
                    JOIN revenue_billing_account_students s ON s.billing_account_id=a.id
                   WHERE s.student_id=? AND s.effective_to IS NULL AND a.status='active'
                   ORDER BY s.effective_from,id LIMIT 1""",
                (student_id,),
            ).fetchone()
            if prior:
                return dict(prior)
            account_uid = new_uid("billing-account")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_billing_accounts(
                     uid,account_type,display_name,currency_code,status,created_by,created_at
                   ) VALUES(?,'family',?,'AUD','active',?,?)""",
                (account_uid, f"{student_id} Family", actor_user_id, now),
            )
            account = self.repository.require_by_uid(
                "revenue_billing_accounts", account_uid
            )
            self.connection.execute(
                """INSERT INTO revenue_billing_account_students(
                     uid,billing_account_id,student_id,effective_from,created_at
                   ) VALUES(?,?,?,?,?)""",
                (new_uid("billing-student"), account["id"], student_id,
                 effective_from, now),
            )
            return account

        return self.repository.command(
            idempotency_key=idempotency_key,
            command_type="student_billing_account_ensured",
            aggregate_type="billing_account",
            payload=payload,
            actor_user_id=actor_user_id,
            operation=operation,
        )

    def _rule_snapshot(self, rule_kind: str, source_rule_uid: str, value: dict, actor_user_id: int):
        canonical = canonical_json(value)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        prior = self.connection.execute(
            "SELECT * FROM revenue_rule_snapshots WHERE sha256=?", (digest,)
        ).fetchone()
        if prior:
            return dict(prior)
        uid = new_uid("snapshot")
        self.connection.execute(
            """INSERT INTO revenue_rule_snapshots(
                 uid,rule_kind,source_rule_uid,canonical_json,sha256,created_by,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (uid, rule_kind, source_rule_uid, canonical, digest, actor_user_id, self.repository.now()),
        )
        return self.repository.require_by_uid("revenue_rule_snapshots", uid)

    def create_fee_items(
        self, commercial_case_uid: str, billing_account_uid: str, items: list[dict],
        actor_user_id: int, idempotency_key: str, source_rule_uid: str,
    ):
        payload = {"commercial_case_uid": commercial_case_uid,
                   "billing_account_uid": billing_account_uid,
                   "items": items, "source_rule_uid": source_rule_uid}

        def operation():
            case = self.repository.require_by_uid("revenue_commercial_cases", commercial_case_uid)
            account = self.repository.require_by_uid("revenue_billing_accounts", billing_account_uid)
            cycle = self.connection.execute(
                "SELECT cycle_type FROM application_cycles WHERE id=?",
                (case["application_cycle_id"],),
            ).fetchone()
            if cycle and cycle["cycle_type"] == "transfer":
                from student_lifecycle.transfer_source_authority import (
                    SourceAuthorityError, schema_ready,
                )
                definition = json.loads(case["classification_evidence_json"] or "{}")
                if definition.get("definition") == "transfer-source-v1":
                    from revenue.transfer_pricing import validate_transfer_fee_items
                    validate_transfer_fee_items(
                        self.connection, case, items, source_rule_uid
                    )
                elif definition.get("definition") == "existing_aaie_k12_v1":
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
                            (case["application_cycle_id"], marker[0]),
                        ).fetchone())
                        old_case = bool(self.connection.execute(
                            "SELECT 1 WHERE julianday(?)<julianday(?)",
                            (case["opened_at"], marker[0]),
                        ).fetchone())
                        projected = self.connection.execute(
                            "SELECT 1 FROM transfer_cases WHERE case_id=?",
                            (case["application_cycle_id"],),
                        ).fetchone()
                        source_seen = self.connection.execute(
                            """SELECT 1 FROM transfer_source_authorities
                                 WHERE application_cycle_id=? OR transfer_contract_id IN
                                   (SELECT id FROM contracts WHERE application_cycle_id=?)""",
                            (case["application_cycle_id"], case["application_cycle_id"]),
                        ).fetchone()
                        later_contract = self.connection.execute(
                            """SELECT 1 FROM contracts WHERE application_cycle_id=?
                                 AND contract_type='TransferApplicationContract'
                                 AND deleted_at IS NULL AND voided_at IS NULL
                                 AND (generated_at IS NULL OR fully_executed_at IS NULL
                                      OR julianday(generated_at) IS NULL
                                      OR julianday(fully_executed_at) IS NULL
                                      OR julianday(generated_at)>=julianday(?)
                                      OR julianday(fully_executed_at)>=julianday(?))""",
                            (case["application_cycle_id"], marker[0], marker[0]),
                        ).fetchone()
                        historical = bool(old_cycle and (old_case or
                            definition.get("transfer_legacy_cutover") == "20260920_041")
                            and not projected and not source_seen and not later_contract)
                    else:
                        projection = self.connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transfer_cases'"
                        ).fetchone()
                        historical = not schema_ready(self.connection) and not projection
                    if not historical:
                        raise SourceAuthorityError('Unverified historical Transfer fee source; HOLD')
                    linked_student = self.connection.execute(
                        """SELECT 1 FROM revenue_billing_account_students
                             WHERE billing_account_id=? AND student_id=?
                               AND effective_to IS NULL""",
                        (account["id"], case["student_id"]),
                    ).fetchone()
                    if not linked_student:
                        raise SourceAuthorityError('Historical Transfer billing account owner mismatch; HOLD')
                    if len(items) != 1 or items[0].get("application_cycle_id") != case["application_cycle_id"]:
                        raise SourceAuthorityError('Historical Transfer fee cycle mismatch; HOLD')
                    item = items[0]
                    if item.get("rule_uid") != source_rule_uid:
                        raise SourceAuthorityError('Historical Transfer fee rule mismatch; HOLD')
                    if item.get("visa_case_id") is not None:
                        owned = self.connection.execute(
                            """SELECT 1 FROM visa_cases WHERE id=? AND student_id=?
                                 AND application_cycle_id=?""",
                            (item["visa_case_id"], case["student_id"], case["application_cycle_id"]),
                        ).fetchone()
                    elif item.get("contract_id") is not None:
                        owned = self.connection.execute(
                            """SELECT 1 FROM contracts WHERE id=? AND student_id=?
                                 AND application_cycle_id=?""",
                            (item["contract_id"], case["student_id"], case["application_cycle_id"]),
                        ).fetchone()
                    else:
                        owned = None
                    if not owned:
                        raise SourceAuthorityError('Historical Transfer fee owner mismatch; HOLD')
                    # Direct service callers must prove the same active DB rule as the
                    # operational event path, not supply a rule UID or amount themselves.
                    from revenue.operational_orchestrator import RevenueOperationalOrchestrator
                    verifier = RevenueOperationalOrchestrator(
                        self.connection, actor_user_id, "Admin"
                    )
                    cycle_fact = verifier.facts.cycle(case["application_cycle_id"])
                    if item.get("contract_id") is not None:
                        contract_fact = verifier.facts.contract(item["contract_id"])
                        facts = verifier._client_fee_facts_for_contract(cycle_fact, case)
                        engagement_date = contract_fact.contract_date or cycle_fact.version[:10]
                    else:
                        visa_fact = verifier.facts.visa_case(item["visa_case_id"])
                        facts = verifier._client_fee_facts_for_visa(cycle_fact, case, visa_fact)
                        active_contracts = verifier._active_fully_executed_contracts(cycle_fact.id)
                        if len(active_contracts) > 1:
                            raise SourceAuthorityError('Ambiguous historical Transfer contracts; HOLD')
                        engagement_date = (str(
                            active_contracts[0]["contract_date"] or
                            active_contracts[0]["fully_executed_at"] or
                            cycle_fact.version
                        )[:10] if active_contracts else str(visa_fact.version or cycle_fact.version)[:10])
                    resolved = verifier.client_fee_rules.resolve(facts, engagement_date)
                    snapshot = resolved.snapshot()
                    if (resolved.net_cents is None or source_rule_uid != resolved.rule_uid
                            or any(item.get(key) != value for key, value in snapshot.items())
                            or item.get("net_cents") != resolved.net_cents
                            or item.get("gst_cents") != 0):
                        raise SourceAuthorityError('Historical Transfer fee DB rule mismatch; HOLD')
                else:
                    raise SourceAuthorityError('Unknown Transfer fee source definition; HOLD')
            snapshot = self._rule_snapshot("fee", source_rule_uid, payload, actor_user_id)
            result = []
            for item in items:
                net = self._cents(item["net_cents"], "net_cents", allow_zero=True)
                gst = self._cents(item["gst_cents"], "gst_cents", allow_zero=True)
                uid = new_uid("fee-item")
                self.connection.execute(
                    """INSERT INTO revenue_fee_items(
                         uid,commercial_case_id,billing_account_id,student_id,rule_snapshot_id,
                         milestone_code,description,net_cents,gst_cents,gross_cents,
                         currency_code,due_date,status,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'unbilled',?)""",
                    (
                        uid, case["id"], account["id"], case["student_id"], snapshot["id"],
                        item["milestone_code"], item["description"], net, gst, net + gst,
                        account["currency_code"], item.get("due_date"), self.repository.now(),
                    ),
                )
                result.append(self.repository.require_by_uid("revenue_fee_items", uid))
            return {"uid": new_uid("fee-batch"), "items": result}

        response = self.repository.command(
            idempotency_key=idempotency_key, command_type="fee_items_created",
            aggregate_type="fee_item_batch", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )
        return response["items"]

    def _next_number(self, document_kind: str, prefix: str, financial_year: int) -> str:
        row = self.connection.execute(
            "SELECT * FROM revenue_number_sequences WHERE document_kind=? AND financial_year=?",
            (document_kind, financial_year),
        ).fetchone()
        if row:
            value = row["next_number"]
            self.connection.execute(
                "UPDATE revenue_number_sequences SET next_number=?,updated_at=? WHERE id=?",
                (value + 1, self.repository.now(), row["id"]),
            )
        else:
            value = 1
            self.connection.execute(
                """INSERT INTO revenue_number_sequences(
                     uid,document_kind,financial_year,next_number,updated_at
                   ) VALUES(?,?,?,?,?)""",
                (new_uid("sequence"), document_kind, financial_year, 2, self.repository.now()),
            )
        return f"{prefix}-{financial_year}-{value:06d}"

    def issue_invoice(
        self, fee_item_uids: list[str], actor_user_id: int, idempotency_key: str,
        issue_date: str, due_date: str | None = None,
        document_kind: str = "client_invoice",
        planned_payment_allocations: list[dict] | None = None,
    ):
        payload = {"fee_item_uids": fee_item_uids, "issue_date": issue_date,
                   "due_date": due_date, "document_kind": document_kind,
                   "planned_payment_allocations": planned_payment_allocations}

        def operation():
            if not fee_item_uids:
                raise ValueError("invoice requires fee items")
            items = [self.repository.require_by_uid("revenue_fee_items", uid) for uid in fee_item_uids]
            if any(item["status"] != "unbilled" for item in items):
                raise ValueError("fee item already invoiced")
            if len({(item["commercial_case_id"], item["billing_account_id"], item["currency_code"])
                    for item in items}) != 1:
                raise ValueError("invoice items must share case, account and currency")
            net = sum(item["net_cents"] for item in items)
            active_payment_rules = self.connection.execute(
                "SELECT COUNT(*) FROM revenue_payment_method_rule_versions WHERE status='active'"
            ).fetchone()[0]
            planned = []
            if document_kind == "client_invoice":
                if not active_payment_rules:
                    raise ValueError("active DB-backed payment method authority is required")
                if not planned_payment_allocations:
                    raise ValueError("an active DB-backed payment method is required")
                allocations = list(planned_payment_allocations or [])
                if len(allocations) == 1 and allocations[0].get("net_cents") is None:
                    allocations[0] = {**allocations[0], "net_cents": net}
                allocated_net = 0
                resolver = DatabaseClientFeeRuleResolver(self.connection)
                for allocation in allocations:
                    method_code = str(allocation.get("method_code") or "").strip()
                    amount = self._cents(
                        allocation.get("net_cents"), "planned net_cents", allow_zero=False
                    )
                    authority = resolver.resolve_payment_method(method_code, issue_date)
                    if int(authority["surcharge_basis_points"]) != 0:
                        raise ValueError("payment-method surcharge is not supported by Invoice V1")
                    planned_gst = apply_basis_points(
                        amount, int(authority["gst_rate_basis_points"])
                    )
                    planned.append({
                        **authority,
                        "planned_net_cents": amount,
                        "planned_gst_cents": planned_gst,
                        "planned_gross_cents": amount + planned_gst,
                    })
                    allocated_net += amount
                if allocated_net != net:
                    raise ValueError("planned payment method allocations must equal invoice net")
                gst = sum(item["planned_gst_cents"] for item in planned)
            else:
                gst = sum(item["gst_cents"] for item in items)
            invoice_uid = new_uid("invoice")
            invoice_number = self._next_number("invoice", "INV", int(issue_date[:4]))
            now = self.repository.now()
            first = items[0]
            self.connection.execute(
                """INSERT INTO revenue_invoices(
                     uid,document_kind,invoice_number,commercial_case_id,billing_account_id,
                     issue_date,due_date,currency_code,net_cents,gst_cents,gross_cents,
                     status,issued_by,issued_at,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'issued',?,?,?)""",
                (
                    invoice_uid, document_kind, invoice_number, first["commercial_case_id"],
                    first["billing_account_id"], issue_date, due_date, first["currency_code"],
                    net, gst, net + gst, actor_user_id, now, now,
                ),
            )
            invoice = self.repository.require_by_uid("revenue_invoices", invoice_uid)
            lines = []
            line_gst = allocate_cents(gst, [item["net_cents"] for item in items]) if gst else [0] * len(items)
            for index, (item, item_gst) in enumerate(zip(items, line_gst)):
                line_uid = new_uid("invoice-line")
                self.connection.execute(
                    """INSERT INTO revenue_invoice_lines(
                         uid,invoice_id,source_kind,source_uid,student_id,description,quantity,
                         net_cents,gst_cents,gross_cents,sort_order
                       ) VALUES(?,?,'fee_item',?,?,?,?,?,?,?,?)""",
                    (
                        line_uid, invoice["id"], item["uid"], item["student_id"],
                        item["description"], 1, item["net_cents"], item_gst,
                        item["net_cents"] + item_gst, index,
                    ),
                )
                self.connection.execute(
                    "UPDATE revenue_fee_items SET status='invoiced' WHERE id=?", (item["id"],)
                )
                lines.append(self.repository.require_by_uid("revenue_invoice_lines", line_uid))
            stored_plans = []
            for plan in planned:
                uid = new_uid("planned-payment")
                self.connection.execute(
                    """INSERT INTO revenue_planned_payment_allocations(
                         uid,invoice_id,payment_method_id,planned_net_cents,
                         planned_gst_cents,planned_gross_cents,created_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        uid,
                        invoice["id"],
                        self.connection.execute(
                            "SELECT id FROM revenue_payment_methods WHERE uid=?",
                            (plan["method_uid"],),
                        ).fetchone()[0],
                        plan["planned_net_cents"],
                        plan["planned_gst_cents"],
                        plan["planned_gross_cents"],
                        now,
                    ),
                )
                stored = self.repository.require_by_uid(
                    "revenue_planned_payment_allocations", uid
                )
                stored_plans.append({
                    **stored,
                    "method_code": plan["method_code"],
                    "payment_method_rule_uid": plan["rule_uid"],
                    "gst_rate_basis_points": plan["gst_rate_basis_points"],
                })
            self.append_balanced_ledger_event(
                "invoiced", "invoice", invoice_uid, net + gst,
                "AccountsReceivable", "ClientRevenue", actor_user_id,
                commercial_case_id=first["commercial_case_id"],
            )
            return {**invoice, "lines": lines,
                    "planned_payment_allocations": stored_plans}

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="invoice_issued",
            aggregate_type="invoice", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def issue_direct_commission_invoice(
        self, claim_uid: str, actor_user_id: int, actor_role: str,
        idempotency_key: str, issue_date: str, due_date: str | None = None,
        actor_has_revenue_access: bool = False,
    ):
        self._require_operator(actor_role, actor_has_revenue_access)
        payload = {"claim_uid": claim_uid, "issue_date": issue_date, "due_date": due_date}

        def operation():
            claim = self.repository.require_by_uid("revenue_claims", claim_uid)
            if claim["status"] != "issued" or claim["claim_kind"] != "direct":
                raise ValueError("issued direct claim is required")
            context = self.connection.execute(
                """SELECT l.commercial_case_id,c.student_id
                     FROM revenue_claim_allocations a
                     JOIN revenue_commission_instalments i ON i.id=a.commission_instalment_id
                     JOIN revenue_commission_lines l ON l.id=i.commission_line_id
                     JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id
                    WHERE a.claim_id=? GROUP BY l.commercial_case_id,c.student_id""",
                (claim["id"],),
            ).fetchall()
            if len(context) != 1:
                raise ValueError("direct claim must resolve to one commercial case")
            uid = new_uid("invoice")
            number = self._next_number("invoice", "INV", int(issue_date[:4]))
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_invoices(
                     uid,document_kind,invoice_number,commercial_case_id,revenue_entity_id,
                     issue_date,due_date,currency_code,net_cents,gst_cents,gross_cents,
                     status,issued_by,issued_at,created_at
                   ) VALUES(?,'direct_commission_invoice',?,?,?,?,?,'AUD',?,?,?,
                            'issued',?,?,?)""",
                (uid, number, context[0]["commercial_case_id"], claim["revenue_entity_id"],
                 issue_date, due_date, claim["net_cents"], claim["gst_cents"],
                 claim["gross_cents"], actor_user_id, now, now),
            )
            invoice = self.repository.require_by_uid("revenue_invoices", uid)
            self.connection.execute(
                """INSERT INTO revenue_invoice_lines(
                     uid,invoice_id,source_kind,source_uid,student_id,description,quantity,
                     net_cents,gst_cents,gross_cents,sort_order
                   ) VALUES(?,?,'claim',?,?,?,1,?,?,?,0)""",
                (new_uid("invoice-line"), invoice["id"], claim_uid,
                 context[0]["student_id"], f"Commission claim {claim['claim_number']}",
                 claim["net_cents"], claim["gst_cents"], claim["gross_cents"]),
            )
            return {**invoice, "lines": [dict(self.connection.execute(
                "SELECT * FROM revenue_invoice_lines WHERE invoice_id=?", (invoice["id"],)
            ).fetchone())]}

        return self.repository.command(
            idempotency_key=idempotency_key,
            command_type="direct_commission_invoice_issued",
            aggregate_type="invoice", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def issue_master_agent_invoice(
        self, statement_item_uid: str, actor_user_id: int, actor_role: str,
        idempotency_key: str, issue_date: str, due_date: str | None = None,
        actor_has_revenue_access: bool = False,
    ):
        self._require_operator(actor_role, actor_has_revenue_access)
        payload = {"statement_item_uid": statement_item_uid,
                   "issue_date": issue_date, "due_date": due_date}

        def operation():
            item = self.repository.require_by_uid(
                "revenue_master_agent_statement_items", statement_item_uid
            )
            context = self.connection.execute(
                """SELECT s.master_agent_entity_id,l.commercial_case_id,c.student_id
                     FROM revenue_master_agent_statements s
                     JOIN revenue_statement_matches m ON m.statement_item_id=?
                     JOIN revenue_commission_lines l ON l.id=m.commission_line_id
                     JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id
                    WHERE s.id=? AND m.match_status='confirmed'""",
                (item["id"], item["statement_id"]),
            ).fetchone()
            if not context:
                raise ValueError("Admin-confirmed statement match is required")
            net, gst = gst_from_inclusive(item["payable_cents"], 1000)
            uid = new_uid("invoice")
            number = self._next_number("invoice", "INV", int(issue_date[:4]))
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_invoices(
                     uid,document_kind,invoice_number,commercial_case_id,revenue_entity_id,
                     issue_date,due_date,currency_code,net_cents,gst_cents,gross_cents,
                     status,issued_by,issued_at,created_at
                   ) VALUES(?,'master_agent_invoice',?,?,?,?,?,'AUD',?,?,?,
                            'issued',?,?,?)""",
                (uid, number, context["commercial_case_id"],
                 context["master_agent_entity_id"], issue_date, due_date,
                 net, gst, item["payable_cents"], actor_user_id, now, now),
            )
            invoice = self.repository.require_by_uid("revenue_invoices", uid)
            self.connection.execute(
                """INSERT INTO revenue_invoice_lines(
                     uid,invoice_id,source_kind,source_uid,student_id,description,quantity,
                     net_cents,gst_cents,gross_cents,sort_order
                   ) VALUES(?,?,'master_agent_statement_item',?,?,?,1,?,?,?,0)""",
                (new_uid("invoice-line"), invoice["id"], statement_item_uid,
                 context["student_id"], "Master Agent commission share",
                 net, gst, item["payable_cents"]),
            )
            return {**invoice, "lines": [dict(self.connection.execute(
                "SELECT * FROM revenue_invoice_lines WHERE invoice_id=?", (invoice["id"],)
            ).fetchone())]}

        return self.repository.command(
            idempotency_key=idempotency_key,
            command_type="master_agent_invoice_issued",
            aggregate_type="invoice", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def record_payment(
        self, amount_cents: int, payment_direction: str, counterparty_kind: str,
        actor_user_id: int, idempotency_key: str, received_date: str,
        billing_account_uid: str | None = None, revenue_entity_uid: str | None = None,
        allocations: list[dict] | None = None, external_reference: str | None = None,
    ):
        amount = self._cents(amount_cents, "amount_cents")
        allocations = allocations or []
        payload = {"amount_cents": amount, "payment_direction": payment_direction,
                   "counterparty_kind": counterparty_kind, "received_date": received_date,
                   "billing_account_uid": billing_account_uid,
                   "revenue_entity_uid": revenue_entity_uid,
                   "allocations": allocations, "external_reference": external_reference}

        def operation():
            allocation_total = sum(self._cents(item["allocated_cents"], "allocated_cents")
                                   for item in allocations)
            if allocation_total > amount:
                raise ValueError("allocation exceeds payment")
            account = self.repository.require_by_uid("revenue_billing_accounts", billing_account_uid) if billing_account_uid else None
            entity = self.repository.require_by_uid("revenue_entities", revenue_entity_uid) if revenue_entity_uid else None
            resolved = []
            case_id = None
            for allocation in allocations:
                line = self.repository.require_by_uid("revenue_invoice_lines", allocation["invoice_line_uid"])
                prior = self.connection.execute(
                    "SELECT COALESCE(SUM(allocated_cents),0) FROM revenue_payment_allocations WHERE invoice_line_id=?",
                    (line["id"],),
                ).fetchone()[0]
                value = allocation["allocated_cents"]
                if prior + value > line["gross_cents"]:
                    raise ValueError("allocation exceeds invoice line outstanding amount")
                invoice = self.connection.execute(
                    """SELECT commercial_case_id,billing_account_id,document_kind,status
                         FROM revenue_invoices WHERE id=?""",
                    (line["invoice_id"],),
                ).fetchone()
                if counterparty_kind == "client":
                    if account is None:
                        raise ValueError("client payment requires billing account")
                    if (
                        invoice[1] != account["id"]
                        or invoice[2] != "client_invoice"
                        or invoice[3] != "issued"
                    ):
                        raise ValueError(
                            "invoice is not an issued client invoice for this billing account"
                        )
                case_id = case_id or invoice[0]
                resolved.append((line, value))
            uid = new_uid("payment")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_payments(
                     uid,payment_direction,counterparty_kind,billing_account_id,revenue_entity_id,
                     external_reference,received_date,amount_cents,allocated_cents,currency_code,
                     status,confirmed_by,confirmed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,'AUD','confirmed',?,?)""",
                (
                    uid, payment_direction, counterparty_kind,
                    account["id"] if account else None, entity["id"] if entity else None,
                    external_reference, received_date, amount, allocation_total,
                    actor_user_id, now,
                ),
            )
            payment = self.repository.require_by_uid("revenue_payments", uid)
            for line, value in resolved:
                self.connection.execute(
                    """INSERT INTO revenue_payment_allocations(
                         uid,payment_id,invoice_line_id,allocated_cents,confirmed_by,confirmed_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (new_uid("allocation"), payment["id"], line["id"], value, actor_user_id, now),
                )
            self.append_balanced_ledger_event(
                "received" if payment_direction == "received" else "paid",
                "payment", uid, amount,
                "Bank" if payment_direction == "received" else "Payable",
                "AccountsReceivable" if payment_direction == "received" else "Bank",
                actor_user_id, commercial_case_id=case_id,
            )
            return payment

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="payment_recorded",
            aggregate_type="payment", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def reconcile_direct_commission_payment(
        self, commission_line_uid: str, amount_cents: int,
        institution_entity_uid: str, received_date: str, actor_user_id: int,
        actor_role: str, idempotency_key: str,
        actor_has_revenue_access: bool = False,
    ):
        self._require_operator(actor_role, actor_has_revenue_access)
        amount = self._cents(amount_cents, "amount_cents")
        payload = {"commission_line_uid": commission_line_uid,
                   "amount_cents": amount,
                   "institution_entity_uid": institution_entity_uid,
                   "received_date": received_date}

        def operation():
            line = self.repository.require_by_uid(
                "revenue_commission_lines", commission_line_uid
            )
            entity = self.repository.require_by_uid(
                "revenue_entities", institution_entity_uid
            )
            if entity["id"] != line["revenue_entity_id"]:
                raise ValueError("payment institution does not own commission line")
            claimed = self.connection.execute(
                """SELECT COALESCE(SUM(a.claimed_cents),0)
                     FROM revenue_claim_allocations a
                     JOIN revenue_commission_instalments i ON i.id=a.commission_instalment_id
                    WHERE i.commission_line_id=?""",
                (line["id"],),
            ).fetchone()[0]
            allocated = self.connection.execute(
                "SELECT COALESCE(SUM(allocated_cents),0) FROM revenue_payment_allocations WHERE commission_line_id=?",
                (line["id"],),
            ).fetchone()[0]
            if allocated + amount > claimed:
                raise ValueError("direct commission payment exceeds claimed amount")
            uid = new_uid("payment")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_payments(
                     uid,payment_direction,counterparty_kind,revenue_entity_id,received_date,
                     amount_cents,allocated_cents,currency_code,status,confirmed_by,confirmed_at
                   ) VALUES(?,'received','institution',?,?,?,?,'AUD','confirmed',?,?)""",
                (uid, entity["id"], received_date, amount, amount, actor_user_id, now),
            )
            payment = self.repository.require_by_uid("revenue_payments", uid)
            self.connection.execute(
                """INSERT INTO revenue_payment_allocations(
                     uid,payment_id,commission_line_id,allocated_cents,confirmed_by,confirmed_at
                   ) VALUES(?,?,?,?,?,?)""",
                (new_uid("allocation"), payment["id"], line["id"], amount,
                 actor_user_id, now),
            )
            if allocated + amount == claimed:
                self.connection.execute(
                    """UPDATE revenue_commission_instalments SET status='paid'
                        WHERE commission_line_id=? AND claimed_cents>0""",
                    (line["id"],),
                )
                self.connection.execute(
                    "UPDATE revenue_commission_lines SET status='paid' WHERE id=?",
                    (line["id"],),
                )
            self.append_balanced_ledger_event(
                "received", "direct_commission_payment", uid, amount, "Bank",
                "CommissionReceivable", actor_user_id, line["commercial_case_id"],
            )
            return payment

        return self.repository.command(
            idempotency_key=idempotency_key,
            command_type="direct_commission_payment_reconciled",
            aggregate_type="payment", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def register_document(
        self, document_kind: str, storage_path: str, sha256: str, mime_type: str,
        size_bytes: int, source_kind: str, actor_user_id: int, idempotency_key: str,
    ):
        payload = {"document_kind": document_kind, "storage_path": storage_path,
                   "sha256": sha256, "mime_type": mime_type,
                   "size_bytes": size_bytes, "source_kind": source_kind}

        def operation():
            uid = new_uid("document")
            self.connection.execute(
                """INSERT INTO revenue_documents(
                     uid,document_kind,storage_path,sha256,mime_type,size_bytes,
                     source_kind,created_by,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (uid, document_kind, storage_path, sha256, mime_type, size_bytes,
                 source_kind, actor_user_id, self.repository.now()),
            )
            return self.repository.require_by_uid("revenue_documents", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="document_registered",
            aggregate_type="document", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def issue_receipt(
        self, payment_uid: str, amount_cents: int, document_uid: str,
        actor_user_id: int, idempotency_key: str,
    ):
        amount = self._cents(amount_cents, "amount_cents")
        payload = {"payment_uid": payment_uid, "amount_cents": amount,
                   "document_uid": document_uid}

        def operation():
            payment = self.repository.require_by_uid("revenue_payments", payment_uid)
            document = self.repository.require_by_uid("revenue_documents", document_uid)
            if amount > payment["amount_cents"]:
                raise ValueError("receipt exceeds payment")
            uid = new_uid("receipt")
            number = self._next_number("receipt", "RCT", int(payment["received_date"][:4]))
            self.connection.execute(
                """INSERT INTO revenue_receipts(
                     uid,receipt_number,payment_id,amount_cents,currency_code,document_id,
                     issued_by,issued_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (uid, number, payment["id"], amount, payment["currency_code"],
                 document["id"], actor_user_id, self.repository.now()),
            )
            return self.repository.require_by_uid("revenue_receipts", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="receipt_issued",
            aggregate_type="receipt", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def record_refund(
        self, payment_uid: str, amount_cents: int, reason: str,
        actor_user_id: int, idempotency_key: str,
    ):
        amount = self._cents(amount_cents, "amount_cents")
        payload = {"payment_uid": payment_uid, "amount_cents": amount, "reason": reason}

        def operation():
            payment = self.repository.require_by_uid("revenue_payments", payment_uid)
            refunded = self.connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) FROM revenue_refunds WHERE payment_uid=? AND status IN ('approved','paid')",
                (payment_uid,),
            ).fetchone()[0]
            if refunded + amount > payment["amount_cents"]:
                raise ValueError("refund exceeds payment")
            uid = new_uid("refund")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_refunds(
                     uid,payment_uid,amount_cents,reason,status,approved_by,approved_at,created_at
                   ) VALUES(?,?,?,?,'approved',?,?,?)""",
                (uid, payment_uid, amount, reason, actor_user_id, now, now),
            )
            self.append_balanced_ledger_event(
                "refund", "refund", uid, amount, "ClientRefund", "Bank",
                actor_user_id, commercial_case_id=None,
            )
            return self.repository.require_by_uid("revenue_refunds", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="refund_approved",
            aggregate_type="refund", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def append_balanced_ledger_event(
        self, event_type: str, source_kind: str, source_uid: str,
        amount_cents: int, debit_account: str, credit_account: str,
        actor_user_id: int, commercial_case_id: int | None,
    ):
        amount = self._cents(amount_cents, "amount_cents")
        now = self.repository.now()
        for account, debit, credit in (
            (debit_account, amount, 0), (credit_account, 0, amount)
        ):
            self.connection.execute(
                """INSERT INTO revenue_ledger_entries(
                     uid,account_code,event_type,source_kind,source_uid,commercial_case_id,
                     debit_cents,credit_cents,currency_code,occurred_at,created_by
                   ) VALUES(?,?,?,?,?,?,?,?,'AUD',?,?)""",
                (new_uid("ledger"), account, event_type, source_kind, source_uid,
                 commercial_case_id, debit, credit, now, actor_user_id),
            )

    @staticmethod
    def _cents(value: int, label: str, allow_zero: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label} must be integer cents")
        if value < 0 or (value == 0 and not allow_zero):
            raise ValueError(f"{label} must be {'non-negative' if allow_zero else 'positive'}")
        return value

    @staticmethod
    def _require_admin(actor_role: str):
        if actor_role != "Admin":
            raise PermissionError("Revenue administration requires exact Admin role")

    @staticmethod
    def _require_operator(actor_role: str, actor_has_revenue_access: bool = False):
        if not can_operate_revenue(actor_role, actor_has_revenue_access):
            raise PermissionError("Revenue module access is required")

    def create_revenue_entity(
        self, entity_type: str, legal_name: str, actor_user_id: int,
        idempotency_key: str, trading_name: str | None = None,
        abn: str | None = None,
    ):
        payload = {"entity_type": entity_type, "legal_name": legal_name,
                   "trading_name": trading_name, "abn": abn}

        def operation():
            uid = new_uid("entity")
            self.connection.execute(
                """INSERT INTO revenue_entities(
                     uid,entity_type,legal_name,trading_name,abn,currency_code,status,
                     created_by,created_at
                   ) VALUES(?,?,?,?,?,'AUD','active',?,?)""",
                (uid, entity_type, legal_name, trading_name, abn,
                 actor_user_id, self.repository.now()),
            )
            return self.repository.require_by_uid("revenue_entities", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="revenue_entity_created",
            aggregate_type="revenue_entity", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def create_contract_version(
        self, revenue_entity_uid: str, document_uid: str, contract_kind: str,
        effective_from: str, actor_user_id: int, actor_role: str,
        idempotency_key: str, reference_code: str | None = None,
        actor_has_revenue_access: bool = False,
    ):
        self._require_operator(actor_role, actor_has_revenue_access)
        payload = {"revenue_entity_uid": revenue_entity_uid, "document_uid": document_uid,
                   "contract_kind": contract_kind, "effective_from": effective_from,
                   "reference_code": reference_code}

        def operation():
            entity = self.repository.require_by_uid("revenue_entities", revenue_entity_uid)
            document = self.repository.require_by_uid("revenue_documents", document_uid)
            group_uid = new_uid("contract-group")
            self.connection.execute(
                """INSERT INTO revenue_contract_groups(
                     uid,revenue_entity_id,contract_kind,reference_code,created_by,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (group_uid, entity["id"], contract_kind, reference_code,
                 actor_user_id, self.repository.now()),
            )
            group = self.repository.require_by_uid("revenue_contract_groups", group_uid)
            uid = new_uid("contract")
            self.connection.execute(
                """INSERT INTO revenue_contract_versions(
                     uid,contract_group_id,version_no,document_id,effective_from,status,created_at
                   ) VALUES(?,?,1,?,?,'draft',?)""",
                (uid, group["id"], document["id"], effective_from, self.repository.now()),
            )
            return self.repository.require_by_uid("revenue_contract_versions", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="contract_version_created",
            aggregate_type="contract_version", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def create_parse_run(
        self, contract_version_uid: str, provider, actor_user_id: int,
        actor_role: str, idempotency_key: str,
        actor_has_revenue_access: bool = False,
    ):
        self._require_operator(actor_role, actor_has_revenue_access)
        payload = {"contract_version_uid": contract_version_uid,
                   "provider_code": provider.provider_code,
                   "provider_model": provider.provider_model}

        def operation():
            contract = self.repository.require_by_uid(
                "revenue_contract_versions", contract_version_uid
            )
            if contract["status"] not in ("draft", "under_review"):
                raise ValueError("contract parsing requires draft or under review status")
            if contract["status"] == "draft":
                self.connection.execute(
                    "UPDATE revenue_contract_versions SET status='under_review' WHERE id=? AND status='draft'",
                    (contract["id"],),
                )
            document = self.connection.execute(
                "SELECT * FROM revenue_documents WHERE id=?", (contract["document_id"],)
            ).fetchone()
            uid = new_uid("parse-run")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_contract_parse_runs(
                     uid,contract_version_id,provider_code,provider_model,status,
                     started_at,created_by
                   ) VALUES(?,?,?,?, 'pending',?,?)""",
                (uid, contract["id"], provider.provider_code, provider.provider_model,
                 now, actor_user_id),
            )
            try:
                suggestions = provider.suggest(dict(document))
            except ProviderUnavailable:
                self.connection.execute(
                    """UPDATE revenue_contract_parse_runs
                       SET status='failed',error_code='PROVIDER_UNAVAILABLE',completed_at=?
                       WHERE uid=?""",
                    (now, uid),
                )
                result = self.repository.require_by_uid("revenue_contract_parse_runs", uid)
                return {**result, "extractions": []}
            extraction_rows = []
            for suggestion in suggestions:
                confidence = suggestion.confidence_basis_points
                if confidence is not None and not 0 <= int(confidence) <= 10000:
                    raise ValueError("confidence_basis_points out of range")
                extraction_uid = new_uid("extraction")
                self.connection.execute(
                    """INSERT INTO revenue_contract_extractions(
                         uid,parse_run_id,field_name,suggested_value,page_number,
                         clause_reference,evidence_text,confidence_basis_points,review_status
                       ) VALUES(?,?,?,?,?,?,?,?,'pending')""",
                    (
                        extraction_uid,
                        self.repository.require_by_uid("revenue_contract_parse_runs", uid)["id"],
                        suggestion.field_name, suggestion.suggested_value,
                        suggestion.page_number, suggestion.clause_reference,
                        suggestion.evidence_text, confidence,
                    ),
                )
                extraction_rows.append(self.repository.require_by_uid(
                    "revenue_contract_extractions", extraction_uid
                ))
            self.connection.execute(
                "UPDATE revenue_contract_parse_runs SET status='suggested',completed_at=? WHERE uid=?",
                (now, uid),
            )
            result = self.repository.require_by_uid("revenue_contract_parse_runs", uid)
            return {**result, "extractions": extraction_rows}

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="contract_parse_requested",
            aggregate_type="contract_parse_run", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def review_extraction(
        self, extraction_uid: str, confirmed_value: str | None, accepted: bool,
        actor_user_id: int, actor_role: str, idempotency_key: str,
    ):
        self._require_admin(actor_role)
        payload = {"extraction_uid": extraction_uid, "confirmed_value": confirmed_value,
                   "accepted": bool(accepted)}

        def operation():
            extraction = self.repository.require_by_uid(
                "revenue_contract_extractions", extraction_uid
            )
            lifecycle = self.connection.execute(
                """SELECT r.status AS parse_status,v.status AS contract_status
                     FROM revenue_contract_parse_runs r
                     JOIN revenue_contract_versions v ON v.id=r.contract_version_id
                    WHERE r.id=?""",
                (extraction["parse_run_id"],),
            ).fetchone()
            if extraction["review_status"] != "pending":
                raise ValueError("only pending contract extraction can be reviewed")
            if lifecycle is None or lifecycle["parse_status"] != "suggested":
                raise ValueError("contract extraction requires suggested parse run")
            if lifecycle["contract_status"] != "under_review":
                raise ValueError("contract extraction requires under review contract")
            status = "confirmed" if accepted else "rejected"
            if accepted and confirmed_value is None:
                raise ValueError("accepted extraction requires confirmed value")
            self.connection.execute(
                """UPDATE revenue_contract_extractions
                   SET review_status=?,confirmed_value=?,reviewed_by=?,reviewed_at=?
                   WHERE id=?""",
                (status, confirmed_value, actor_user_id, self.repository.now(), extraction["id"]),
            )
            pending = self.connection.execute(
                "SELECT COUNT(*) FROM revenue_contract_extractions WHERE parse_run_id=? AND review_status='pending'",
                (extraction["parse_run_id"],),
            ).fetchone()[0]
            if pending == 0:
                self.connection.execute(
                    "UPDATE revenue_contract_parse_runs SET status='reviewed' WHERE id=?",
                    (extraction["parse_run_id"],),
                )
            return self.repository.require_by_uid("revenue_contract_extractions", extraction_uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="contract_extraction_reviewed",
            aggregate_type="contract_extraction", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def activate_contract_version(
        self, contract_version_uid: str, actor_user_id: int, actor_role: str,
        idempotency_key: str,
    ):
        self._require_admin(actor_role)
        payload = {"contract_version_uid": contract_version_uid}

        def operation():
            contract = self.repository.require_by_uid(
                "revenue_contract_versions", contract_version_uid
            )
            if contract["status"] != "under_review":
                raise ValueError("contract activation requires under review status")
            extractions = self.connection.execute(
                """SELECT e.* FROM revenue_contract_extractions e
                   JOIN revenue_contract_parse_runs r ON r.id=e.parse_run_id
                  WHERE r.contract_version_id=?""",
                (contract["id"],),
            ).fetchall()
            monetary = [row for row in extractions if row["field_name"].endswith(
                ("_cents", "_basis_points")
            )]
            if not monetary or any(
                row["review_status"] not in ("confirmed", "corrected")
                or row["confirmed_value"] in (None, "") for row in monetary
            ):
                raise ValueError("all monetary and rate extractions must be Admin-confirmed")
            now = self.repository.now()
            self.connection.execute(
                """UPDATE revenue_contract_versions
                   SET status='active',activated_by=?,activated_at=? WHERE id=? AND status='under_review'""",
                (actor_user_id, now, contract["id"]),
            )
            return self.repository.require_by_uid("revenue_contract_versions", contract_version_uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="contract_version_activated",
            aggregate_type="contract_version", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def activate_commission_rule(
        self, contract_version_uid: str, configuration: dict,
        actor_user_id: int, actor_role: str, idempotency_key: str,
    ):
        self._require_admin(actor_role)
        payload = {"contract_version_uid": contract_version_uid,
                   "configuration": configuration}

        def operation():
            contract = self.repository.require_by_uid(
                "revenue_contract_versions", contract_version_uid
            )
            if contract["status"] != "active":
                raise ValueError("commission rule requires active Admin-reviewed contract")
            if not configuration.get("instalments"):
                raise ValueError("commission rule requires instalments")
            snapshot = self._rule_snapshot(
                "commission", contract_version_uid, configuration, actor_user_id
            )
            uid = new_uid("commission-rule")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_commission_rule_versions(
                     uid,contract_version_id,version_no,effective_from,priority,status,
                     rule_snapshot_id,activated_by,activated_at,created_at
                   ) VALUES(?,?,1,?,100,'active',?,?,?,?)""",
                (uid, contract["id"], contract["effective_from"], snapshot["id"],
                 actor_user_id, now, now),
            )
            rule = self.repository.require_by_uid("revenue_commission_rule_versions", uid)
            scopes = configuration.get("scopes") or [
                {"scope_kind": "all", "scope_value": None, "priority": 100}
            ]
            for scope in scopes:
                self.connection.execute(
                    """INSERT INTO revenue_commission_rule_scopes(
                         uid,commission_rule_version_id,scope_kind,scope_value,priority,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (new_uid("scope"), rule["id"], scope["scope_kind"],
                     scope.get("scope_value"), int(scope.get("priority", 100)), now),
                )
            for fee_component in configuration.get("fee_components", ["tuition"]):
                self.connection.execute(
                    """INSERT INTO revenue_commission_rule_fee_components(
                         uid,commission_rule_version_id,fee_component,treatment,created_at
                       ) VALUES(?,?,?,'included',?)""",
                    (new_uid("fee-component"), rule["id"], fee_component, now),
                )
            for instalment in configuration["instalments"]:
                self.connection.execute(
                    """INSERT INTO revenue_commission_rate_instalments(
                         uid,commission_rule_version_id,instalment_no,academic_period,
                         rate_basis_points,fixed_cents,created_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (new_uid("rate-instalment"), rule["id"], instalment["instalment_no"],
                     instalment["academic_period"], instalment["rate_basis_points"],
                     instalment.get("fixed_cents"), now),
                )
            for requirement in configuration.get("document_requirements", []):
                if isinstance(requirement, str):
                    document_kind, required = requirement, True
                else:
                    document_kind = requirement["document_kind"]
                    required = bool(requirement.get("required", True))
                self.connection.execute(
                    """INSERT INTO revenue_claim_document_requirements(
                         uid,commission_rule_version_id,document_kind,required,created_at
                       ) VALUES(?,?,?,?,?)""",
                    (new_uid("claim-requirement"), rule["id"], document_kind,
                     int(required), now),
                )
            return {**rule, "configuration": configuration}

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="commission_rule_activated",
            aggregate_type="commission_rule", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def calculate_commission(
        self, commercial_case_uid: str, component_uid: str, revenue_entity_uid: str,
        commission_rule_uid: str, actor_user_id: int, actor_role: str,
        idempotency_key: str,
    ):
        self._require_admin(actor_role)
        payload = {"commercial_case_uid": commercial_case_uid,
                   "component_uid": component_uid,
                   "revenue_entity_uid": revenue_entity_uid,
                   "commission_rule_uid": commission_rule_uid}

        def operation():
            case = self.repository.require_by_uid("revenue_commercial_cases", commercial_case_uid)
            component = self.repository.require_by_uid("revenue_enrolment_components", component_uid)
            entity = self.repository.require_by_uid("revenue_entities", revenue_entity_uid)
            rule = self.repository.require_by_uid("revenue_commission_rule_versions", commission_rule_uid)
            if rule["status"] != "active":
                raise ValueError("commission rule is not active")
            snapshot = self.connection.execute(
                "SELECT * FROM revenue_rule_snapshots WHERE id=?", (rule["rule_snapshot_id"],)
            ).fetchone()
            configuration = json.loads(snapshot["canonical_json"])
            included = set(configuration.get("fee_components", ["tuition"]))
            tuition_rows = self.connection.execute(
                """SELECT * FROM revenue_tuition_facts
                   WHERE enrolment_component_id=? AND status='active'""",
                (component["id"],),
            ).fetchall()
            commissionable = sum(
                row["tuition_cents"] for row in tuition_rows if row["fee_component"] in included
            )
            reducing_adjustments = self.connection.execute(
                """SELECT COALESCE(SUM(a.amount_cents),0)
                   FROM revenue_tuition_adjustments a
                   JOIN revenue_tuition_facts t ON t.id=a.tuition_fact_id
                  WHERE t.enrolment_component_id=? AND t.status='active'
                    AND a.commission_effect='reduces_base'""",
                (component["id"],),
            ).fetchone()[0]
            commissionable = max(0, commissionable - reducing_adjustments)
            rate_terms = self.connection.execute(
                """SELECT instalment_no,academic_period,rate_basis_points,fixed_cents
                   FROM revenue_commission_rate_instalments
                  WHERE commission_rule_version_id=? ORDER BY instalment_no""",
                (rule["id"],),
            ).fetchall()
            calculated = CommissionRuleEvaluator().calculate(
                commissionable, [dict(row) for row in rate_terms]
            )
            uid = new_uid("commission-line")
            net = sum(item["net_cents"] for item in calculated)
            gst = sum(item["gst_cents"] for item in calculated)
            self.connection.execute(
                """INSERT INTO revenue_commission_lines(
                     uid,commercial_case_id,enrolment_component_id,revenue_entity_id,
                     commission_rule_version_id,rule_snapshot_id,commissionable_cents,
                     net_cents,gst_cents,gross_cents,currency_code,status,calculated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,'AUD','accrued',?)""",
                (uid, case["id"], component["id"], entity["id"], rule["id"],
                 snapshot["id"], commissionable, net, gst, net + gst, self.repository.now()),
            )
            line = self.repository.require_by_uid("revenue_commission_lines", uid)
            instalments = []
            for item in calculated:
                instalment_uid = new_uid("commission-instalment")
                self.connection.execute(
                    """INSERT INTO revenue_commission_instalments(
                         uid,commission_line_id,instalment_no,academic_period,net_cents,
                         gst_cents,gross_cents,eligible_cents,claimed_cents,status,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,0,'eligible',?)""",
                    (instalment_uid, line["id"], item["instalment_no"],
                     item["academic_period"], item["net_cents"], item["gst_cents"],
                     item["gross_cents"], item["gross_cents"], self.repository.now()),
                )
                instalments.append(self.repository.require_by_uid(
                    "revenue_commission_instalments", instalment_uid
                ))
            return {**line, "instalments": instalments}

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="commission_calculated",
            aggregate_type="commission_line", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def calculate_zero_commission(
        self, commercial_case_uid: str, component_uid: str,
        revenue_entity_uid: str, commission_rule_uid: str, reason_code: str,
        actor_user_id: int, actor_role: str, idempotency_key: str,
    ):
        self._require_admin(actor_role)
        payload = {
            "commercial_case_uid": commercial_case_uid,
            "component_uid": component_uid,
            "revenue_entity_uid": revenue_entity_uid,
            "commission_rule_uid": commission_rule_uid,
            "reason_code": reason_code,
        }

        def operation():
            case = self.repository.require_by_uid("revenue_commercial_cases", commercial_case_uid)
            component = self.repository.require_by_uid("revenue_enrolment_components", component_uid)
            entity = self.repository.require_by_uid("revenue_entities", revenue_entity_uid)
            rule = self.repository.require_by_uid("revenue_commission_rule_versions", commission_rule_uid)
            uid = new_uid("commission-line")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_commission_lines(
                     uid,commercial_case_id,enrolment_component_id,revenue_entity_id,
                     commission_rule_version_id,rule_snapshot_id,commissionable_cents,
                     net_cents,gst_cents,gross_cents,currency_code,status,calculated_at
                   ) VALUES(?,?,?,?,?,?,0,0,0,0,'AUD','accrued',?)""",
                (uid, case["id"], component["id"], entity["id"], rule["id"],
                 rule["rule_snapshot_id"], now),
            )
            line = self.repository.require_by_uid("revenue_commission_lines", uid)
            terms = self.connection.execute(
                """SELECT instalment_no,academic_period
                     FROM revenue_commission_rate_instalments
                    WHERE commission_rule_version_id=? ORDER BY instalment_no""",
                (rule["id"],),
            ).fetchall()
            instalments = []
            for term in terms:
                instalment_uid = new_uid("commission-instalment")
                self.connection.execute(
                    """INSERT INTO revenue_commission_instalments(
                         uid,commission_line_id,instalment_no,academic_period,net_cents,
                         gst_cents,gross_cents,eligible_cents,claimed_cents,status,created_at
                       ) VALUES(?,?,?,?,0,0,0,0,0,'eligible',?)""",
                    (instalment_uid, line["id"], term["instalment_no"],
                     term["academic_period"], now),
                )
                instalments.append(self.repository.require_by_uid(
                    "revenue_commission_instalments", instalment_uid
                ))
            return {**line, "instalments": instalments, "reason_code": reason_code}

        return self.repository.command(
            idempotency_key=idempotency_key,
            command_type="zero_commission_calculated",
            aggregate_type="commission_line",
            payload=payload,
            actor_user_id=actor_user_id,
            operation=operation,
        )

    def evaluate_claim_eligibility(self, commission_line_uid: str, facts: dict):
        line = self.repository.require_by_uid("revenue_commission_lines", commission_line_uid)
        snapshot = self.connection.execute(
            "SELECT canonical_json FROM revenue_rule_snapshots WHERE id=?",
            (line["rule_snapshot_id"],),
        ).fetchone()
        configuration = json.loads(snapshot[0])
        trigger = configuration.get("claim_trigger")
        eligible = True if not trigger else CommissionRuleEvaluator().evaluate_trigger(trigger, facts)
        return {"commission_line_uid": commission_line_uid, "eligible": eligible,
                "evaluated_facts": facts}

    def issue_claim(
        self, commission_line_uid: str, allocations: list[dict], claim_date: str,
        actor_user_id: int, actor_role: str, idempotency_key: str,
        claim_kind: str = "direct",
        actor_has_revenue_access: bool = False,
    ):
        self._require_operator(actor_role, actor_has_revenue_access)
        payload = {"commission_line_uid": commission_line_uid,
                   "allocations": allocations, "claim_date": claim_date,
                   "claim_kind": claim_kind}

        def operation():
            line = self.repository.require_by_uid("revenue_commission_lines", commission_line_uid)
            decision = ClaimEligibilityResolver(self.connection).evaluate(
                commission_line_uid, claim_date
            )
            if not decision.eligible:
                self._open_claim_not_ready_exception(
                    commission_line_uid, decision.reasons
                )
                return {
                    "aggregate_uid": commission_line_uid,
                    "commission_line_uid": commission_line_uid,
                    **decision.public_dict(),
                }
            resolved = []
            gross = 0
            for requested in allocations:
                instalment = self.connection.execute(
                    """SELECT * FROM revenue_commission_instalments
                       WHERE commission_line_id=? AND instalment_no=?""",
                    (line["id"], requested["instalment_no"]),
                ).fetchone()
                if not instalment:
                    raise ValueError("commission instalment not found")
                value = self._cents(requested["claimed_cents"], "claimed_cents")
                if instalment["claimed_cents"] + value > instalment["eligible_cents"]:
                    raise ValueError("claim exceeds eligible amount")
                gross += value
                resolved.append((instalment, value))
            net, gst = gst_from_inclusive(gross, 1000)
            uid = new_uid("claim")
            now = self.repository.now()
            number = self._next_number("claim", "CLM", int(claim_date[:4]))
            self.connection.execute(
                """INSERT INTO revenue_claims(
                     uid,claim_number,claim_kind,revenue_entity_id,claim_date,net_cents,
                     gst_cents,gross_cents,status,issued_by,issued_at,created_at
                   ) VALUES(?,?,?,?,?,?,?,?, 'issued',?,?,?)""",
                (uid, number, claim_kind, line["revenue_entity_id"], claim_date,
                 net, gst, gross, actor_user_id, now, now),
            )
            claim = self.repository.require_by_uid("revenue_claims", uid)
            for instalment, value in resolved:
                self.connection.execute(
                    """INSERT INTO revenue_claim_allocations(
                         uid,claim_id,commission_instalment_id,claimed_cents,created_at
                       ) VALUES(?,?,?,?,?)""",
                    (new_uid("claim-allocation"), claim["id"], instalment["id"],
                     value, now),
                )
                claimed = instalment["claimed_cents"] + value
                status = "claimed" if claimed == instalment["gross_cents"] else "partial"
                self.connection.execute(
                    "UPDATE revenue_commission_instalments SET claimed_cents=?,status=? WHERE id=?",
                    (claimed, status, instalment["id"]),
                )
            self.append_balanced_ledger_event(
                "invoiced", "claim", uid, gross, "CommissionReceivable",
                "CommissionRevenue", actor_user_id,
                commercial_case_id=line["commercial_case_id"],
            )
            facts_json = canonical_json(decision.facts)
            self.connection.execute(
                """INSERT INTO revenue_claim_eligibility_evaluations(
                     uid,claim_id,commission_line_id,rule_snapshot_id,evaluation_version,
                     result_code,eligible,evaluator_source,authoritative_facts_json,
                     authoritative_facts_sha256,evaluated_by,evaluated_at,created_at
                   ) VALUES(?,?,?,?,1,'eligible',1,'system',?,?,?,?,?)""",
                (new_uid("claim-evaluation"), claim["id"], line["id"],
                 line["rule_snapshot_id"], facts_json,
                 hashlib.sha256(facts_json.encode("utf-8")).hexdigest(),
                 actor_user_id, now, now),
            )
            for evidence in decision.evidence:
                document = self._ensure_imported_evidence_document(evidence, actor_user_id)
                self.connection.execute(
                    """INSERT INTO revenue_claim_evidence_links(
                         uid,claim_id,claim_document_requirement_id,revenue_document_id,
                         student_document_id,source_kind,source_reference,
                         verification_status,verified_by,verified_at,created_at
                       ) VALUES(?,?,?,?,?,'student_document',?,'verified',?,?,?)""",
                    (new_uid("claim-evidence"), claim["id"], evidence.requirement_id,
                     document["id"], evidence.student_document_id,
                     evidence.source_reference, actor_user_id, now, now),
                )
            self._resolve_claim_not_ready_exception(commission_line_uid, actor_user_id)
            return {**claim, **decision.public_dict()}

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="commission_claim_issued",
            aggregate_type="claim", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def _open_claim_not_ready_exception(self, commission_line_uid, reasons):
        message = "; ".join(item["code"] for item in reasons)
        prior = self.connection.execute(
            """SELECT id FROM revenue_exceptions
                WHERE exception_code='claim_not_ready'
                  AND aggregate_type='commission_line' AND aggregate_uid=?
                  AND status='open'""",
            (commission_line_uid,),
        ).fetchone()
        if prior:
            self.connection.execute(
                "UPDATE revenue_exceptions SET message=? WHERE id=?",
                (message, prior["id"]),
            )
            return
        self.connection.execute(
            """INSERT INTO revenue_exceptions(
                 uid,exception_code,severity,aggregate_type,aggregate_uid,message,
                 status,opened_at
               ) VALUES(?,'claim_not_ready','blocking','commission_line',?,?,'open',?)""",
            (new_uid("exception"), commission_line_uid, message, self.repository.now()),
        )

    def _resolve_claim_not_ready_exception(self, commission_line_uid, actor_user_id):
        self.connection.execute(
            """UPDATE revenue_exceptions
                  SET status='resolved',resolved_at=?,resolved_by=?,
                      resolution_note='Claim eligibility hard gate passed.'
                WHERE exception_code='claim_not_ready'
                  AND aggregate_type='commission_line' AND aggregate_uid=?
                  AND status='open'""",
            (self.repository.now(), actor_user_id, commission_line_uid),
        )

    def _ensure_imported_evidence_document(self, evidence, actor_user_id):
        prior = self.connection.execute(
            """SELECT * FROM revenue_documents
                WHERE document_kind=? AND storage_path=? AND sha256=?
                ORDER BY version_no DESC LIMIT 1""",
            (evidence.document_kind, evidence.storage_path, evidence.sha256),
        ).fetchone()
        if prior:
            return dict(prior)
        version_no = self.connection.execute(
            """SELECT COALESCE(MAX(version_no),0)+1 FROM revenue_documents
                WHERE document_kind=? AND storage_path=?""",
            (evidence.document_kind, evidence.storage_path),
        ).fetchone()[0]
        uid = new_uid("document")
        self.connection.execute(
            """INSERT INTO revenue_documents(
                 uid,document_kind,version_no,storage_path,sha256,mime_type,
                 size_bytes,source_kind,created_by,created_at
               ) VALUES(?,?,?,?,?,?,?,'imported',?,?)""",
            (uid, evidence.document_kind, version_no, evidence.storage_path,
             evidence.sha256, evidence.mime_type, evidence.size_bytes,
             actor_user_id, self.repository.now()),
        )
        return self.repository.require_by_uid("revenue_documents", uid)

    def record_commission_adjustment(
        self, commission_line_uid: str, adjustment_kind: str, amount_cents: int,
        reason: str, actor_user_id: int, actor_role: str, idempotency_key: str,
    ):
        self._require_admin(actor_role)
        amount = self._cents(amount_cents, "amount_cents")
        payload = {"commission_line_uid": commission_line_uid,
                   "adjustment_kind": adjustment_kind, "amount_cents": amount,
                   "reason": reason}

        def operation():
            line = self.repository.require_by_uid("revenue_commission_lines", commission_line_uid)
            uid = new_uid("commission-adjustment")
            self.connection.execute(
                """INSERT INTO revenue_commission_adjustments(
                     uid,commission_line_id,adjustment_kind,amount_cents,reason,
                     approved_by,approved_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (uid, line["id"], adjustment_kind, amount, reason,
                 actor_user_id, self.repository.now()),
            )
            self.append_balanced_ledger_event(
                "adjustment", "commission_adjustment", uid, amount,
                "CommissionAdjustment", "CommissionReceivable",
                actor_user_id, line["commercial_case_id"],
            )
            return self.repository.require_by_uid("revenue_commission_adjustments", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="commission_adjustment_recorded",
            aggregate_type="commission_adjustment", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def record_clawback(
        self, commission_line_uid: str, amount_cents: int, reason_code: str,
        source_event_type: str, source_event_id: int, actor_user_id: int,
        actor_role: str, idempotency_key: str,
    ):
        self._require_admin(actor_role)
        amount = self._cents(amount_cents, "amount_cents")
        payload = {"commission_line_uid": commission_line_uid, "amount_cents": amount,
                   "reason_code": reason_code, "source_event_type": source_event_type,
                   "source_event_id": source_event_id}

        def operation():
            line = self.repository.require_by_uid("revenue_commission_lines", commission_line_uid)
            uid = new_uid("clawback")
            self.connection.execute(
                """INSERT INTO revenue_clawbacks(
                     uid,commission_line_id,amount_cents,reason_code,source_event_type,
                     source_event_id,status,approved_by,approved_at
                   ) VALUES(?,?,?,?,?,?,'open',?,?)""",
                (uid, line["id"], amount, reason_code, source_event_type,
                 source_event_id, actor_user_id, self.repository.now()),
            )
            self.append_balanced_ledger_event(
                "clawback", "clawback", uid, amount, "ClawbackExpense",
                "CommissionReceivable", actor_user_id, line["commercial_case_id"],
            )
            return self.repository.require_by_uid("revenue_clawbacks", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="commission_clawback_recorded",
            aggregate_type="clawback", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def record_deferral(
        self, commission_line_uid: str, previous_period: str, new_period: str,
        reason: str, actor_user_id: int, actor_role: str, idempotency_key: str,
    ):
        self._require_admin(actor_role)
        payload = {"commission_line_uid": commission_line_uid,
                   "previous_period": previous_period, "new_period": new_period,
                   "reason": reason}

        def operation():
            line = self.repository.require_by_uid("revenue_commission_lines", commission_line_uid)
            uid = new_uid("deferral")
            self.connection.execute(
                """INSERT INTO revenue_deferral_events(
                     uid,commission_line_id,previous_period,new_period,reason,
                     actor_user_id,occurred_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (uid, line["id"], previous_period, new_period, reason,
                 actor_user_id, self.repository.now()),
            )
            self.connection.execute(
                """UPDATE revenue_commission_instalments SET academic_period=?
                   WHERE commission_line_id=? AND academic_period=? AND status NOT IN ('claimed','paid','closed')""",
                (new_period, line["id"], previous_period),
            )
            return self.repository.require_by_uid("revenue_deferral_events", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="commission_deferral_recorded",
            aggregate_type="deferral", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def run_forecast(
        self, as_of_date: str, actor_user_id: int, actor_role: str,
        idempotency_key: str,
    ):
        self._require_admin(actor_role)
        payload = {"as_of_date": as_of_date}

        def operation():
            uid = new_uid("forecast")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_forecast_runs(
                     uid,as_of_date,status,created_by,created_at
                   ) VALUES(?,?,'running',?,?)""",
                (uid, as_of_date, actor_user_id, now),
            )
            run = self.repository.require_by_uid("revenue_forecast_runs", uid)
            result_lines = []
            for line in self.connection.execute("SELECT * FROM revenue_commission_lines"):
                tuition = self.connection.execute(
                    """SELECT authority_level,tuition_cents FROM revenue_tuition_facts
                       WHERE enrolment_component_id=? AND status='active'
                       ORDER BY CASE authority_level WHEN 'official' THEN 3 WHEN 'manual' THEN 2 ELSE 1 END DESC LIMIT 1""",
                    (line["enrolment_component_id"],),
                ).fetchone()
                if not tuition:
                    continue
                forecast_uid = new_uid("forecast-line")
                self.connection.execute(
                    """INSERT INTO revenue_forecast_lines(
                         uid,forecast_run_id,commercial_case_id,enrolment_component_id,
                         tuition_authority_level,tuition_cents,commission_cents,
                         prior_commission_cents,variance_cents,expected_date,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (forecast_uid, run["id"], line["commercial_case_id"],
                     line["enrolment_component_id"], tuition["authority_level"],
                     tuition["tuition_cents"], line["gross_cents"], None,
                     line["gross_cents"], None, now),
                )
                result_lines.append(self.repository.require_by_uid(
                    "revenue_forecast_lines", forecast_uid
                ))
            self.connection.execute(
                "UPDATE revenue_forecast_runs SET status='completed',completed_at=? WHERE id=?",
                (now, run["id"]),
            )
            return {**self.repository.require_by_uid("revenue_forecast_runs", uid),
                    "lines": result_lines}

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="commission_forecast_completed",
            aggregate_type="forecast_run", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def activate_master_agent_share_rule(
        self, master_agent_entity_uid: str, provider_entity_uid: str,
        share_basis_points: int, effective_from: str, actor_user_id: int,
        actor_role: str, idempotency_key: str, program_scope: str | None = None,
    ):
        self._require_admin(actor_role)
        if isinstance(share_basis_points, bool) or not 0 <= int(share_basis_points) <= 10000:
            raise ValueError("share_basis_points out of range")
        payload = {"master_agent_entity_uid": master_agent_entity_uid,
                   "provider_entity_uid": provider_entity_uid,
                   "share_basis_points": int(share_basis_points),
                   "effective_from": effective_from, "program_scope": program_scope}

        def operation():
            master = self.repository.require_by_uid("revenue_entities", master_agent_entity_uid)
            provider = self.repository.require_by_uid("revenue_entities", provider_entity_uid)
            if master["entity_type"] != "master_agent":
                raise ValueError("share rule requires master agent entity")
            snapshot = self._rule_snapshot(
                "master_agent_share", f"{master_agent_entity_uid}:{provider_entity_uid}",
                payload, actor_user_id,
            )
            uid = new_uid("share-rule")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_master_agent_share_rule_versions(
                     uid,master_agent_entity_id,provider_entity_id,program_scope,
                     effective_from,share_basis_points,status,rule_snapshot_id,
                     activated_by,activated_at,created_at
                   ) VALUES(?,?,?,?,?,?,'active',?,?,?,?)""",
                (uid, master["id"], provider["id"], program_scope, effective_from,
                 share_basis_points, snapshot["id"], actor_user_id, now, now),
            )
            return self.repository.require_by_uid(
                "revenue_master_agent_share_rule_versions", uid
            )

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="master_agent_share_rule_activated",
            aggregate_type="master_agent_share_rule", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def activate_master_agent_aaie_share_rule(
        self, master_agent_entity_uid: str, provider_entity_uid: str,
        aaie_share_basis_points: int, effective_from: str, actor_user_id: int,
        actor_role: str, idempotency_key: str, program_scope: str | None = None,
    ):
        """Activate an AAIE-facing share while preserving the stored agent-share schema.

        The V1 schema stores the amount retained by the master agent and derives the
        amount payable to AAIE.  Operational screens use the more natural AAIE-share
        percentage, so this boundary converts once and returns both explicit values.
        """
        self._require_admin(actor_role)
        if (
            isinstance(aaie_share_basis_points, bool)
            or not 0 <= int(aaie_share_basis_points) <= 10000
        ):
            raise ValueError("aaie_share_basis_points out of range")
        aaie_share_basis_points = int(aaie_share_basis_points)
        master_agent_share_basis_points = 10000 - aaie_share_basis_points
        stored = self.activate_master_agent_share_rule(
            master_agent_entity_uid, provider_entity_uid,
            master_agent_share_basis_points, effective_from, actor_user_id,
            actor_role, idempotency_key, program_scope,
        )
        return {
            **stored,
            "aaie_share_basis_points": aaie_share_basis_points,
            "master_agent_share_basis_points": master_agent_share_basis_points,
        }

    def import_statement(
        self, master_agent_entity_uid: str, provider_entity_uid: str,
        document_uid: str, statement_reference: str, items: list[dict],
        actor_user_id: int, actor_role: str, idempotency_key: str,
        period_start: str | None = None, period_end: str | None = None,
        actor_has_revenue_access: bool = False,
    ):
        self._require_operator(actor_role, actor_has_revenue_access)
        payload = {"master_agent_entity_uid": master_agent_entity_uid,
                   "provider_entity_uid": provider_entity_uid,
                   "document_uid": document_uid, "statement_reference": statement_reference,
                   "items": items, "period_start": period_start, "period_end": period_end}
        import_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

        def operation():
            master = self.repository.require_by_uid("revenue_entities", master_agent_entity_uid)
            provider = self.repository.require_by_uid("revenue_entities", provider_entity_uid)
            document = self.repository.require_by_uid("revenue_documents", document_uid)
            prior = self.connection.execute(
                "SELECT * FROM revenue_master_agent_statements WHERE import_hash=?",
                (import_hash,),
            ).fetchone()
            if prior:
                prior_items = [dict(row) for row in self.connection.execute(
                    "SELECT * FROM revenue_master_agent_statement_items WHERE statement_id=? ORDER BY line_number",
                    (prior["id"],),
                )]
                return {**dict(prior), "items": prior_items}
            rule = self.connection.execute(
                """SELECT * FROM revenue_master_agent_share_rule_versions
                   WHERE master_agent_entity_id=? AND provider_entity_id=? AND status='active'
                   ORDER BY effective_from DESC,id DESC LIMIT 1""",
                (master["id"], provider["id"]),
            ).fetchone()
            if not rule:
                raise ValueError("active master agent share rule required")
            total = sum(self._cents(item["gross_commission_cents"],
                                    "gross_commission_cents", allow_zero=True) for item in items)
            uid = new_uid("statement")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_master_agent_statements(
                     uid,master_agent_entity_id,statement_reference,period_start,period_end,
                     currency_code,total_cents,document_id,import_hash,status,imported_by,imported_at
                   ) VALUES(?,?,?,?,?,'AUD',?,?,?,'imported',?,?)""",
                (uid, master["id"], statement_reference, period_start, period_end,
                 total, document["id"], import_hash, actor_user_id, now),
            )
            statement = self.repository.require_by_uid("revenue_master_agent_statements", uid)
            result_items = []
            for line_number, item in enumerate(items, start=1):
                gross = item["gross_commission_cents"]
                share = apply_basis_points(gross, rule["share_basis_points"])
                item_uid = new_uid("statement-item")
                self.connection.execute(
                    """INSERT INTO revenue_master_agent_statement_items(
                         uid,statement_id,external_reference,student_reference,student_name_text,
                         program_text,gross_commission_cents,share_cents,payable_cents,line_number,
                         raw_evidence_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_uid, statement["id"], item.get("external_reference"),
                     item.get("student_reference"), item.get("student_name_text"),
                     item.get("program_text"), gross, share, gross - share, line_number,
                     canonical_json(item)),
                )
                result_items.append(self.repository.require_by_uid(
                    "revenue_master_agent_statement_items", item_uid
                ))
            return {**statement, "items": result_items}

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="master_agent_statement_imported",
            aggregate_type="master_agent_statement", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def suggest_statement_matches(
        self, statement_uid: str, actor_user_id: int, actor_role: str,
        idempotency_key: str, actor_has_revenue_access: bool = False,
    ):
        self._require_operator(actor_role, actor_has_revenue_access)
        payload = {"statement_uid": statement_uid}

        def operation():
            statement = self.repository.require_by_uid(
                "revenue_master_agent_statements", statement_uid
            )
            run_uid = new_uid("reconciliation")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_reconciliation_runs(
                     uid,reconciliation_kind,source_document_id,status,created_by,created_at
                   ) VALUES(?,'master_agent_statement',?,'suggested',?,?)""",
                (run_uid, statement["document_id"], actor_user_id, now),
            )
            run = self.repository.require_by_uid("revenue_reconciliation_runs", run_uid)
            suggestions = []
            items = self.connection.execute(
                "SELECT * FROM revenue_master_agent_statement_items WHERE statement_id=? ORDER BY line_number",
                (statement["id"],),
            ).fetchall()
            for item in items:
                target = self.connection.execute(
                    "SELECT * FROM revenue_commission_lines WHERE uid=?",
                    (item["external_reference"],),
                ).fetchone()
                if not target:
                    continue
                suggestion_uid = new_uid("reconciliation-suggestion")
                amount = min(item["payable_cents"], target["gross_cents"])
                self.connection.execute(
                    """INSERT INTO revenue_reconciliation_suggestions(
                         uid,reconciliation_run_id,source_item_uid,suggested_target_kind,
                         suggested_target_uid,suggested_amount_cents,confidence_basis_points,
                         evidence_json,review_status
                       ) VALUES(?,? ,?,'commission_line',?,?,10000,?,'pending')""",
                    (suggestion_uid, run["id"], item["uid"], target["uid"], amount,
                     canonical_json({"match": "exact_external_reference"})),
                )
                suggestions.append(self.repository.require_by_uid(
                    "revenue_reconciliation_suggestions", suggestion_uid
                ))
            return {**run, "suggestions": suggestions}

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="statement_matches_suggested",
            aggregate_type="reconciliation_run", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def confirm_statement_match(
        self, suggestion_uid: str, actor_user_id: int, actor_role: str,
        idempotency_key: str, actor_has_revenue_access: bool = False,
    ):
        self._require_operator(actor_role, actor_has_revenue_access)
        payload = {"suggestion_uid": suggestion_uid}

        def operation():
            suggestion = self.repository.require_by_uid(
                "revenue_reconciliation_suggestions", suggestion_uid
            )
            if suggestion["review_status"] != "pending":
                raise ValueError("suggestion already reviewed")
            item = self.repository.require_by_uid(
                "revenue_master_agent_statement_items", suggestion["source_item_uid"]
            )
            line = self.repository.require_by_uid(
                "revenue_commission_lines", suggestion["suggested_target_uid"]
            )
            uid = new_uid("statement-match")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_statement_matches(
                     uid,statement_item_id,commission_line_id,match_status,matched_cents,
                     evidence_json,confirmed_by,confirmed_at
                   ) VALUES(?,?,?,'confirmed',?,?,?,?)""",
                (uid, item["id"], line["id"], suggestion["suggested_amount_cents"],
                 suggestion["evidence_json"], actor_user_id, now),
            )
            self.connection.execute(
                """UPDATE revenue_reconciliation_suggestions
                   SET review_status='confirmed',reviewed_by=?,reviewed_at=? WHERE id=?""",
                (actor_user_id, now, suggestion["id"]),
            )
            self.connection.execute(
                """UPDATE revenue_reconciliation_runs
                   SET status='confirmed',confirmed_by=?,confirmed_at=? WHERE id=?""",
                (actor_user_id, now, suggestion["reconciliation_run_id"]),
            )
            self.append_balanced_ledger_event(
                "expected", "statement_match", uid, suggestion["suggested_amount_cents"],
                "MasterAgentReceivable", "CommissionRevenue", actor_user_id,
                line["commercial_case_id"],
            )
            return self.repository.require_by_uid("revenue_statement_matches", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="statement_match_confirmed",
            aggregate_type="statement_match", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def open_dispute(
        self, statement_item_uid: str, commission_line_uid: str | None,
        dispute_code: str, description: str, disputed_cents: int,
        actor_user_id: int, actor_role: str, idempotency_key: str,
    ):
        self._require_admin(actor_role)
        amount = self._cents(disputed_cents, "disputed_cents")
        payload = {"statement_item_uid": statement_item_uid,
                   "commission_line_uid": commission_line_uid,
                   "dispute_code": dispute_code, "description": description,
                   "disputed_cents": amount}

        def operation():
            item = self.repository.require_by_uid(
                "revenue_master_agent_statement_items", statement_item_uid
            )
            line = self.repository.require_by_uid(
                "revenue_commission_lines", commission_line_uid
            ) if commission_line_uid else None
            uid = new_uid("dispute")
            self.connection.execute(
                """INSERT INTO revenue_disputes(
                     uid,statement_item_id,commission_line_id,dispute_code,description,
                     disputed_cents,status,opened_by,opened_at
                   ) VALUES(?,?,?,?,?,?,'open',?,?)""",
                (uid, item["id"], line["id"] if line else None, dispute_code,
                 description, amount, actor_user_id, self.repository.now()),
            )
            return self.repository.require_by_uid("revenue_disputes", uid)

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="statement_dispute_opened",
            aggregate_type="dispute", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )

    def reconcile_statement_payment(
        self, statement_item_uid: str, amount_cents: int,
        master_agent_entity_uid: str, received_date: str, actor_user_id: int,
        actor_role: str, idempotency_key: str,
        actor_has_revenue_access: bool = False,
    ):
        self._require_operator(actor_role, actor_has_revenue_access)
        amount = self._cents(amount_cents, "amount_cents")
        payload = {"statement_item_uid": statement_item_uid,
                   "amount_cents": amount,
                   "master_agent_entity_uid": master_agent_entity_uid,
                   "received_date": received_date}

        def operation():
            item = self.repository.require_by_uid(
                "revenue_master_agent_statement_items", statement_item_uid
            )
            entity = self.repository.require_by_uid(
                "revenue_entities", master_agent_entity_uid
            )
            allocated = self.connection.execute(
                "SELECT COALESCE(SUM(allocated_cents),0) FROM revenue_payment_allocations WHERE statement_item_uid=?",
                (statement_item_uid,),
            ).fetchone()[0]
            if allocated + amount > item["payable_cents"]:
                raise ValueError("statement payment exceeds payable amount")
            uid = new_uid("payment")
            now = self.repository.now()
            self.connection.execute(
                """INSERT INTO revenue_payments(
                     uid,payment_direction,counterparty_kind,revenue_entity_id,received_date,
                     amount_cents,allocated_cents,currency_code,status,confirmed_by,confirmed_at
                   ) VALUES(?,'received','master_agent',?,?,?,?,'AUD','confirmed',?,?)""",
                (uid, entity["id"], received_date, amount, amount, actor_user_id, now),
            )
            payment = self.repository.require_by_uid("revenue_payments", uid)
            self.connection.execute(
                """INSERT INTO revenue_payment_allocations(
                     uid,payment_id,statement_item_uid,allocated_cents,confirmed_by,confirmed_at
                   ) VALUES(?,?,?,?,?,?)""",
                (new_uid("allocation"), payment["id"], statement_item_uid, amount,
                 actor_user_id, now),
            )
            self.append_balanced_ledger_event(
                "received", "master_agent_payment", uid, amount, "Bank",
                "MasterAgentReceivable", actor_user_id, commercial_case_id=None,
            )
            return payment

        return self.repository.command(
            idempotency_key=idempotency_key, command_type="statement_payment_reconciled",
            aggregate_type="payment", payload=payload,
            actor_user_id=actor_user_id, operation=operation,
        )
