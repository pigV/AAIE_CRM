"""DB-backed client-fee authority and the auditable 2026 bootstrap manifest.

The manifest is an initialization input only. Runtime resolution reads active,
effective-dated rows from Migration 032 and has no Python-policy fallback.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any

from revenue.repository import RevenueRepository, canonical_json


class MissingFeeRule(LookupError):
    """No active DB rule is authoritative for the supplied facts and date."""


class AmbiguousFeeRule(LookupError):
    """Multiple equally authoritative DB rules match the same fee facts."""


class FeeRuleInitializationConflict(ValueError):
    """The target DB already contains a conflicting initialization authority."""


@dataclass(frozen=True)
class ClientFeeRuleFacts:
    service_kind: str
    education_stream: str
    cycle_type: str
    existing_aaie_k12: bool = False
    application_type: str | None = None
    independent_service: bool = False
    he_category: str | None = None
    onshore: bool = False


@dataclass(frozen=True)
class ResolvedClientFeeRule:
    rule_uid: str
    policy_code: str
    net_cents: int | None
    milestone_code: str
    applicability: dict[str, Any]
    amount_basis: dict[str, Any]
    source_engagement_date: str

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


def _rule(
    code: str,
    amount_cents: int,
    *,
    classification: str = "any",
    stream: str = "any",
    priority: int = 100,
    included: bool = False,
    conditions: tuple[tuple[str, str, Any], ...],
) -> dict[str, Any]:
    return {
        "code": code,
        "description": code.replace("-", " "),
        "student_classification": classification,
        "education_stream": stream,
        "visa_scope": "any",
        "priority": priority,
        "conditions": conditions,
        "milestone_code": f"{code}-included" if included else code,
        "fixed_cents": amount_cents,
        "included": included,
    }


# Formally approved initialization material. It is deliberately data-shaped so
# its SHA can be reviewed independently of the runtime resolver.
CLIENT_FEE_RULES_2026: tuple[dict[str, Any], ...] = (
    _rule(
        "k12-new", 240_000, classification="new", stream="school_education",
        conditions=(
            ("service_kind", "eq", "base"),
            ("cycle_type", "ne", "transfer"),
            ("cycle_type", "ne", "progression"),
        ),
    ),
    _rule(
        "existing-transfer", 120_000, classification="existing",
        stream="school_education", priority=220,
        conditions=(("service_kind", "eq", "base"), ("cycle_type", "eq", "transfer")),
    ),
    _rule(
        "new-onshore-transfer", 240_000, classification="new",
        stream="school_education", priority=220,
        conditions=(("service_kind", "eq", "base"), ("cycle_type", "eq", "transfer")),
    ),
    _rule(
        "higher-education-standard", 0, stream="higher_education",
        conditions=(
            ("service_kind", "eq", "base"),
            ("cycle_type", "ne", "transfer"),
            ("cycle_type", "ne", "progression"),
            ("he_category", "eq", "university_foundation"),
        ),
    ),
    _rule(
        "standalone-language", 0, stream="higher_education",
        conditions=(
            ("service_kind", "eq", "base"),
            ("he_category", "eq", "language_school"),
        ),
    ),
    _rule(
        "he-onshore-transfer", 0, stream="higher_education", priority=220,
        conditions=(
            ("service_kind", "eq", "base"),
            ("cycle_type", "eq", "transfer"),
            ("he_category", "eq", "university_foundation"),
            ("onshore", "eq", True),
        ),
    ),
    _rule(
        "he-onshore-progression", 0, stream="higher_education", priority=220,
        conditions=(
            ("service_kind", "eq", "base"),
            ("cycle_type", "eq", "progression"),
            ("he_category", "eq", "university_foundation"),
            ("onshore", "eq", True),
        ),
    ),
    _rule(
        "k12-new-student-visa-included", 0, classification="new",
        stream="school_education", included=True,
        conditions=(
            ("service_kind", "eq", "student_visa"),
            ("cycle_type", "ne", "transfer"),
            ("application_type", "eq", "new"),
            ("independent_service", "eq", False),
        ),
    ),
    _rule(
        "new-transfer-student-visa-included", 0, classification="new",
        stream="school_education", priority=220, included=True,
        conditions=(
            ("service_kind", "eq", "student_visa"),
            ("cycle_type", "eq", "transfer"),
            ("application_type", "eq", "new"),
            ("independent_service", "eq", False),
        ),
    ),
    _rule(
        "existing-transfer-independent-visa", 90_000,
        classification="existing", stream="school_education", priority=420,
        conditions=(
            ("service_kind", "eq", "student_visa"),
            ("cycle_type", "eq", "transfer"),
            ("application_type", "eq", "new"),
            ("independent_service", "eq", True),
        ),
    ),
    _rule(
        "existing-transfer-student-visa-included", 0,
        classification="existing", stream="school_education", priority=220,
        included=True,
        conditions=(
            ("service_kind", "eq", "student_visa"),
            ("cycle_type", "eq", "transfer"),
            ("application_type", "eq", "new"),
            ("independent_service", "eq", False),
        ),
    ),
    _rule(
        "standalone-student-visa", 90_000, priority=300,
        conditions=(
            ("service_kind", "eq", "student_visa"),
            ("application_type", "eq", "new"),
            ("independent_service", "eq", True),
        ),
    ),
    _rule(
        "student-visa-renewal", 90_000, priority=400,
        conditions=(
            ("service_kind", "eq", "student_visa"),
            ("application_type", "eq", "renewal"),
            ("independent_service", "eq", True),
        ),
    ),
    _rule(
        "guardian-590", 90_000, priority=300,
        conditions=(
            ("service_kind", "eq", "guardian_590"),
            ("application_type", "eq", "new"),
        ),
    ),
    _rule(
        "guardian-590-renewal", 90_000, priority=400,
        conditions=(
            ("service_kind", "eq", "guardian_590"),
            ("application_type", "eq", "renewal"),
        ),
    ),
)


PAYMENT_METHODS_2026: tuple[dict[str, Any], ...] = (
    {"code": "alipay", "display_name": "支付宝", "gst_rate_basis_points": 0},
    {"code": "wechat", "display_name": "微信", "gst_rate_basis_points": 0},
    {"code": "cash", "display_name": "现金", "gst_rate_basis_points": 0},
    {"code": "au_personal_account", "display_name": "澳洲个人账户", "gst_rate_basis_points": 0},
    {"code": "au_company_account", "display_name": "澳洲公司账户", "gst_rate_basis_points": 1000},
)


INITIALIZATION_MANIFEST_2026 = {
    "manifest_uid": "client-fee-authority-2026-v1",
    "effective_from": "2026-01-01",
    "fee_rules": CLIENT_FEE_RULES_2026,
    "payment_methods": PAYMENT_METHODS_2026,
}
INITIALIZATION_MANIFEST_2026_SHA256 = hashlib.sha256(
    canonical_json(INITIALIZATION_MANIFEST_2026).encode("utf-8")
).hexdigest()


class FeeRuleAuthorityService:
    def __init__(self, repository: RevenueRepository):
        self.repository = repository
        self.connection = repository.connection

    def initialize_2026(
        self, *, actor_user_id: int, actor_role: str, idempotency_key: str
    ) -> dict[str, Any]:
        if actor_role != "Admin":
            raise PermissionError("Revenue fee-rule activation requires exact Admin role")
        payload = {
            "manifest_uid": INITIALIZATION_MANIFEST_2026["manifest_uid"],
            "manifest_sha256": INITIALIZATION_MANIFEST_2026_SHA256,
        }

        def operation():
            existing = sum(
                self.connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in (
                    "revenue_fee_rule_sets",
                    "revenue_fee_rule_versions",
                    "revenue_fee_rule_conditions",
                    "revenue_fee_rule_milestones",
                    "revenue_payment_methods",
                    "revenue_payment_method_rule_versions",
                )
            )
            if existing:
                raise FeeRuleInitializationConflict(
                    "fee/payment authority is not empty; initialization requires a fresh authority gate"
                )
            now = self.repository.now()
            effective_from = INITIALIZATION_MANIFEST_2026["effective_from"]
            version_ids: list[int] = []
            for definition in CLIENT_FEE_RULES_2026:
                code = definition["code"]
                set_uid = f"fee-rule-set-2026-{code}"
                version_uid = f"fee-rule-version-2026-{code}-v1"
                cursor = self.connection.execute(
                    """INSERT INTO revenue_fee_rule_sets(
                         uid,name,description,created_by,created_at
                       ) VALUES(?,?,?,?,?)""",
                    (set_uid, f"client_fee.{code}", definition["description"], actor_user_id, now),
                )
                rule_set_id = cursor.lastrowid
                cursor = self.connection.execute(
                    """INSERT INTO revenue_fee_rule_versions(
                         uid,rule_set_id,version_no,effective_from,priority,status,
                         student_classification,education_stream,visa_scope,
                         created_by,created_at
                       ) VALUES(?,?,1,?,?,'draft',?,?,?,?,?)""",
                    (
                        version_uid,
                        rule_set_id,
                        effective_from,
                        definition["priority"],
                        definition["student_classification"],
                        definition["education_stream"],
                        definition["visa_scope"],
                        actor_user_id,
                        now,
                    ),
                )
                version_id = cursor.lastrowid
                version_ids.append(version_id)
                for index, (field, operator, value) in enumerate(definition["conditions"]):
                    self.connection.execute(
                        """INSERT INTO revenue_fee_rule_conditions(
                             uid,rule_version_id,field_name,operator,comparison_value,
                             sort_order,created_at
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            f"fee-condition-2026-{code}-{index + 1}",
                            version_id,
                            field,
                            operator,
                            canonical_json(value),
                            index,
                            now,
                        ),
                    )
                self.connection.execute(
                    """INSERT INTO revenue_fee_rule_milestones(
                         uid,rule_version_id,milestone_code,calculation_kind,
                         fixed_cents,rate_basis_points,due_offset_days,refundable,sort_order
                       ) VALUES(?,?,?,'fixed',?,NULL,0,0,0)""",
                    (
                        f"fee-milestone-2026-{code}",
                        version_id,
                        definition["milestone_code"],
                        definition["fixed_cents"],
                    ),
                )

            for version_id in version_ids:
                self.connection.execute(
                    """UPDATE revenue_fee_rule_versions
                          SET status='active',activated_by=?,activated_at=?
                        WHERE id=? AND status='draft'""",
                    (actor_user_id, now, version_id),
                )

            for definition in PAYMENT_METHODS_2026:
                code = definition["code"]
                cursor = self.connection.execute(
                    """INSERT INTO revenue_payment_methods(
                         uid,method_code,display_name,status
                       ) VALUES(?,?,?,'active')""",
                    (f"payment-method-2026-{code}", code, definition["display_name"]),
                )
                method_id = cursor.lastrowid
                rule_uid = f"payment-method-rule-2026-{code}-v1"
                self.connection.execute(
                    """INSERT INTO revenue_payment_method_rule_versions(
                         uid,payment_method_id,effective_from,gst_rate_basis_points,
                         surcharge_basis_points,status,created_at
                       ) VALUES(?,?,?, ?,0,'draft',?)""",
                    (rule_uid, method_id, effective_from, definition["gst_rate_basis_points"], now),
                )
                self.connection.execute(
                    "UPDATE revenue_payment_method_rule_versions SET status='active' WHERE uid=? AND status='draft'",
                    (rule_uid,),
                )

            return {
                "uid": INITIALIZATION_MANIFEST_2026["manifest_uid"],
                "manifest_sha256": INITIALIZATION_MANIFEST_2026_SHA256,
                "effective_from": effective_from,
                "status": "active",
                "approved_by": actor_user_id,
                "activated_at": now,
                "fee_rule_versions": len(version_ids),
                "payment_methods": len(PAYMENT_METHODS_2026),
            }

        return self.repository.command(
            idempotency_key=idempotency_key,
            command_type="client_fee_authority_initialized",
            aggregate_type="client_fee_authority",
            payload=payload,
            actor_user_id=actor_user_id,
            operation=operation,
        )


