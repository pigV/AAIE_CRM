"""Thin business-event adapter for the existing CRM reminders table."""

import json
import re
from datetime import datetime

from reminder_center import create_from_event


SUPPORTED_EVENT_TYPES = frozenset({
    "parent_form.submitted",
    "parent_form.updated",
    "contract.created",
    "contract.opened",
    "contract.viewed",
    "contract.signed",
    "contract.completed",
    "contract.expired",
})

SENSITIVE_METADATA_KEYS = frozenset({
    "token",
    "token_hash",
    "signature",
    "signature_data",
    "signature_image",
    "customer_body",
    "form_body",
    "form_data",
    "content",
})


def _safe_metadata(metadata):
    if not isinstance(metadata, dict):
        return {}
    return {
        str(key): value
        for key, value in metadata.items()
        if str(key).lower() not in SENSITIVE_METADATA_KEYS
        and isinstance(value, (str, int, float, bool, type(None)))
    }


def _related_module(entity_type):
    return "contracts" if entity_type == "contract" else "profile"


def emit_business_event(
    database,
    *,
    event_type,
    student_id,
    entity_type,
    entity_id,
    occurred_at,
    actor_type,
    idempotency_key,
    title,
    message,
    href,
    metadata=None,
):
    """Persist one standard event as an existing CRM reminder and return it."""
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise ValueError(f"Unsupported business event: {event_type}")
    required = {
        "student_id": student_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "occurred_at": occurred_at,
        "actor_type": actor_type,
        "idempotency_key": idempotency_key,
        "title": title,
        "href": href,
    }
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if missing:
        raise ValueError(f"Missing business event fields: {', '.join(missing)}")

    source_type = f"business_event:{entity_type}"
    source_record_id = str(idempotency_key)
    canonical_event_type = event_type
    dedupe_key = source_record_id
    if entity_type == "contract" and event_type in {"contract.opened", "contract.viewed"}:
        match = re.fullmatch(r"contract:([^:]+):(opened|viewed):(.+)", source_record_id)
        if not match:
            raise ValueError("Contract engagement identity must include contract and link IDs")
        contract_id, _raw_action, link_id = match.groups()
        canonical_event_type = "contract.parent_engaged"
        dedupe_key = f"contract:{contract_id}:parent_engaged:{link_id}"
        source_record_id = dedupe_key
    notes = json.dumps(
        {
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "actor_type": actor_type,
            "href": href,
            "metadata": _safe_metadata(metadata),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return create_from_event(database, {
        "dedupe_key": dedupe_key,
        "student_id": student_id,
        "reminder_type": canonical_event_type,
        "title": title,
        "description": message or "",
        "related_module": _related_module(entity_type),
        "related_record_id": str(entity_id),
        "priority": "High",
        "due_date": str(occurred_at)[:10],
        "due_datetime": occurred_at,
        "reminder_datetime": occurred_at,
        "repeat_rule": "none",
        "created_at": occurred_at,
        "updated_at": occurred_at,
        "source_type": source_type,
        "source_record_id": source_record_id,
        "source_module": entity_type,
        "source_object_type": entity_type,
        "source_object_id": str(entity_id),
        "source_event_type": canonical_event_type,
        "source_event_id": dedupe_key,
        "auto_generated": True,
        "suggested_action": href,
        "notes": notes,
        "metadata": {"raw_event_type": event_type, **_safe_metadata(metadata)},
    })


def business_event_notifications(database, since, limit=60, now=None):
    """Return adapter-backed reminders in the existing live-toast shape."""
    rows = database.all(
        """SELECT id,student_id,reminder_type,title,description,created_at,
                  suggested_action,notes,status
             FROM reminders
            WHERE source_type LIKE 'business_event:%'
              AND created_at>?
              AND status NOT IN ('Completed','Dismissed','Cancelled')
              AND (status<>'Snoozed' OR COALESCE(snooze_until,'')<=?)
            ORDER BY created_at ASC,id ASC
            LIMIT ?""",
        (since, str(now or datetime.now().astimezone().isoformat()), int(limit)),
    )
    events = []
    for row in rows:
        try:
            notes = json.loads(row["notes"] or "{}")
        except (TypeError, ValueError):
            notes = {}
        event_type = row["reminder_type"] or notes.get("event_type") or "business_event"
        events.append({
            "id": f"business_event:{row['id']}",
            "type": event_type,
            "kind": "success" if event_type.endswith(("submitted", "signed", "completed")) else "info",
            "title": row["title"] or event_type,
            "message": row["description"] or row["title"] or event_type,
            "student_id": row["student_id"] or "",
            "student_name": "",
            "reference": str(notes.get("entity_id") or ""),
            "entry_type": str(notes.get("entity_type") or "business_event"),
            "event_at": row["created_at"],
            "href": row["suggested_action"] or str(notes.get("href") or ""),
            "reminder_id": row["id"],
            "reminder_status": row["status"] or "Pending",
            "refresh": ["contracts"] if str(event_type).startswith("contract.") else [],
        })
    return events
