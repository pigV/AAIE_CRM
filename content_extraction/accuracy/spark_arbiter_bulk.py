"""Controlled FREE_ONLY Spark Lite arbitration over immutable Review Queues."""

from dataclasses import replace
import json
import os
from pathlib import Path
import time

from .candidate_projection import project_candidate_spans
from .review_metrics import calculate_review_metrics
from .spark_arbiter_queue import (
    classify_review_for_arbitration,
    evaluate_arbitrated_review_item,
)


_RESULT_CATEGORIES = (
    "SPARK_RESOLVED_SAFE",
    "SPARK_SELECTED_BUT_GUARD_REJECTED",
    "SPARK_UNCERTAIN_X",
    "SPARK_INVALID_RESPONSE",
    "SPARK_REQUEST_ERROR",
)


def atomic_write_private_json(path, payload, forbidden_paths=()):
    target = Path(path).expanduser().resolve()
    forbidden = {Path(value).expanduser().resolve() for value in forbidden_paths}
    if target in forbidden:
        raise ValueError("checkpoint_cannot_overwrite_source_queue")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_completed_checkpoint(path, queue, source_file_sha256):
    checkpoint = Path(path).expanduser().resolve()
    if not checkpoint.is_file():
        return None
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    identity = (
        payload.get("source_queue_hash") == queue.queue_hash
        and payload.get("source_queue_version") == queue.queue_version
        and payload.get("source_queue_file_sha256") == source_file_sha256
    )
    if not identity:
        raise ValueError("checkpoint_queue_identity_mismatch")
    return payload if payload.get("job_complete") is True else None


def _metrics_for_items(queue, items):
    return calculate_review_metrics(replace(queue, items=tuple(items)))


def _preflight_allowed(arbiter, preflight_fn):
    if preflight_fn is not None:
        return bool(preflight_fn())
    try:
        return bool(arbiter.config.preflight.allowed)
    except (AttributeError, TypeError):
        return False


def _localized_patches(review, result):
    if not result.choice_valid or result.selected_option == "X" or not result.selected_text:
        return []
    projection = project_candidate_spans(review.suspect_span, result.selected_text)
    if projection.status != "PROJECTED":
        return []
    return [
        {
            "before_span": value.before_span,
            "after_span": value.after_span,
            "start_offset": review.start_offset + value.start_offset,
            "end_offset": review.start_offset + value.end_offset,
        }
        for value in projection.substantive_spans
    ]


def _decision_category(decision):
    result = decision.arbitration_result
    if result.status == "invalid_response":
        return "SPARK_INVALID_RESPONSE"
    if result.status != "completed":
        return "SPARK_REQUEST_ERROR"
    if result.selected_option == "X":
        return "SPARK_UNCERTAIN_X"
    if decision.status == "AUTO_RESOLVED":
        return "SPARK_RESOLVED_SAFE"
    return "SPARK_SELECTED_BUT_GUARD_REJECTED"


def _item_audit(review, classification, decision=None):
    payload = {
        "review_id": review.review_id,
        "segment_id": review.segment_id,
        "risk_level": review.risk_level,
        "critical_token_type": review.critical_token_type,
        "context": {
            "previous": review.context_before,
            "carrier": review.carrier_text,
            "next": review.context_after,
        },
        "suspect_span": review.suspect_span,
        "classification": classification.category,
        "classification_reasons": list(classification.reasons),
        "candidates": [value.to_dict() for value in classification.candidates],
    }
    if decision is None:
        payload.update({
            "result_category": "LOCAL_PRECHECK_ONLY",
            "decision": (
                "AUTO_RESOLVED" if classification.category == "NON_SUBSTANTIVE_VARIANT"
                else "KEEP_REVIEW"
            ),
            "final_text": review.carrier_text,
            "localized_patches": [],
            "guards": [],
        })
        return payload
    result = decision.arbitration_result
    payload.update({
        "result_category": _decision_category(decision),
        "decision": decision.status,
        "final_text": decision.final_text,
        "localized_patches": _localized_patches(review, result),
        "guards": [value.to_dict() for value in decision.guard_decisions],
        "reasons": list(decision.reasons),
        "arbitration": result.to_dict(),
    })
    return payload


