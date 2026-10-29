"""Zero-tolerance engineering safety gate for non-Ground-Truth validation."""


def evaluate_engineering_safety_gate(metrics: dict) -> dict:
    failures = []
    if metrics.get("json_compliant_count", 0) != metrics.get("total", 0):
        failures.append("json_compliance_not_100_percent")
    checks = (
        ("accepted_rewrite_violations", "accepted_rewrite_violation"),
        ("accepted_completeness_violations", "accepted_completeness_violation"),
        ("accepted_unauthorized_critical_changes", "unauthorized_critical_change"),
        ("accepted_outside_span_changes", "outside_span_change"),
        ("external_network_request_count", "external_network_request"),
        ("estimated_cost", "nonzero_cost"),
        ("credentials_leak_count", "credentials_leak"),
    )
    for key, reason in checks:
        if metrics.get(key, 0) != 0:
            failures.append(reason)
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "gate_type": "ENGINEERING_SAFETY_ONLY",
        "accuracy_claim": "NOT_AVAILABLE_WITHOUT_HUMAN_GROUND_TRUTH",
    }
