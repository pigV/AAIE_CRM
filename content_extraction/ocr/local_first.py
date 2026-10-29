import time
from collections import Counter
from dataclasses import dataclass, replace

from .box_merge import merge_provider_text
from .critical_tokens import critical_token_conflicts, extract_critical_tokens
from .normalization import normalize_for_comparison, similarity_signals
from .parallel import ParallelOCRRunner


@dataclass(frozen=True)
class FusionConflict:
    type: str
    values: dict


@dataclass(frozen=True)
class FusionMetricsSnapshot:
    providers: dict
    fusion: dict


@dataclass(frozen=True)
class FrameOCRFusion:
    frame_id: str
    timestamp_ms: int
    start_ms: int
    end_ms: int
    fused_text: str
    normalized_text: str
    fusion_status: str
    confidence_level: str
    providers_attempted: tuple
    providers_used: tuple
    local_providers: tuple
    cloud_providers: tuple
    cloud_called: bool
    cloud_call_count: int
    agreement_count: int
    provider_results: tuple
    critical_tokens: tuple
    conflicts: tuple
    alternatives: tuple
    selected_source: str
    selection_reason: str
    bounding_region: dict
    metrics: FusionMetricsSnapshot
    local_fusion_status: str = ""

    def to_dict(self):
        return {
            "frame_id": self.frame_id,
            "timestamp": self.timestamp_ms / 1000.0,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "fused_text": self.fused_text,
            "normalized_text": self.normalized_text,
            "fusion_status": self.fusion_status,
            "local_fusion_status": self.local_fusion_status,
            "confidence_level": self.confidence_level,
            "providers_attempted": list(self.providers_attempted),
            "providers_used": list(self.providers_used),
            "local_providers": list(self.local_providers),
            "cloud_providers": list(self.cloud_providers),
            "cloud_called": self.cloud_called,
            "cloud_call_count": self.cloud_call_count,
            "agreement_count": self.agreement_count,
            "provider_results": [item.to_unified_dict() for item in self.provider_results],
            "critical_tokens": [item.__dict__ for item in self.critical_tokens],
            "conflicts": [{"type": item.type, "values": item.values} for item in self.conflicts],
            "alternatives": list(self.alternatives),
            "selected_source": self.selected_source,
            "selection_reason": self.selection_reason,
            "bounding_region": self.bounding_region,
            "metrics": {"providers": self.metrics.providers, "fusion": self.metrics.fusion},
        }


class _FusionMetrics:
    def __init__(self, provider_names):
        self.providers = {
            name: {"calls": 0, "successes": 0, "failures": 0,
                   "latency_ms": 0.0, "conflict_count": 0}
            for name in provider_names
        }
        self.fusion = {
            "local_exact_count": 0,
            "local_similar_count": 0,
            "cloud_arbitration_count": 0,
            "tencent_arbitration_count": 0,
            "iflytek_arbitration_count": 0,
            "unresolved_conflict_count": 0,
        }

    def record_call(self, provider, result, elapsed_ms):
        values = self.providers.setdefault(
            provider, {"calls": 0, "successes": 0, "failures": 0,
                       "latency_ms": 0.0, "conflict_count": 0}
        )
        values["calls"] += 1
        values["latency_ms"] = round(values["latency_ms"] + elapsed_ms, 3)
        if result is not None and result.status == "completed" and result.text:
            values["successes"] += 1
        else:
            values["failures"] += 1

    def snapshot(self):
        providers = {}
        for provider, values in self.providers.items():
            item = dict(values)
            item["average_latency_ms"] = round(
                item["latency_ms"] / item["calls"], 3
            ) if item["calls"] else 0.0
            providers[provider] = item
        return FusionMetricsSnapshot(providers, dict(self.fusion))


class _MeasuredAdapter:
    def __init__(self, adapter, metrics):
        self.adapter = adapter
        self.metrics = metrics
        self.provider = adapter.provider
        self.model_version = adapter.model_version
        self.billing_type = getattr(adapter, "billing_type", "cloud")

    def recognize(self, segment):
        started = time.perf_counter()
        result = None
        try:
            result = self.adapter.recognize(segment)
            return result
        finally:
            self.metrics.record_call(
                self.provider, result, (time.perf_counter() - started) * 1000
            )


