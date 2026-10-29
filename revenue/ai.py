"""Provider-neutral contract extraction suggestions; never activates business rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ContractSuggestion:
    field_name: str
    suggested_value: str | None
    page_number: int | None
    clause_reference: str | None
    evidence_text: str | None
    confidence_basis_points: int | None


class ProviderUnavailable(RuntimeError):
    pass


class ContractSuggestionProvider(Protocol):
    provider_code: str
    provider_model: str | None

    def suggest(self, contract_document: dict) -> list[ContractSuggestion]: ...


class ManualContractSuggestionProvider:
    provider_code = "manual"
    provider_model = None

    def __init__(self, suggestions: list[ContractSuggestion]):
        self.suggestions = list(suggestions)

    def suggest(self, contract_document: dict) -> list[ContractSuggestion]:
        del contract_document
        return list(self.suggestions)


class UnavailableContractSuggestionProvider:
    provider_code = "unavailable"
    provider_model = None

    def suggest(self, contract_document: dict) -> list[ContractSuggestion]:
        del contract_document
        raise ProviderUnavailable("contract suggestion provider unavailable")
