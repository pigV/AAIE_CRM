"""Live Quick Review refinement using frozen SAFEBOX 182 components."""

from dataclasses import dataclass, replace

from .candidate_projection import apply_edit_spans, project_candidate_spans
from .review_queue import ReviewQueue
from .spark_arbiter_queue import (
    classify_review_for_arbitration,
    evaluate_arbitrated_review_item,
)


@dataclass(frozen=True)
class ReviewRefinementResult:
    queue: ReviewQueue
    carrier_segments: dict[str, str]
    audit: tuple[dict, ...]
    accounting: dict


def _safe_result_audit(item, classification, decision=None, error_code=""):
    payload = {
        "review_id": item.review_id,
        "segment_id": item.segment_id,
        "classification": classification.category,
        "decision": "KEEP_REVIEW",
        "reasons": list(classification.reasons),
    }
    if error_code:
        payload["error_code"] = str(error_code)[:120]
    if decision is not None:
        payload["decision"] = decision.status
        payload["reasons"] = list(decision.reasons)
        result = decision.arbitration_result
        payload["spark"] = {
            "status": result.status,
            "selected_option": result.selected_option,
            "choice_valid": bool(result.choice_valid),
            "request_count": int(result.request_count),
            "cache_hit": bool(result.cache_hit),
            "error_code": str(result.error_code or ""),
            "estimated_cost": float(result.estimated_cost),
        }
    elif classification.category == "NON_SUBSTANTIVE_VARIANT":
        payload["decision"] = "AUTO_RESOLVED"
    return payload


def _updated_review_item(item, original_carrier, final_carrier):
    if original_carrier == final_carrier:
        return item
    projection = project_candidate_spans(original_carrier, final_carrier)
    if projection.status != "PROJECTED":
        raise ValueError("carrier_projection_unsafe")
    start = item.start_offset
    end = item.end_offset
    shift = 0
    for span in projection.spans:
        if span.end_offset <= start:
            shift += len(span.after_span) - len(span.before_span)
        elif span.start_offset >= end:
            continue
        else:
            raise ValueError("refinement_overlap_with_unresolved_item")
    new_start = start + shift
    new_end = end + shift
    return replace(
        item,
        carrier_text=final_carrier,
        start_offset=new_start,
        end_offset=new_end,
        suspect_span=final_carrier[new_start:new_end],
    )


class ReviewRefinementStage:
    """Remove safe local resolutions before publishing the immutable Queue."""

    def __init__(self, arbiter_factory):
        self.arbiter_factory = arbiter_factory

    def run(self, queue, carrier_segments):
        original_carriers = {str(key): str(value) for key, value in dict(carrier_segments).items()}
        resolved_carriers = dict(original_carriers)
        unresolved = []
        audit = []
        resolved_ids = set()
        resolved_spans = {}
        arbiter = None
        arbiter_unavailable = ""
        request_count = 0

        for item in queue.items:
            classification = classify_review_for_arbitration(item)
            if classification.category == "NON_SUBSTANTIVE_VARIANT":
                resolved_ids.add(item.review_id)
                audit.append(_safe_result_audit(item, classification))
                continue
            if classification.category != "SPARK_ELIGIBLE_LEXICAL_CONFLICT":
                unresolved.append(item)
                audit.append(_safe_result_audit(item, classification))
                continue
            if arbiter is None and not arbiter_unavailable:
                try:
                    arbiter = self.arbiter_factory(queue.queue_hash)
                except Exception:
                    arbiter_unavailable = "SPARK_ARBITER_UNAVAILABLE"
            if arbiter is None:
                unresolved.append(item)
                audit.append(_safe_result_audit(
                    item, classification, error_code=arbiter_unavailable,
                ))
                continue
            try:
                decision = evaluate_arbitrated_review_item(item, arbiter)
            except Exception:
                unresolved.append(item)
                audit.append(_safe_result_audit(
                    item, classification, error_code="SPARK_ARBITER_EXCEPTION",
                ))
                continue
            request_count += int(decision.arbitration_result.request_count)
            audit.append(_safe_result_audit(item, classification, decision))
            if decision.status != "AUTO_RESOLVED":
                unresolved.append(item)
                continue
            projection = project_candidate_spans(
                original_carriers.get(item.segment_id, item.carrier_text),
                decision.final_text,
            )
            if projection.status not in {"EXACT", "PROJECTED"}:
                unresolved.append(item)
                audit[-1]["decision"] = "KEEP_REVIEW"
                audit[-1]["error_code"] = "RESOLVED_CARRIER_PROJECTION_UNSAFE"
                continue
            resolved_ids.add(item.review_id)
            resolved_spans.setdefault(item.segment_id, []).extend(projection.spans)

        try:
            for segment_id, spans in resolved_spans.items():
                resolved_carriers[segment_id] = apply_edit_spans(
                    original_carriers[segment_id], spans,
                )
            adjusted = tuple(
                _updated_review_item(
                    item,
                    original_carriers.get(item.segment_id, item.carrier_text),
                    resolved_carriers.get(item.segment_id, item.carrier_text),
                )
                for item in unresolved
            )
        except ValueError:
            adjusted = tuple(queue.items)
            resolved_carriers = dict(original_carriers)
            resolved_ids.clear()
            audit.append({
                "classification": "REFINEMENT_FAIL_CLOSED",
                "decision": "KEEP_REVIEW",
                "error_code": "REFINEMENT_OFFSET_UNSAFE",
            })

        refined = ReviewQueue.create(
            queue.source_video_id,
            sum(len(value) for value in resolved_carriers.values()),
            adjusted,
            queue.auto_resolved_count + len(resolved_ids),
        )
        return ReviewRefinementResult(
            queue=refined,
            carrier_segments=resolved_carriers,
            audit=tuple(audit),
            accounting={
                "spark_lite": {
                    "current_run_delta": request_count,
                    "checkpoint_accumulated": 0,
                    "diagnostic_rerun_total": 0,
                },
            },
        )