def select_unique_vote(results):
    successful = tuple(
        result for result in results
        if result.status == "completed" and result.text and float(result.confidence) > 0
    )
    if not successful:
        return None, 0
    prepared = tuple((result, merge_provider_text(result)) for result in successful)
    keys = [normalize_for_comparison(text) for _, text in prepared]
    counts = Counter(keys)
    ordered = counts.most_common()
    top_key, top_count = ordered[0]
    second_count = ordered[1][1] if len(ordered) > 1 else 0
    if top_count < 2 or top_count == second_count:
        return None, top_count
    winner = next((result, text) for (result, text), key in zip(prepared, keys) if key == top_key)
    return winner, top_count


class LocalFirstOCRFusionCoordinator:
    def __init__(
        self, repository, local_adapters, cloud_adapters, billing_gate,
        cloud_order=("tencent", "iflytek"), similar_threshold=.9,
        runner_factory=ParallelOCRRunner,
    ):
        self.repository = repository
        self.billing_gate = billing_gate
        self.cloud_order = tuple(dict.fromkeys(str(item).strip().lower() for item in cloud_order if str(item).strip()))
        self.similar_threshold = float(similar_threshold)
        self.runner_factory = runner_factory
        names = [adapter.provider for adapter in (*local_adapters, *cloud_adapters)]
        self.metrics = _FusionMetrics(names)
        self.local_adapters = tuple(_MeasuredAdapter(item, self.metrics) for item in local_adapters)
        self.cloud_adapters = {
            item.provider: _MeasuredAdapter(item, self.metrics) for item in cloud_adapters
        }

    def _run(self, segment, adapters):
        if not adapters:
            return ()
        return self.runner_factory(
            self.repository, tuple(adapters), max_workers=max(1, len(adapters))
        ).recognize(segment).results

    @staticmethod
    def _successful(results):
        return tuple(
            result for result in results
            if result.status == "completed" and result.text and float(result.confidence) > 0
        )

    @staticmethod
    def _choose_fallback(results):
        successful = LocalFirstOCRFusionCoordinator._successful(results)
        if not successful:
            return None, ""
        prepared = [(result, merge_provider_text(result)) for result in successful]
        max_length = max(len(normalize_for_comparison(text)) for _, text in prepared)
        candidates = [item for item in prepared if len(normalize_for_comparison(item[1])) == max_length]
        return max(candidates, key=lambda item: float(item[0].confidence))

    @staticmethod
    def _conflicts(results):
        output = []
        prepared = tuple(
            replace(result, text=merge_provider_text(result)) for result in results
        )
        for conflict in critical_token_conflicts(prepared):
            values = {
                provider: list(tokens) for provider, tokens in conflict.values
            }
            output.append(FusionConflict(conflict.token_type, values))
        return tuple(output)

    @staticmethod
    def _bounding_region(result):
        if result is None or not result.bounding_boxes:
            return {}
        valid = [item for item in result.bounding_boxes if isinstance(item, dict) and item]
        if not valid:
            return {}
        try:
            left = min(int(item["x"]) for item in valid)
            top = min(int(item["y"]) for item in valid)
            right = max(int(item["x"]) + int(item["width"]) for item in valid)
            bottom = max(int(item["y"]) + int(item["height"]) for item in valid)
        except (KeyError, TypeError, ValueError):
            return {}
        return {"x": left, "y": top, "width": right - left, "height": bottom - top}

    def _finish(self, segment, results, selected, selected_text, status, confidence,
                agreement, cloud_calls_before, reason, local_status=""):
        successful = self._successful(results)
        conflicts = self._conflicts(successful)
        for conflict in conflicts:
            for provider in conflict.values:
                self.metrics.providers[provider]["conflict_count"] += 1
        cloud_calls = sum(
            values["calls"] for name, values in self.metrics.providers.items()
            if name in self.cloud_adapters
        ) - cloud_calls_before
        alternatives = tuple(dict.fromkeys(
            merge_provider_text(result) for result in successful
            if normalize_for_comparison(merge_provider_text(result)) != normalize_for_comparison(selected_text)
        ))
        return FrameOCRFusion(
            segment.id, segment.start_ms, segment.start_ms, segment.end_ms,
            selected_text, normalize_for_comparison(selected_text), status, confidence,
            tuple(result.provider for result in results),
            tuple(result.provider for result in successful),
            tuple(result.provider for result in successful if result.provider in {item.provider for item in self.local_adapters}),
            tuple(result.provider for result in successful if result.provider in self.cloud_adapters),
            cloud_calls > 0, cloud_calls, agreement, tuple(results),
            extract_critical_tokens(selected_text), conflicts, alternatives,
            selected.provider if selected else "", reason,
            self._bounding_region(selected), self.metrics.snapshot(), local_status,
        )

    def fuse(self, segment):
        cloud_calls_before = sum(
            values["calls"] for name, values in self.metrics.providers.items()
            if name in self.cloud_adapters
        )
        local_results = tuple(self._run(segment, self.local_adapters))
        local_success = self._successful(local_results)
        local_status = "LOCAL_CONFLICT_UNRESOLVED"
        if len(local_success) == 2:
            first_text = merge_provider_text(local_success[0])
            second_text = merge_provider_text(local_success[1])
            critical = self._conflicts(local_success)
            signals = similarity_signals(first_text, second_text)
            if first_text == second_text:
                local_status = "LOCAL_CONSENSUS_EXACT"
                self.metrics.fusion["local_exact_count"] += 1
                return self._finish(
                    segment, local_results, local_success[0], first_text,
                    "LOCAL_CONSENSUS_EXACT", "HIGH", 2, cloud_calls_before,
                    "two local providers returned identical raw text", local_status,
                )
            if not critical and (signals.exact or signals.ratio >= self.similar_threshold):
                local_status = "LOCAL_CONSENSUS_SIMILAR"
                self.metrics.fusion["local_similar_count"] += 1
                winner, text = self._choose_fallback(local_success)
                return self._finish(
                    segment, local_results, winner, text,
                    "LOCAL_CONSENSUS_SIMILAR", "HIGH", 2, cloud_calls_before,
                    "local normalized text and critical tokens agree", local_status,
                )
            local_status = (
                "LOCAL_CONFLICT_CRITICAL" if critical else "LOCAL_CONFLICT_LOW"
            )
        elif len(local_success) == 1 and not any(
            self.billing_gate.allows(name, "cloud") and name in self.cloud_adapters
            for name in self.cloud_order
        ):
            local_status = "LOCAL_ONLY_SINGLE_PROVIDER"
            selected = local_success[0]
            return self._finish(
                segment, local_results, selected, merge_provider_text(selected),
                "LOCAL_ONLY_SINGLE_PROVIDER", "LOW", 1, cloud_calls_before,
                "only one local provider succeeded and no free cloud validator is available",
                local_status,
            )
        elif len(local_success) == 1:
            local_status = "LOCAL_ONLY_SINGLE_PROVIDER"

        results = list(local_results)
        called_cloud = False
        for provider in self.cloud_order:
            adapter = self.cloud_adapters.get(provider)
            if adapter is None or not self.billing_gate.allows(provider, "cloud"):
                continue
            called_cloud = True
            self.metrics.fusion["cloud_arbitration_count"] += 1
            self.metrics.fusion[f"{provider}_arbitration_count"] = (
                self.metrics.fusion.get(f"{provider}_arbitration_count", 0) + 1
            )
            results.extend(self._run(segment, (adapter,)))
            winner, count = select_unique_vote(results)
            if winner is not None:
                selected, selected_text = winner
                usable = len(self._successful(results))
                status = "CLOUD_CONSENSUS" if count == usable else "CLOUD_ARBITRATED_MAJORITY"
                return self._finish(
                    segment, tuple(results), selected, selected_text, status,
                    "HIGH" if count >= 2 else "MEDIUM", count, cloud_calls_before,
                    f"{count}/{usable} provider agreement after {provider} arbitration",
                    local_status,
                )

        selected, selected_text = self._choose_fallback(results)
        successful = self._successful(results)
        self.metrics.fusion["unresolved_conflict_count"] += 1
        if len(successful) == 1 and successful[0].provider in {item.provider for item in self.local_adapters}:
            status = "LOCAL_ONLY_SINGLE_PROVIDER"
        elif called_cloud and len(successful) >= 3:
            status = "ALL_PROVIDER_CONFLICT"
        else:
            status = "LOCAL_CONFLICT_UNRESOLVED"
        return self._finish(
            segment, tuple(results), selected, selected_text, status,
            "LOW", 1 if selected else 0, cloud_calls_before,
            "no unique provider agreement; selected longest complete result with confidence tie-breaker",
            local_status,
        )