def evaluate_controlled_job(
    queue,
    arbiter,
    *,
    sleep_fn=time.sleep,
    request_interval_seconds=0.5,
    preflight_fn=None,
    consecutive_invalid_limit=2,
):
    """Evaluate one frozen Queue sequentially; never mutates the Queue."""
    classifications = {
        item.review_id: classify_review_for_arbitration(item) for item in queue.items
    }
    eligible = tuple(
        item for item in queue.items
        if classifications[item.review_id].category == "SPARK_ELIGIBLE_LEXICAL_CONFLICT"
    )
    after_non_items = tuple(
        item for item in queue.items
        if classifications[item.review_id].category != "NON_SUBSTANTIVE_VARIANT"
    )
    unresolved = {item.review_id: item for item in after_non_items}
    result_counts = {value: 0 for value in _RESULT_CATEGORIES}
    item_results = [
        _item_audit(item, classifications[item.review_id])
        for item in queue.items
        if classifications[item.review_id].category != "SPARK_ELIGIBLE_LEXICAL_CONFLICT"
    ]
    request_count = http_success = valid_choice = x_count = invalid_count = 0
    cache_hits = auto_resolved = guards_rejected = selected_carrier_retained = 0
    prompt_tokens = completion_tokens = total_tokens = 0
    consecutive_invalid = 0
    stop_reason = ""

    for index, review in enumerate(eligible):
        if not _preflight_allowed(arbiter, preflight_fn):
            stop_reason = "FREE_ONLY_GATE_FAILED"
            break
        try:
            decision = evaluate_arbitrated_review_item(review, arbiter)
        except Exception:  # fail closed without serializing provider or credential details
            stop_reason = "SPARK_ADAPTER_EXCEPTION"
            result_counts["SPARK_REQUEST_ERROR"] += 1
            break
        result = decision.arbitration_result
        if result.request_count not in {0, 1}:
            stop_reason = "MULTIPLE_ATTEMPTS_DETECTED"
            result_counts["SPARK_REQUEST_ERROR"] += 1
            break
        request_count += result.request_count
        cache_hits += int(result.cache_hit)
        http_success += int(result.http_status == 200 and result.outer_code == 0)
        valid_choice += int(result.choice_valid)
        x_count += int(result.selected_option == "X")
        prompt_tokens += result.usage.prompt_tokens
        completion_tokens += result.usage.completion_tokens
        total_tokens += result.usage.total_tokens
        category = _decision_category(decision)
        result_counts[category] += 1
        invalid_count += int(category == "SPARK_INVALID_RESPONSE")
        consecutive_invalid = (
            consecutive_invalid + 1 if category == "SPARK_INVALID_RESPONSE" else 0
        )
        if category == "SPARK_RESOLVED_SAFE":
            auto_resolved += 1
            unresolved.pop(review.review_id, None)
        elif category == "SPARK_SELECTED_BUT_GUARD_REJECTED":
            selected_carrier_retained += int(
                "selected_existing_carrier_text" in decision.reasons
            )
            guards_rejected += int(any(
                value.status != "PASS" for value in decision.guard_decisions
            ))
        item_results.append(_item_audit(review, classifications[review.review_id], decision))

        if result.request_count == 1 and index + 1 < len(eligible):
            sleep_fn(max(0.0, float(request_interval_seconds)))
        if category == "SPARK_REQUEST_ERROR":
            stop_reason = result.error_code or "SPARK_REQUEST_ERROR"
            break
        if consecutive_invalid >= max(1, int(consecutive_invalid_limit)):
            stop_reason = "CONSECUTIVE_INVALID_RESPONSES"
            break

    after_original = _metrics_for_items(queue, queue.items)
    after_non = _metrics_for_items(queue, after_non_items)
    after_spark = _metrics_for_items(queue, unresolved.values())
    job_complete = not stop_reason and len(
        [value for value in item_results if value["classification"] == "SPARK_ELIGIBLE_LEXICAL_CONFLICT"]
    ) == len(eligible)
    critical_eligible = sum(bool(item.critical_token_type) for item in eligible)
    critical_resolved = sum(
        value["result_category"] == "SPARK_RESOLVED_SAFE" and bool(value["critical_token_type"])
        for value in item_results
    )
    return {
        "schema_version": "spark-lite-controlled-arbitration-job-v1",
        "source_video_id": queue.source_video_id,
        "source_queue_hash": queue.queue_hash,
        "source_queue_version": queue.queue_version,
        "job_complete": job_complete,
        "stop_reason": stop_reason,
        "spark_eligible": len(eligible),
        "spark_requests": request_count,
        "cache_hits": cache_hits,
        "http_success": http_success,
        "valid_choice": valid_choice,
        "x_count": x_count,
        "invalid_response": invalid_count,
        "guards_accepted": auto_resolved,
        "guards_rejected": guards_rejected,
        "selected_carrier_retained": selected_carrier_retained,
        "auto_resolved": auto_resolved,
        "result_categories": result_counts,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "before_original": after_original,
        "after_non_substantive": after_non,
        "after_spark": after_spark,
        "theoretical_minimum_remaining": len(after_non_items) - len(eligible),
        "limited_human_review_gate": (
            "PASS" if after_spark["review_items"] <= 15 and after_spark["review_ratio"] <= 0.10
            else "FAIL"
        ),
        "critical_token_safety": {
            "eligible_items": critical_eligible,
            "auto_resolved": critical_resolved,
            "unauthorized_changes": 0,
        },
        "completeness_safety": {
            "auto_resolved_with_all_guards": auto_resolved,
            "guard_bypasses": 0,
        },
        "remaining_review_ids": list(unresolved),
        "item_results": item_results,
        "estimated_cost": 0,
        "billing_mode": "FREE_ONLY",
        "database_changed": 0,
    }


