"""Union-based Quick Review workload accounting."""


def union_length(intervals):
    ordered = sorted((int(start), int(end)) for start, end in intervals if int(end) > int(start))
    if not ordered:
        return 0
    total = 0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


def workload_status(review_items, review_ratio):
    return "REVIEW_REQUIRED" if int(review_items) else "ACCURATE_TRANSCRIPT_COMPLETE"


def historical_limited_review_status(review_items, review_ratio):
    """Retain the retired SAFEBOX 181 limit for diagnostics only."""
    return (
        "FAIL" if int(review_items) > 15 or float(review_ratio) > 0.10
        else "PASS"
    )


def calculate_review_metrics(queue):
    by_segment = {}
    for item in queue.items:
        by_segment.setdefault(item.segment_id, []).append((item.start_offset, item.end_offset))
    review_characters = sum(union_length(intervals) for intervals in by_segment.values())
    denominator = int(queue.total_transcript_characters)
    ratio = review_characters / denominator if denominator else 0.0
    duration_ms = union_length((item.start_ms, item.end_ms) for item in queue.items)
    priority = {name: 0 for name in ("P0", "P1", "P2", "P3")}
    for item in queue.items:
        priority[item.risk_level] += 1
    return {
        "review_items": len(queue.items),
        "review_characters": review_characters,
        "total_transcript_characters": denominator,
        "review_ratio": ratio,
        "review_duration_seconds": duration_ms / 1000,
        "priority_counts": priority,
        "auto_resolved_count": queue.auto_resolved_count,
        "workload_status": workload_status(len(queue.items), ratio),
        "historical_limited_review_gate": historical_limited_review_status(
            len(queue.items), ratio,
        ),
    }
