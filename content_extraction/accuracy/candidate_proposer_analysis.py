"""Read-only benefit ceiling for Conservative Candidate Proposal."""

from collections import Counter
from dataclasses import replace

from .candidate_proposer import classify_candidate_proposal_item
from .review_metrics import calculate_review_metrics


_OUTPUT_CATEGORIES = {
    "CANDIDATE_ARBITER_EXISTING": "candidate_arbiter_existing",
    "CANDIDATE_PROPOSER_ELIGIBLE": "candidate_proposer_eligible",
    "CRITICAL_HUMAN_ONLY": "critical_human_only",
    "PROPER_NOUN_HUMAN_ONLY": "proper_noun_human_only",
    "COMPLETENESS_HUMAN_ONLY": "completeness_human_only",
    "OTHER_HUMAN_ONLY": "other_human_only",
}


def analyze_remaining_queue(queue, controlled_job_report):
    if str(controlled_job_report.get("source_video_id") or "") != queue.source_video_id:
        raise ValueError("controlled_report_job_mismatch")
    remaining_ids = tuple(controlled_job_report.get("remaining_review_ids") or ())
    queue_by_id = {value.review_id: value for value in queue.items}
    if len(remaining_ids) != len(set(remaining_ids)) or any(
        value not in queue_by_id for value in remaining_ids
    ):
        raise ValueError("controlled_report_remaining_identity_invalid")
    arbitration = {
        str(value.get("review_id") or ""): str(value.get("classification") or "")
        for value in controlled_job_report.get("item_results") or ()
    }
    if any(value not in arbitration for value in remaining_ids):
        raise ValueError("controlled_report_classification_missing")

    items = tuple(queue_by_id[value] for value in remaining_ids)
    classifications = tuple(
        classify_candidate_proposal_item(item, arbitration[item.review_id])
        for item in items
    )
    counts = Counter(value.category for value in classifications)
    eligible_ids = {
        item.review_id for item, classification in zip(items, classifications)
        if classification.eligible
    }
    minimum_items = tuple(value for value in items if value.review_id not in eligible_ids)
    current = calculate_review_metrics(replace(queue, items=items))
    minimum = calculate_review_metrics(replace(queue, items=minimum_items))
    categories = {
        output: counts.get(internal, 0)
        for internal, output in _OUTPUT_CATEGORIES.items()
    }
    return {
        "source_video_id": queue.source_video_id,
        "source_queue_hash": queue.queue_hash,
        "source_queue_version": queue.queue_version,
        "current": current,
        "category_counts": categories,
        "candidate_proposer_eligible_ids": sorted(eligible_ids),
        "theoretical_minimum": minimum,
        "limited_human_review_possible_at_theoretical_minimum": (
            minimum["review_items"] <= 15 and minimum["review_ratio"] <= 0.10
        ),
        "item_classifications": [
            {
                "review_id": item.review_id,
                "suspect_span": item.suspect_span,
                "arbitration_category": arbitration[item.review_id],
                **classification.to_dict(),
            }
            for item, classification in zip(items, classifications)
        ],
        "provider_request_count": 0,
        "model_execution_count": 0,
        "network_request_count": 0,
        "estimated_cost": 0,
        "database_changed": 0,
    }