class DatabaseClientFeeRuleResolver:
    """Resolves only active DB records; absence and ambiguity are explicit."""

    def __init__(self, connection):
        self.connection = connection
        self.connection.row_factory = getattr(self.connection, "row_factory", None) or __import__(
            "sqlite3"
        ).Row

    @staticmethod
    def _condition_matches(actual: Any, operator: str, expected: Any) -> bool:
        if operator == "eq":
            return actual == expected
        if operator == "ne":
            return actual != expected
        if operator == "in":
            return actual in expected
        if operator == "not_in":
            return actual not in expected
        if operator == "exists":
            return (actual is not None) == bool(expected)
        if operator == "gte":
            return actual is not None and actual >= expected
        if operator == "lte":
            return actual is not None and actual <= expected
        raise ValueError(f"unsupported fee condition operator: {operator}")

    def resolve(
        self, facts: ClientFeeRuleFacts, source_engagement_date: str
    ) -> ResolvedClientFeeRule:
        fact_values = asdict(facts)
        classification = "existing" if facts.existing_aaie_k12 else "new"
        rows = self.connection.execute(
            """SELECT v.*,s.name AS rule_name
                 FROM revenue_fee_rule_versions v
                 JOIN revenue_fee_rule_sets s ON s.id=v.rule_set_id
                WHERE v.status='active'
                  AND v.effective_from<=?
                  AND (v.effective_to IS NULL OR v.effective_to>=?)
                  AND v.student_classification IN ('any',?)
                  AND v.education_stream IN ('any',?)
                ORDER BY v.priority DESC,v.id""",
            (
                source_engagement_date,
                source_engagement_date,
                classification,
                facts.education_stream,
            ),
        ).fetchall()
        matches: list[tuple[Any, list[dict[str, Any]]]] = []
        for row in rows:
            checks = []
            conditions = self.connection.execute(
                """SELECT field_name,operator,comparison_value
                     FROM revenue_fee_rule_conditions
                    WHERE rule_version_id=? ORDER BY sort_order,id""",
                (row["id"],),
            ).fetchall()
            for condition in conditions:
                expected = (
                    json.loads(condition["comparison_value"])
                    if condition["comparison_value"] is not None
                    else None
                )
                actual = fact_values.get(condition["field_name"])
                matched = self._condition_matches(actual, condition["operator"], expected)
                checks.append(
                    {
                        "field": condition["field_name"],
                        "operator": condition["operator"],
                        "expected": expected,
                        "actual": actual,
                        "matched": matched,
                    }
                )
            if all(check["matched"] for check in checks):
                matches.append((row, checks))
        if not matches:
            raise MissingFeeRule(
                f"no active fee rule for {canonical_json(fact_values)} at {source_engagement_date}"
            )
        top_priority = int(matches[0][0]["priority"])
        top = [match for match in matches if int(match[0]["priority"]) == top_priority]
        if len(top) != 1:
            raise AmbiguousFeeRule(
                "multiple active fee rules have the same authoritative priority: "
                + ",".join(match[0]["uid"] for match in top)
            )
        row, checks = top[0]
        milestones = self.connection.execute(
            """SELECT * FROM revenue_fee_rule_milestones
                WHERE rule_version_id=? ORDER BY sort_order,id""",
            (row["id"],),
        ).fetchall()
        if len(milestones) != 1:
            raise AmbiguousFeeRule("client fee rule must have exactly one authoritative milestone")
        milestone = milestones[0]
        if milestone["calculation_kind"] != "fixed" or milestone["fixed_cents"] is None:
            raise MissingFeeRule("client fee rule has no fixed integer-cent authority")
        policy_code = row["rule_name"].removeprefix("client_fee.")
        included = str(milestone["milestone_code"]).endswith("-included")
        return ResolvedClientFeeRule(
            rule_uid=row["uid"],
            policy_code=policy_code,
            net_cents=None if included else int(milestone["fixed_cents"]),
            milestone_code=milestone["milestone_code"],
            applicability={
                "matched": True,
                "student_classification": classification,
                "education_stream": facts.education_stream,
                "conditions": checks,
            },
            amount_basis={
                "kind": "fixed_cents",
                "fixed_cents": int(milestone["fixed_cents"]),
                "included": included,
            },
            source_engagement_date=source_engagement_date,
        )

    def resolve_payment_method(self, method_code: str, effective_date: str) -> dict[str, Any]:
        rows = self.connection.execute(
            """SELECT r.uid AS rule_uid,m.uid AS method_uid,m.method_code,m.display_name,
                      r.gst_rate_basis_points,r.surcharge_basis_points
                 FROM revenue_payment_methods m
                 JOIN revenue_payment_method_rule_versions r ON r.payment_method_id=m.id
                WHERE m.method_code=? AND m.status='active' AND r.status='active'
                  AND r.effective_from<=?
                  AND (r.effective_to IS NULL OR r.effective_to>=?)
                ORDER BY r.effective_from DESC,r.id DESC""",
            (method_code, effective_date, effective_date),
        ).fetchall()
        if not rows:
            raise MissingFeeRule(
                f"no active payment-method fee rule for {method_code} at {effective_date}"
            )
        if len(rows) != 1:
            raise AmbiguousFeeRule(
                f"multiple active payment-method rules for {method_code} at {effective_date}"
            )
        return dict(rows[0])
