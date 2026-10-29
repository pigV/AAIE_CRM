"""Deterministic Phase 1C comparison helpers; no model execution lives here."""

from ..shared.recognition_text import levenshtein_distance


def require_exact_request_count(actual, expected, label):
    actual = int(actual)
    expected = int(expected)
    if actual != expected:
        raise RuntimeError(f"{label}_request_count_mismatch:{actual}!={expected}")
    return actual


def classify_quality_trend(*, improved_known_phrases, new_severe_errors,
                           engineering_safety):
    if engineering_safety != "PASS" or int(new_severe_errors) > 0:
        return "DEGRADED"
    if int(improved_known_phrases) > 0:
        return "IMPROVED"
    return "SAME"


def _decision_failures(row):
    return {
        decision.get("guard_name", "")
        for decision in row.get("guard_decisions", ())
        if decision.get("status") != "PASS"
    }


def _run_metrics(run):
    rows = list(run.get("item_results") or ())
    proposals = [row for row in rows if row.get("proposal_audit", {}).get("proposal")]
    rejected = [row for row in proposals if not row.get("accepted")]
    accepted = [row for row in proposals if row.get("accepted")]
    ratios = []
    rewrite_count = contextual_count = provider_supported_count = 0
    failures = {"minimal_edit": 0, "completeness": 0, "critical_token": 0,
                "patch_bounds": 0}
    for row in proposals:
        proposal = row["proposal_audit"]["proposal"]
        failure_reasons = {
            str(reason)
            for decision in row.get("guard_decisions", ())
            if decision.get("status") != "PASS"
            for reason in decision.get("reasons", ())
        }
        if any("rewrite" in reason for reason in failure_reasons):
            rewrite_count += 1
        evidence = tuple(proposal.get("evidence_refs") or ())
        contextual_count += int(any("context" in str(value).lower() for value in evidence))
        provider_supported_count += int(row.get("evidence_level") in {"LEVEL_1", "LEVEL_2"})
        for name in _decision_failures(row):
            if name in failures:
                failures[name] += 1
        if row.get("accepted"):
            source = row.get("before_text", "")
            final = row.get("final_text", "")
            ratios.append(levenshtein_distance(source, final) / max(1, len(source)))
    no_correction_preserved = sum(
        1 for row in rows
        if not row.get("proposal_audit", {}).get("correction_needed")
        and row.get("before_text") == row.get("final_text")
    )
    return {
        "sample_count": len(rows),
        "json_compliant_count": sum(
            int(bool(row.get("proposal_audit", {}).get("json_compliant"))) for row in rows
        ),
        "proposal_count": len(proposals),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "no_correction_preserved_count": no_correction_preserved,
        "rewrite_proposal_count": rewrite_count,
        "completeness_violation_count": failures["completeness"],
        "critical_token_violation_count": failures["critical_token"],
        "minimal_edit_violation_count": failures["minimal_edit"],
        "patch_bounds_violation_count": failures["patch_bounds"],
        "average_accepted_edit_ratio": sum(ratios) / len(ratios) if ratios else 0.0,
        "contextual_correction_count": contextual_count,
        "provider_supported_correction_count": provider_supported_count,
    }


def compare_silver_runs(qwen8_run, qwen14_run):
    return {"qwen8": _run_metrics(qwen8_run), "qwen14": _run_metrics(qwen14_run)}
