from __future__ import annotations

import json
from datetime import datetime


FAILURE_DETAIL_KEYS = ("failure_items", "failed_items", "failures", "failed", "errors")
FAILURE_COUNT_KEYS = (
    "failed_count",
    "failed",
    "failure_items",
    "failed_items",
    "failures",
    "errors",
)


def _decoded_value(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _scalar_count(value):
    decoded = _decoded_value(value)
    if isinstance(decoded, bool) or not isinstance(decoded, int):
        return None
    return max(0, decoded)


def _detail_item(value) -> dict:
    item = dict(value) if isinstance(value, dict) else {"reason": str(value)}
    item["status"] = "failed"
    return item


def _detail_items(report: dict) -> list[dict]:
    for key in FAILURE_DETAIL_KEYS:
        if key not in report:
            continue
        decoded = _decoded_value(report.get(key))
        if isinstance(decoded, list) and decoded:
            return [_detail_item(value) for value in decoded]
        if isinstance(decoded, dict):
            return [_detail_item(decoded)]
        if isinstance(decoded, str):
            return [_detail_item(decoded)]
    decoded_items = _decoded_value(report.get("items"))
    failed_items = [
        _detail_item(item)
        for item in (decoded_items if isinstance(decoded_items, list) else [])
        if isinstance(item, dict) and str(item.get("status") or "").lower() == "failed"
    ]
    return failed_items


def normalize_failure_report(report: dict | None) -> dict:
    row = report if isinstance(report, dict) else {}
    items = _detail_items(row)
    for key in FAILURE_COUNT_KEYS:
        if key not in row:
            continue
        count = _scalar_count(row.get(key))
        if count is not None:
            return {"count": count, "items": items}
    return {"count": len(items), "items": items}


def _timestamp(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _event_time(row: dict) -> str:
    return str(row.get("created_at") or row.get("ended_at") or row.get("started_at") or "")


def _first_count(row: dict, keys: tuple[str, ...]) -> int:
    for key in keys:
        if key in row:
            count = _scalar_count(row.get(key))
            if count is not None:
                return count
    return 0


def _report_signature(row: dict) -> tuple[int, int, int, int]:
    failures = normalize_failure_report(row)
    return (
        _first_count(row, ("created", "imported", "imported_count")),
        _first_count(row, ("updated", "updated_count")),
        _first_count(row, ("skipped", "skipped_duplicate", "skipped_count", "skipped_duplicate_count")),
        failures["count"],
    )


def select_latest_automatic_report(logs, reports) -> dict | None:
    automatic_logs = [row for row in (logs or []) if row.get("trigger") == "automatic"]
    if not automatic_logs:
        return None
    latest_log = max(automatic_logs, key=_event_time)
    automatic_reports = [row for row in (reports or []) if row.get("trigger") == "automatic"]
    requested_id = str(latest_log.get("report_id") or latest_log.get("scan_id") or "")
    matched = None
    if requested_id:
        matched = next(
            (
                row
                for row in automatic_reports
                if str(row.get("report_id") or row.get("scan_id") or "") == requested_id
            ),
            None,
        )
    if matched is None:
        signature_matches = [
            row for row in automatic_reports if _report_signature(row) == _report_signature(latest_log)
        ]
        ended_at = _timestamp(latest_log.get("ended_at") or latest_log.get("created_at"))
        timed_matches = [
            (abs((stored_at - ended_at).total_seconds()), row)
            for row in signature_matches
            if ended_at is not None and (stored_at := _timestamp(row.get("_stored_at"))) is not None
        ]
        if timed_matches:
            timed_matches.sort(key=lambda pair: pair[0])
            distance, candidate = timed_matches[0]
            nearest_is_unique = len(timed_matches) == 1 or timed_matches[1][0] != distance
            if distance <= 300 and nearest_is_unique:
                matched = candidate
        elif len(signature_matches) == 1:
            matched = signature_matches[0]
    combined = dict(matched or {})
    combined.update(latest_log)
    if matched:
        combined["report_id"] = str(matched.get("report_id") or matched.get("scan_id") or "")
    elif requested_id:
        combined["report_id"] = requested_id
    return combined
