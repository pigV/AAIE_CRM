import hashlib
import json
from dataclasses import dataclass

from ..shared.recognition_text import levenshtein_distance


@dataclass(frozen=True)
class GlossaryEntry:
    value: str
    category: str = "DOMAIN_ACRONYM"
    context_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class GlossaryCandidate:
    value: str
    source_value: str
    generation_reason: str


@dataclass(frozen=True)
class ScoredGlossaryCandidate:
    value: str
    score: int
    reasons: tuple[str, ...]


class DomainGlossary:
    def __init__(self, entries, *, version="asr-domain-v1"):
        self.entries = tuple(entries)
        self.version = str(version)
        self._by_value = {self._canonical(item.value): item for item in self.entries}
        payload = [
            {
                "value": item.value,
                "category": item.category,
                "context_keywords": list(item.context_keywords),
            }
            for item in self.entries
        ]
        self.fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @classmethod
    def default(cls):
        language = ("英语", "课程", "语言")
        academics = ("教育", "选课", "成绩", "课程", "大学")
        migration = ("签证", "留学", "申请")
        return cls((
            GlossaryEntry("EAL", context_keywords=language),
            GlossaryEntry("ESL", context_keywords=language),
            GlossaryEntry("VCE", context_keywords=academics),
            GlossaryEntry("ATAR", context_keywords=academics),
            GlossaryEntry("GPA", context_keywords=academics),
            GlossaryEntry("IB", context_keywords=academics),
            GlossaryEntry("IELTS", context_keywords=language),
            GlossaryEntry("COE", context_keywords=migration),
            GlossaryEntry("OSHC", context_keywords=migration),
            GlossaryEntry("VISA", context_keywords=migration),
        ))

    @staticmethod
    def _canonical(value):
        return "".join(str(value or "").split()).upper()

    def generate_candidates(self, value, context=""):
        source = self._canonical(value)
        if not source:
            return ()
        output = [GlossaryCandidate(source, source, "provider_raw")]
        for entry in self.entries:
            candidate = self._canonical(entry.value)
            if candidate == source:
                output[0] = GlossaryCandidate(
                    source, source, "domain_glossary_exact"
                )
            elif (
                source.isascii()
                and candidate.isascii()
                and len(source) == len(candidate)
                and levenshtein_distance(source, candidate) == 1
            ):
                output.append(GlossaryCandidate(
                    candidate, source, "domain_edit_distance_one"
                ))
        unique = {}
        for item in output:
            unique.setdefault(item.value, item)
        return tuple(unique.values())

    def score_candidates(self, provider_values, context=""):
        raw_by_provider = {
            str(provider): tuple(
                self._canonical(value) for value in values if self._canonical(value)
            )
            for provider, values in provider_values.items()
        }
        candidate_values = set()
        for values in raw_by_provider.values():
            for value in values:
                candidate_values.update(
                    item.value for item in self.generate_candidates(value, context)
                )
        scored = []
        for candidate in sorted(candidate_values):
            score = 0
            reasons = []
            for provider, values in raw_by_provider.items():
                if candidate in values:
                    score += 4
                    reasons.append(f"provider_exact:{provider}")
            entry = self._by_value.get(candidate)
            if entry is not None:
                score += 2
                reasons.append("domain_glossary_exact")
                if any(keyword and keyword in str(context or "")
                       for keyword in entry.context_keywords):
                    score += 1
                    reasons.append("context_category_compatible")
            distances = [
                levenshtein_distance(candidate, raw)
                for values in raw_by_provider.values()
                for raw in values
            ]
            if distances and min(distances) == 1:
                score += 1
                reasons.append("domain_edit_distance_one")
            elif distances and min(distances) > 1:
                score -= 2
                reasons.append("multi_character_change_penalty")
            scored.append(ScoredGlossaryCandidate(
                candidate, score, tuple(reasons)
            ))
        return tuple(sorted(scored, key=lambda item: (-item.score, item.value)))
