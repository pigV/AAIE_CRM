from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace

from ..models import OCRResult
from .base import redact_error


@dataclass(frozen=True)
class OCRBatchOutcome:
    results: tuple[OCRResult, ...]
    status: str
    reliability: str

    @property
    def successful(self):
        return tuple(result for result in self.results if result.status == "completed")


class ParallelOCRRunner:
    def __init__(self, repository, adapters, max_workers=3):
        self.repository = repository
        self.adapters = tuple(adapters)
        self.max_workers = max(1, min(int(max_workers), 3))

    @staticmethod
    def _unexpected_failure(adapter, segment, exc):
        return OCRResult(
            adapter.provider, segment.start_ms, segment.end_ms, "", 0.0, (),
            "failed", adapter.model_version, "OCR_ADAPTER_FAILED", redact_error(exc),
        )

    def recognize(self, segment):
        results_by_provider = {}
        pending = []
        for adapter in self.adapters:
            cached = self.repository.get_cached_ocr_result(
                segment.image_hash,
                adapter.provider,
                adapter.model_version,
                segment.preprocessing_version,
            )
            if cached is not None:
                results_by_provider[adapter.provider] = replace(
                    cached, start_ms=segment.start_ms, end_ms=segment.end_ms
                )
            else:
                pending.append(adapter)

        if pending:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(pending))) as executor:
                future_adapters = {
                    executor.submit(adapter.recognize, segment): adapter
                    for adapter in pending
                }
                for future in as_completed(future_adapters):
                    adapter = future_adapters[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = self._unexpected_failure(adapter, segment, exc)
                    results_by_provider[adapter.provider] = result
                    self.repository.record_ocr_attempt(
                        segment.id, adapter.provider, result.status,
                        result.error_code, result.error_message,
                    )
                    if result.status == "completed":
                        self.repository.save_completed_ocr_result(
                            segment.id, result, segment.preprocessing_version
                        )

        ordered = tuple(
            results_by_provider[adapter.provider]
            for adapter in self.adapters
            if adapter.provider in results_by_provider
        )
        success_count = sum(result.status == "completed" for result in ordered)
        if success_count == 0:
            return OCRBatchOutcome(ordered, "ocr_failed", "none")
        reliability = "high" if success_count >= 3 else "medium" if success_count == 2 else "low"
        return OCRBatchOutcome(ordered, "completed", reliability)
