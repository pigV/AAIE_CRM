from __future__ import annotations

from datetime import datetime, timezone


def normalize_drive_timestamp(value, field_name: str) -> str:
    """Normalize a real, timezone-aware Drive timestamp to UTC milliseconds."""
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} missing")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def normalize_source_modified_time(value) -> str:
    return normalize_drive_timestamp(value, "modifiedTime")


def normalize_source_created_time(value) -> str:
    return normalize_drive_timestamp(value, "createdTime")
