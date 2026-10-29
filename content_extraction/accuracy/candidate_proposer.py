"""Fail-closed eligibility and plain-text parsing for Candidate Proposer."""

from dataclasses import dataclass
import re
import unicodedata

from ..shared.critical_tokens import extract_critical_tokens


_PROPER_TYPES = {
    "ACRONYM", "ASCII_ACRONYM", "ENGLISH_ACRONYM", "PROPER_NAME",
    "PROPER_NOUN", "PROPER_NOUN_OR_ACRONYM", "SCHOOL_NAME", "PLACE_NAME",
}
_COMPLETENESS_REASONS = {"possible_completeness_loss"}
_PROPER_REASONS = {"uncertain_proper_noun_or_acronym"}
_EXPLANATION_PREFIXES = (
    "我认为", "我認為", "应该是", "應該是", "答案是", "候选是", "候選是",
    "建议", "建議", "可能是", "修正为", "修正為",
)
_ASCII_ACRONYM = re.compile(r"[A-Za-z]{2,}")


def lexical_count(text):
    return sum(
        1 for char in str(text or "")
        if not unicodedata.category(char).startswith(("P", "Z"))
    )


@dataclass(frozen=True)
class CandidateProposalEligibility:
    category: str
    eligible: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "category": self.category,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class CandidateProposalParseResult:
    status: str
    candidate_text: str = ""
    reasons: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "status": self.status,
            "candidate_text": self.candidate_text,
            "reasons": list(self.reasons),
        }


def classify_candidate_proposal_item(review, arbitration_category):
    if arbitration_category == "SPARK_ELIGIBLE_LEXICAL_CONFLICT":
        return CandidateProposalEligibility(
            "CANDIDATE_ARBITER_EXISTING", False,
            ("existing_candidate_arbiter_scope",),
        )

    token_type = str(review.critical_token_type or "").upper()
    reasons = set(str(value) for value in review.reason_for_review)
    suspect = str(review.suspect_span or "")
    if token_type in _PROPER_TYPES or reasons & _PROPER_REASONS:
        return CandidateProposalEligibility(
            "PROPER_NOUN_HUMAN_ONLY", False, ("proper_noun_or_acronym",),
        )
    if token_type or extract_critical_tokens(suspect) or any(char.isdecimal() for char in suspect):
        return CandidateProposalEligibility(
            "CRITICAL_HUMAN_ONLY", False, ("critical_token_or_numeric_content",),
        )
    if reasons & _COMPLETENESS_REASONS:
        return CandidateProposalEligibility(
            "COMPLETENESS_HUMAN_ONLY", False, ("possible_completeness_loss",),
        )
    if lexical_count(suspect) <= 1 and (
        suspect == str(review.carrier_text or "")
        or "strong_ocr_asr_conflict" in reasons
    ):
        return CandidateProposalEligibility(
            "COMPLETENESS_HUMAN_ONLY", False,
            ("fragmentary_carrier_requires_generation",),
        )
    if arbitration_category != "NO_REAL_ALTERNATIVE":
        return CandidateProposalEligibility(
            "OTHER_HUMAN_ONLY", False, ("not_no_real_alternative",),
        )
    if not suspect or lexical_count(suspect) > 12:
        return CandidateProposalEligibility(
            "OTHER_HUMAN_ONLY", False, ("span_length_out_of_bounds",),
        )
    if _ASCII_ACRONYM.fullmatch(suspect):
        return CandidateProposalEligibility(
            "PROPER_NOUN_HUMAN_ONLY", False, ("ascii_acronym",),
        )
    if not review.carrier_text or not (review.context_before or review.context_after):
        return CandidateProposalEligibility(
            "OTHER_HUMAN_ONLY", False, ("insufficient_context",),
        )
    return CandidateProposalEligibility(
        "CANDIDATE_PROPOSER_ELIGIBLE", True,
        ("short_noncritical_no_real_alternative",),
    )


def parse_candidate_proposal(content, carrier_span, max_lexical_chars=12):
    if not isinstance(content, str):
        return CandidateProposalParseResult(
            "INVALID_PROPOSAL", reasons=("content_not_string",),
        )
    value = content.strip()
    if value == "X":
        return CandidateProposalParseResult("UNCERTAIN_X")
    invalid_reason = ""
    if not value:
        invalid_reason = "empty_proposal"
    elif "\n" in value or "\r" in value:
        invalid_reason = "multiline_proposal"
    elif "```" in value or value.startswith(("{", "[")):
        invalid_reason = "structured_or_markdown_output"
    elif any(value.startswith(prefix) for prefix in _EXPLANATION_PREFIXES):
        invalid_reason = "explanatory_output"
    elif lexical_count(value) > int(max_lexical_chars):
        invalid_reason = "proposal_too_long"
    elif value == str(carrier_span or ""):
        invalid_reason = "no_correction"
    elif extract_critical_tokens(value) or any(char.isdecimal() for char in value):
        invalid_reason = "critical_candidate_forbidden"
    elif abs(lexical_count(value) - lexical_count(carrier_span)) > 4:
        invalid_reason = "proposal_expansion_forbidden"
    if invalid_reason:
        return CandidateProposalParseResult(
            "INVALID_PROPOSAL", reasons=(invalid_reason,),
        )
    return CandidateProposalParseResult("VALID_CANDIDATE", value)
