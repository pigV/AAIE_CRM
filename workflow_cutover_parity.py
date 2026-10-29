"""Deterministic parity contract for legacy and canonical projections."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping


@dataclass(frozen=True)
class ParityDifference:
    metric: str
    legacy_value: object
    canonical_value: object
    classification: str
    explanation: str


@dataclass(frozen=True)
class ParityReport:
    legacy: Mapping[str, object]
    canonical: Mapping[str, object]
    differences: tuple[ParityDifference, ...]
    ready: bool

    def to_json(self) -> str:
        return json.dumps(
            {
                "ready": self.ready,
                "legacy": self.legacy,
                "canonical": self.canonical,
                "differences": [item.__dict__ for item in self.differences],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"


def compare_metric_snapshots(
    legacy: Mapping[str, object],
    canonical: Mapping[str, object],
    *,
    expected_differences: Mapping[str, str] | None = None,
) -> ParityReport:
    expected = dict(expected_differences or {})
    for metric, explanation in expected.items():
        if not str(explanation).strip():
            raise ValueError(f"expected difference {metric!r} requires a non-empty explanation")
    differences: list[ParityDifference] = []
    for metric in sorted(set(legacy) | set(canonical)):
        legacy_value = legacy.get(metric)
        canonical_value = canonical.get(metric)
        if legacy_value == canonical_value:
            continue
        explanation = expected.get(metric, "Unexplained parity difference.")
        differences.append(
            ParityDifference(
                metric,
                legacy_value,
                canonical_value,
                "EXPECTED_DIFFERENCE" if metric in expected else "BUG",
                explanation,
            )
        )
    return ParityReport(
        dict(legacy),
        dict(canonical),
        tuple(differences),
        not any(item.classification == "BUG" for item in differences),
    )
