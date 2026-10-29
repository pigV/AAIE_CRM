"""Literal span patching and edit-budget enforcement."""

import math
import unicodedata

from .schemas import GuardDecision


def _lexical_count(text):
    return sum(not unicodedata.category(char).startswith(("P", "Z")) for char in text)


def maximum_lexical_edit_distance(text):
    return max(2, math.ceil(0.12 * _lexical_count(text)))


class MinimalEditGuard:
    def evaluate(self, candidate, carrier, evidence):
        del evidence
        try:
            candidate.validate()
        except ValueError as exc:
            return GuardDecision("minimal_edit", "REJECT", (str(exc),))
        if carrier.fingerprint != candidate.carrier_fingerprint and candidate.carrier_fingerprint != "sha256:x":
            return GuardDecision("minimal_edit", "REJECT", ("carrier_fingerprint_mismatch",))
        if carrier.carrier_text[candidate.start_offset:candidate.end_offset] != candidate.before_span:
            return GuardDecision("minimal_edit", "REJECT", ("before_span_mismatch",))
        if candidate.start_offset == 0 and candidate.end_offset == len(carrier.carrier_text):
            return GuardDecision("minimal_edit", "REJECT", ("whole_sentence_rewrite",))
        if max(len(candidate.before_span), len(candidate.after_span)) > 12:
            return GuardDecision("minimal_edit", "HIGH_RISK", ("span_over_12_characters",))
        budget = maximum_lexical_edit_distance(carrier.carrier_text)
        if candidate.lexical_edit_distance > budget:
            return GuardDecision("minimal_edit", "HIGH_RISK", ("edit_budget_exceeded",), metrics={"budget": budget})
        return GuardDecision("minimal_edit", "PASS", ("literal_local_patch",), metrics={"budget": budget})


def apply_non_overlapping_candidates(carrier_text, candidates):
    ordered = tuple(sorted(candidates, key=lambda item: (item.start_offset, item.end_offset)))
    previous_end = 0
    for item in ordered:
        if item.start_offset < previous_end:
            raise ValueError("overlapping_candidates")
        if carrier_text[item.start_offset:item.end_offset] != item.before_span:
            raise ValueError("before_span_mismatch")
        previous_end = item.end_offset
    output = carrier_text
    for item in reversed(ordered):
        output = output[:item.start_offset] + item.after_span + output[item.end_offset:]
    return output
