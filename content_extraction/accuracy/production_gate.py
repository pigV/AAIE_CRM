"""Fail-closed model-suitability decision for SAFEBOX 181 Phase 1."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSuitabilityResult:
    status: str
    reasons: tuple[str, ...]

    def to_dict(self):
        return {"status": self.status, "reasons": list(self.reasons)}


def evaluate_model_suitability(aggregate):
    reasons = []
    if aggregate.sample_count < 30:
        reasons.append("insufficient_human_verified_samples")
    if aggregate.correction_precision is None or aggregate.correction_precision < .99:
        reasons.append("correction_precision_below_99_percent")
    if aggregate.false_positive_rate is None or aggregate.false_positive_rate >= .01:
        reasons.append("false_positive_rate_not_below_1_percent")
    if aggregate.json_compliance is None or aggregate.json_compliance < .99:
        reasons.append("json_compliance_below_99_percent")
    if aggregate.accepted_rewrite_violation_count:
        reasons.append("accepted_rewrite_violation")
    if aggregate.accepted_completeness_violation_count:
        reasons.append("accepted_completeness_violation")
    if aggregate.unauthorized_critical_change_count:
        reasons.append("unauthorized_critical_change")
    if aggregate.severe_correct_text_break_count:
        reasons.append("severe_correct_text_break")
    return ModelSuitabilityResult("FAIL" if reasons else "PASS", tuple(reasons))