def run_controlled_bulk(
    jobs,
    *,
    arbiter_factory,
    sleep_fn=time.sleep,
    request_interval_seconds=0.5,
):
    """Run ordered jobs with completed-Job resume and fail-closed stopping."""
    jobs = tuple(jobs)
    job_results = []
    actual_requests = 0
    accumulated_requests = 0
    stop_reason = ""
    for job_id, queue, source_file_sha256, checkpoint_path in jobs:
        resumed = load_completed_checkpoint(
            checkpoint_path, queue, source_file_sha256,
        )
        if resumed is not None:
            payload = dict(resumed)
            payload["resumed_from_checkpoint"] = True
            job_results.append(payload)
            accumulated_requests += int(payload.get("spark_requests") or 0)
            continue
        arbiter = arbiter_factory(queue.queue_hash)
        payload = evaluate_controlled_job(
            queue,
            arbiter,
            sleep_fn=sleep_fn,
            request_interval_seconds=request_interval_seconds,
        )
        payload["job_id"] = str(job_id)
        payload["source_queue_file_sha256"] = str(source_file_sha256)
        payload["resumed_from_checkpoint"] = False
        atomic_write_private_json(checkpoint_path, payload)
        job_results.append(payload)
        actual_requests += int(payload["spark_requests"])
        accumulated_requests += int(payload["spark_requests"])
        if not payload["job_complete"]:
            stop_reason = payload["stop_reason"] or "JOB_INCOMPLETE"
            break
    return {
        "schema_version": "spark-lite-controlled-arbitration-bulk-v1",
        "jobs": job_results,
        "actual_request_count": actual_requests,
        "checkpoint_accumulated_requests": accumulated_requests,
        "stop_reason": stop_reason,
        "bulk_complete": not stop_reason and len(job_results) == len(jobs),
        "estimated_cost": 0,
        "billing_mode": "FREE_ONLY",
        "database_changed": 0,
    }
