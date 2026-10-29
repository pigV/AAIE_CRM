"""Auditable Phase 1 local-correction validation metrics."""

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class PhaseOneAggregate:
    sample_count: int
    correction_precision: float | None
    correction_recall: float | None
    false_positive_rate: float | None
    correct_unchanged_rate: float | None
    json_compliance: float | None
    rewrite_violation_count: int
    accepted_rewrite_violation_count: int
    completeness_violation_count: int
    accepted_completeness_violation_count: int
    critical_token_violation_count: int
    unauthorized_critical_change_count: int
    correctly_fixed_count: int
    wrongly_corrected_count: int
    missed_correction_count: int
    minimal_edit_rejection_count: int
    severe_correct_text_break_count: int

    def to_dict(self):
        return asdict(self)


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def evaluate_correction_samples(samples):
    total = len(samples)
    needed = correct = missed = wrong = unchanged_total = unchanged_correct = 0
    json_ok = 0
    rewrite = accepted_rewrite = complete = accepted_complete = critical = unauthorized = minimal = severe = 0
    for item in samples:
        before, truth, final = item["before"], item["ground_truth"], item["final"]
        json_ok += bool(item.get("json_compliant"))
        if before != truth:
            needed += 1
            if final == truth:
                correct += 1
            else:
                missed += 1
                if final != before:
                    wrong += 1
        else:
            unchanged_total += 1
            if final == before:
                unchanged_correct += 1
            else:
                wrong += 1
                severe += bool(item.get("severe", True))
        rewrite += int(item.get("rewrite_violation", 0)); accepted_rewrite += int(item.get("accepted_rewrite_violation", 0))
        complete += int(item.get("completeness_violation", 0)); accepted_complete += int(item.get("accepted_completeness_violation", 0))
        critical += int(item.get("critical_token_violation", 0)); unauthorized += int(item.get("unauthorized_critical_change", 0))
        minimal += int(item.get("minimal_edit_rejection", 0))
    return PhaseOneAggregate(
        total, _ratio(correct, correct + wrong), _ratio(correct, needed),
        _ratio(wrong, unchanged_total), _ratio(unchanged_correct, unchanged_total),
        _ratio(json_ok, total), rewrite, accepted_rewrite, complete, accepted_complete,
        critical, unauthorized, correct, wrong, missed, minimal, severe,
    )
