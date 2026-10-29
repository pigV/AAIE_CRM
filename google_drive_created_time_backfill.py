from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


MELBOURNE = ZoneInfo("Australia/Melbourne")
DRIVE_PREFIX = "gdrive://"


class BackfillConflictError(RuntimeError):
    pass


def normalize_created_time(value) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("createdTime missing")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("createdTime must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _normalized_existing_time(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MELBOURNE)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _json_hash(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _row_dict(row) -> dict:
    return dict(row)


def _protected_script(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "created_at"}


def discover_drive_targets(conn) -> dict:
    material_rows = [
        _row_dict(row)
        for row in conn.execute(
            """SELECT m.id AS material_id,m.script_id,m.source_path,
                      s.*
               FROM content_materials m
               LEFT JOIN content_scripts s ON s.id=m.script_id
               WHERE m.source_path LIKE 'gdrive://%'
               ORDER BY m.id"""
        )
    ]
    drive_to_rows: dict[str, list[dict]] = {}
    script_to_drive_ids: dict[int, set[str]] = {}
    ambiguous = []
    for row in material_rows:
        source_key = str(row.get("source_path") or "")
        drive_id = source_key[len(DRIVE_PREFIX):].strip() if source_key.startswith(DRIVE_PREFIX) else ""
        script_id = row.get("script_id")
        if not drive_id:
            ambiguous.append({"material_id": row.get("material_id"), "reason": "missing_drive_file_id"})
            continue
        if script_id is None or row.get("id") is None:
            ambiguous.append({"drive_file_id": drive_id, "reason": "missing_canonical_script"})
            continue
        script_id = int(script_id)
        row["drive_file_id"] = drive_id
        drive_to_rows.setdefault(drive_id, []).append(row)
        script_to_drive_ids.setdefault(script_id, set()).add(drive_id)

    invalid_drive_ids = set()
    invalid_script_ids = set()
    for drive_id, rows in drive_to_rows.items():
        script_ids = {int(row["script_id"]) for row in rows}
        if len(script_ids) != 1:
            invalid_drive_ids.add(drive_id)
            invalid_script_ids.update(script_ids)
            ambiguous.append({
                "drive_file_id": drive_id,
                "script_ids": sorted(script_ids),
                "reason": "drive_id_maps_to_multiple_scripts",
            })
        elif len(rows) != 1:
            invalid_drive_ids.add(drive_id)
            invalid_script_ids.update(script_ids)
            ambiguous.append({
                "drive_file_id": drive_id,
                "material_ids": [int(row["material_id"]) for row in rows],
                "reason": "duplicate_source_key",
            })
    for script_id, drive_ids in script_to_drive_ids.items():
        if len(drive_ids) != 1:
            invalid_script_ids.add(script_id)
            invalid_drive_ids.update(drive_ids)
            ambiguous.append({
                "script_id": script_id,
                "drive_file_ids": sorted(drive_ids),
                "reason": "script_maps_to_multiple_drive_ids",
            })

    targets = []
    for drive_id, rows in sorted(drive_to_rows.items()):
        row = rows[0]
        script_id = int(row["script_id"])
        if drive_id in invalid_drive_ids or script_id in invalid_script_ids or len(rows) != 1:
            continue
        script_row = {
            key: value
            for key, value in row.items()
            if key not in {"material_id", "script_id", "source_path", "drive_file_id"}
        }
        script_row["id"] = script_id
        targets.append({
            "record_id": script_id,
            "material_id": int(row["material_id"]),
            "source_key": str(row["source_path"]),
            "drive_file_id": drive_id,
            "current_created_at": str(script_row.get("created_at") or ""),
            "current_updated_at": str(script_row.get("updated_at") or ""),
            "protected_hash": _json_hash(_protected_script(script_row)),
        })

    identity = [
        {
            "record_id": target["record_id"],
            "material_id": target["material_id"],
            "source_key": target["source_key"],
            "drive_file_id": target["drive_file_id"],
        }
        for target in targets
    ]
    return {
        "total_material_rows": len(material_rows),
        "target_total": len(targets),
        "targets": targets,
        "ambiguous": ambiguous,
        "inventory_hash": _json_hash(identity),
    }


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def load_local_created_times(paths) -> dict:
    observed: dict[str, set[str]] = {}
    for path_value in paths or []:
        path = Path(path_value)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            continue
        for row in _walk_json(payload):
            drive_id = str(row.get("drive_file_id") or row.get("id") or "").strip()
            raw_created = row.get("createdTime")
            if raw_created is None:
                raw_created = row.get("created_time")
            if not drive_id or raw_created in (None, ""):
                continue
            try:
                normalized = normalize_created_time(raw_created)
            except (TypeError, ValueError):
                continue
            observed.setdefault(drive_id, set()).add(normalized)
    values = {
        drive_id: next(iter(times))
        for drive_id, times in observed.items()
        if len(times) == 1
    }
    conflicts = {
        drive_id: sorted(times)
        for drive_id, times in observed.items()
        if len(times) > 1
    }
    return {"values": values, "conflicts": conflicts}


def _table_count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def build_backfill_plan(conn, metadata_lookup, *, local_metadata=None) -> dict:
    inventory = discover_drive_targets(conn)
    local = local_metadata or {"values": {}, "conflicts": {}}
    local_values = dict(local.get("values") or {})
    local_conflicts = dict(local.get("conflicts") or {})
    proposals = []
    unrecoverable = []
    ambiguity = list(inventory["ambiguous"])
    local_recoverable = 0
    api_recoverable = 0
    changed_expected = 0
    unchanged_expected = 0

    for target in inventory["targets"]:
        drive_id = target["drive_file_id"]
        if drive_id in local_conflicts:
            ambiguity.append({
                "record_id": target["record_id"],
                "drive_file_id": drive_id,
                "reason": "conflicting_local_created_time",
            })
            continue
        source = "local"
        raw_created = local_values.get(drive_id)
        if raw_created:
            local_recoverable += 1
        else:
            source = "api"
            try:
                metadata = metadata_lookup(drive_id)
                if not isinstance(metadata, dict):
                    raise ValueError("metadata response is not an object")
                if str(metadata.get("id") or "") != drive_id:
                    raise ValueError("metadata file ID mismatch")
                raw_created = metadata.get("createdTime")
                canonical = normalize_created_time(raw_created)
            except Exception as exc:
                unrecoverable.append({
                    "record_id": target["record_id"],
                    "drive_file_id": drive_id,
                    "reason": str(exc)[:240],
                    "classification": "UNRECOVERABLE_CREATED_TIME",
                })
                continue
            api_recoverable += 1
        try:
            canonical = normalize_created_time(raw_created)
        except (TypeError, ValueError) as exc:
            unrecoverable.append({
                "record_id": target["record_id"],
                "drive_file_id": drive_id,
                "reason": str(exc)[:240],
                "classification": "UNRECOVERABLE_CREATED_TIME",
            })
            continue
        unchanged = _normalized_existing_time(target["current_created_at"]) == canonical
        if unchanged:
            unchanged_expected += 1
        else:
            changed_expected += 1
        proposals.append({
            **target,
            "canonical_created_at": canonical,
            "metadata_source": source,
            "change_required": not unchanged,
        })

    return {
        "format_version": 1,
        "inventory_hash": inventory["inventory_hash"],
        "targets": inventory["targets"],
        "proposals": proposals,
        "unrecoverable": unrecoverable,
        "ambiguous": ambiguity,
        "table_counts_before": {
            "content_scripts": _table_count(conn, "content_scripts"),
            "content_materials": _table_count(conn, "content_materials"),
        },
        "accounting": {
            "total_material_rows": inventory["total_material_rows"],
            "target_total": inventory["target_total"],
            "local_recoverable": local_recoverable,
            "api_recoverable": api_recoverable,
            "unrecoverable": len(unrecoverable),
            "ambiguous": len(ambiguity),
            "changed_expected": changed_expected,
            "unchanged_expected": unchanged_expected,
        },
    }


def apply_backfill_plan(conn, plan: dict) -> dict:
    if not isinstance(plan, dict) or int(plan.get("format_version") or 0) != 1:
        raise BackfillConflictError("unsupported backfill plan")
    if plan.get("ambiguous"):
        raise BackfillConflictError("backfill plan contains ambiguous mappings")
    expected_targets = list(plan.get("targets") or [])
    proposals = [proposal for proposal in (plan.get("proposals") or []) if proposal.get("change_required")]
    expected_counts = dict(plan.get("table_counts_before") or {})
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = discover_drive_targets(conn)
        if current["ambiguous"] or current["inventory_hash"] != plan.get("inventory_hash"):
            raise BackfillConflictError("Drive identity inventory changed after dry run")
        current_by_id = {target["record_id"]: target for target in current["targets"]}
        if len(current_by_id) != len(expected_targets):
            raise BackfillConflictError("target count changed after dry run")
        for target in expected_targets:
            observed = current_by_id.get(target["record_id"])
            if observed != target:
                raise BackfillConflictError(f"preimage changed for content script {target['record_id']}")
        for table, count in expected_counts.items():
            if _table_count(conn, table) != int(count):
                raise BackfillConflictError(f"{table} count changed after dry run")

        updated = 0
        for proposal in proposals:
            cursor = conn.execute(
                """UPDATE content_scripts
                   SET created_at=?
                   WHERE id=? AND created_at=? AND updated_at=?""",
                (
                    proposal["canonical_created_at"],
                    proposal["record_id"],
                    proposal["current_created_at"],
                    proposal["current_updated_at"],
                ),
            )
            if cursor.rowcount != 1:
                raise BackfillConflictError(f"conditional update failed for content script {proposal['record_id']}")
            updated += 1

        after = discover_drive_targets(conn)
        after_by_id = {target["record_id"]: target for target in after["targets"]}
        for proposal in plan.get("proposals") or []:
            observed = after_by_id.get(proposal["record_id"])
            if observed is None or observed["protected_hash"] != proposal["protected_hash"]:
                raise BackfillConflictError(f"protected fields changed for content script {proposal['record_id']}")
            if observed["current_updated_at"] != proposal["current_updated_at"]:
                raise BackfillConflictError(f"updated_at changed for content script {proposal['record_id']}")
        counts_after = {table: _table_count(conn, table) for table in expected_counts}
        if counts_after != {key: int(value) for key, value in expected_counts.items()}:
            raise BackfillConflictError("content row counts changed during backfill")
        conn.commit()
        return {
            "actual_updated": updated,
            "content_created": 0,
            "content_deleted": 0,
            "table_counts_after": counts_after,
        }
    except Exception:
        conn.rollback()
        raise
