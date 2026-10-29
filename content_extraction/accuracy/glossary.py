"""Versioned Australian education vocabulary used for scoring, never replacement."""

from dataclasses import dataclass
import hashlib
import json


@dataclass(frozen=True)
class GlossaryCandidate:
    value: str
    score: float
    reason: str


@dataclass(frozen=True)
class AccurateDomainGlossary:
    version: str
    entries: tuple[str, ...]
    domain: str = "australian-school-age-education"

    @classmethod
    def default(cls):
        return cls("accurate-domain-v1", (
            "EAL", "ESL", "VCE", "ATAR", "GPA", "IB", "IELTS", "COE",
            "OSHC", "VISA", "500", "590",
        ))

    @property
    def fingerprint(self):
        payload = json.dumps((self.version, self.entries), ensure_ascii=False).encode()
        return hashlib.sha256(payload).hexdigest()

    def generate_candidates(self, value: str, context: str = "") -> tuple[GlossaryCandidate, ...]:
        folded = str(value or "").upper()
        return tuple(
            GlossaryCandidate(item, 1.0 if item == folded else 0.25, "domain_lookup")
            for item in self.entries if item == folded
        )
