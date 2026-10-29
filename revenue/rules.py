"""Pure integer-only fee and commission rule evaluators."""

from __future__ import annotations

from revenue.money import allocate_cents, apply_basis_points


class ClientFeePolicy:
    """Locked V1 client-fee decisions expressed only in integer cents."""

    VERSION = "revenue-client-fee-v1"

    def base_fee(
        self, *, education_stream: str, cycle_type: str, existing_aaie_k12: bool
    ) -> dict:
        if education_stream == "higher_education":
            return {"policy_code": "higher-education", "net_cents": 0}
        if cycle_type == "transfer" and existing_aaie_k12:
            return {"policy_code": "existing-transfer", "net_cents": 120_000}
        if cycle_type == "transfer":
            return {"policy_code": "new-onshore-transfer", "net_cents": 240_000}
        return {"policy_code": "k12-new", "net_cents": 240_000}

    def visa_fee(
        self, *, education_stream: str, cycle_type: str, existing_aaie_k12: bool,
        case_type: str | None, visa_subclass: str | None,
        application_type: str | None,
    ) -> dict:
        normalized = " ".join(
            str(value or "").strip().lower()
            for value in (case_type, visa_subclass, application_type)
        )
        if "590" in normalized:
            return {"policy_code": "guardian-590", "net_cents": 90_000}
        if "standalone" in normalized or "renewal" in normalized:
            return {"policy_code": "standalone-visa", "net_cents": 90_000}
        if education_stream == "school_education" and cycle_type == "transfer":
            if existing_aaie_k12:
                return {"policy_code": "existing-transfer-student-visa", "net_cents": 90_000}
            return {"policy_code": "new-transfer-student-visa-included", "net_cents": None}
        if education_stream == "school_education":
            return {"policy_code": "k12-new-student-visa-included", "net_cents": None}
        return {"policy_code": "standalone-visa", "net_cents": 90_000}


class FeeRuleEvaluator:
    def evaluate(
        self, *, existing_aaie_k12: bool, milestones: list[dict],
        calculation_base_cents: int = 0, family_fee_already_applied: bool = False,
        discount_cents: int = 0,
    ) -> list[dict]:
        del existing_aaie_k12  # classification selects the version before evaluation
        selected = [
            item for item in milestones
            if not (item.get("family_once") and family_fee_already_applied)
        ]
        net_values = []
        for item in selected:
            if item.get("fixed_cents") is not None:
                value = item["fixed_cents"]
            else:
                value = apply_basis_points(calculation_base_cents, item["rate_basis_points"])
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("fee values must be non-negative integer cents")
            net_values.append(value)
        if discount_cents < 0 or discount_cents > sum(net_values):
            raise ValueError("discount exceeds fee value")
        discounts = allocate_cents(discount_cents, net_values) if discount_cents else [0] * len(net_values)
        result = []
        for item, original_net, discount in zip(selected, net_values, discounts):
            net = original_net - discount
            gst = apply_basis_points(net, int(item.get("gst_basis_points", 0)))
            result.append({
                "milestone_code": item["code"],
                "net_cents": net,
                "gst_cents": gst,
                "gross_cents": net + gst,
                "discount_cents": discount,
            })
        return result


class CommissionRuleEvaluator:
    """Evaluates normalized rate instalments without floating-point arithmetic."""

    def calculate(self, commissionable_cents: int, instalments: list[dict]) -> list[dict]:
        result = []
        for term in sorted(instalments, key=lambda row: int(row["instalment_no"])):
            net = apply_basis_points(commissionable_cents, int(term["rate_basis_points"]))
            net += int(term.get("fixed_cents") or 0)
            gst = apply_basis_points(net, int(term.get("gst_basis_points", 1000)))
            result.append({
                "instalment_no": int(term["instalment_no"]),
                "academic_period": str(term["academic_period"]),
                "net_cents": net, "gst_cents": gst, "gross_cents": net + gst,
            })
        return result

    def select_scope(self, scopes: list[dict], facts: dict) -> dict | None:
        specificity = {"all": 0, "provider": 1, "school": 2, "program": 3, "student": 4}
        matches = []
        for scope in scopes:
            kind = scope["scope_kind"]
            if kind == "all" or str(facts.get(kind)) == str(scope.get("scope_value")):
                matches.append(scope)
        if not matches:
            return None
        return max(matches, key=lambda row: (
            specificity.get(row["scope_kind"], -1), int(row.get("priority", 0))
        ))

    def evaluate_trigger(self, node: dict, facts: dict) -> bool:
        if "terms" in node:
            values = [self.evaluate_trigger(term, facts) for term in node["terms"]]
            if node.get("operator", "and") == "or":
                return any(values)
            return all(values)
        actual = facts.get(node["fact"])
        operator = node.get("operator", "eq")
        expected = node.get("value")
        if operator == "eq":
            return actual == expected
        if operator == "ne":
            return actual != expected
        if operator == "exists":
            return actual is not None
        if operator == "gte":
            return actual is not None and actual >= expected
        if operator == "lte":
            return actual is not None and actual <= expected
        raise ValueError("unsupported claim trigger operator")
