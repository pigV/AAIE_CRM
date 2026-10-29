"""Fail-closed Critical Token authorization."""

from .schemas import GuardDecision


_LLM_PROPOSAL_SOURCES = {"local_llm", "external_llm"}


class CriticalTokenGuard:
    def evaluate(self, candidate, carrier, evidence):
        del carrier
        if not candidate.affects_critical_token:
            return GuardDecision("critical_token", "PASS", ("non_critical_change",))
        supporting = tuple(
            item for item in evidence.provider_evidence
            if candidate.after_span in item.text and item.status == "completed"
        )
        refs = tuple(item.evidence_ref for item in supporting if item.evidence_ref)
        providers = {item.provider for item in supporting}
        modalities = {item.modality for item in supporting}
        if candidate.proposal_source in _LLM_PROPOSAL_SOURCES and len(providers) < 2:
            return GuardDecision(
                "critical_token", "REJECT", ("llm_only_critical_token_change",), refs)
        numeric_like = candidate.correction_type == "NUMBER_CORRECTION"
        if numeric_like and (
            len(providers) < 2
            or len(modalities) < 2
        ):
            return GuardDecision(
                "critical_token", "REJECT", ("insufficient_independent_machine_corroboration",), refs)
        if not numeric_like and not supporting:
            return GuardDecision(
                "critical_token", "REJECT", ("missing_exact_provider_alternative",), refs)
        return GuardDecision("critical_token", "PASS", ("exact_provider_evidence",), refs)
