from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google_drive_source_metadata import normalize_source_modified_time


DRIVE_PREFIX = "gdrive://"
MELBOURNE = ZoneInfo("Australia/Melbourne")


class SourceModifiedBackfillConflict(RuntimeError):
    pass


def _normalize_existing_time(value, *, assume_melbourne=False) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if not assume_melbourne:
            return None
        parsed = parsed.replace(tzinfo=MELBOURNE)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _json_hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _protected_material(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "source_modified_at"}


def discover_source_modified_targets(conn) -> dict:
    rows = [
        dict(row)
        for row in conn.execute(
            """SELECT m.*,s.created_at AS canonical_created_at
                 FROM content_materials AS m
                 LEFT JOIN content_scripts AS s ON s.id=m.script_id
                WHERE m.source_path LIKE 'gdrive://%'
                ORDER BY m.id"""
        )
    ]
    by_drive_id: dict[str, list[dict]] = {}
    by_script_id: dict[int, set[str]] = {}
    ambiguous: list[dict] = []
    for row in rows:
        source_key = str(row.get("source_path") or "")
        drive_id = source_key[len(DRIVE_PREFIX):].strip() if source_key.startswith(DRIVE_PREFIX) else ""
        if not drive_id:
            ambiguous.append({"material_id": row.get("id"), "reason": "missing_drive_file_id"})
            continue
        if row.get("script_id") is None or row.get("canonical_created_at") in (None, ""):
            ambiguous.append({
                "material_id": row.get("id"),
                "drive_file_id": drive_id,
                "reason": "missing_canonical_script",
            })
            continue
        row["drive_file_id"] = drive_id
        by_drive_id.setdefault(drive_id, []).append(row)
        by_script_id.setdefault(int(row["script_id"]), set()).add(drive_id)

    invalid_drive_ids = set()
    invalid_script_ids = set()
    for drive_id, matches in by_drive_id.items():
        if len(matches) != 1:
            invalid_drive_ids.add(drive_id)
            invalid_script_ids.update(int(item["script_id"]) for item in matches)
            ambiguous.append({
                "drive_file_id": drive_id,
                "material_ids": [int(item["id"]) for item in matches],
                "reason": "duplicate_source_key",
            })
    for script_id, drive_ids in by_script_id.items():
        if len(drive_ids) != 1:
            invalid_script_ids.add(script_id)
            invalid_drive_ids.update(drive_ids)
            ambiguous.append({
                "script_id": script_id,
                "drive_file_ids": sorted(drive_ids),
                "reason": "script_maps_to_multiple_drive_ids",
            })

    targets = []
    for drive_id, matches in sorted(by_drive_id.items()):
        row = matches[0]
        script_id = int(row["script_id"])
        if len(matches) != 1 or drive_id in invalid_drive_ids or script_id in invalid_script_ids:
            continue
        material = {
            key: value
            for key, value in row.items()
            if key not in {"canonical_created_at", "drive_file_id"}
        }
        targets.append({
            "material_id": int(row["id"]),
            "script_id": script_id,
            "source_key": str(row["source_path"]),
            "drive_file_id": drive_id,
            "canonical_created_at": str(row["canonical_created_at"] or ""),
            "current_source_modified_at": row.get("source_modified_at"),
            "protected_hash": _json_hash(_protected_material(material)),
        })

    identity = [
        {
            "material_id": item["material_id"],
            "script_id": item["script_id"],
            "source_key": item["source_key"],
            "drive_file_id": item["drive_file_id"],
        }
        for item in targets
    ]
    return {
        "total_material_rows": len(rows),
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


def load_local_modified_times(paths) -> dict:
    observed: dict[str, dict[float, set[str]]] = {}
    for path_value in paths or []:
        path = Path(path_value)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            continue
        snapshot_time = _normalize_existing_time(
            payload.get("created_at") or payload.get("generated_at"),
            assume_melbourne=True,
        ) if isinstance(payload, dict) else None
        try:
            snapshot_rank = (
                datetime.fromisoformat(snapshot_time).timestamp()
                if snapshot_time
                else path.stat().st_mtime
            )
        except (OSError, TypeError, ValueError):
            snapshot_rank = 0.0
        for row in _walk_json(payload):
            drive_id = str(row.get("drive_file_id") or row.get("id") or "").strip()
            raw_modified = row.get("modifiedTime")
            if raw_modified is None:
                raw_modified = row.get("modified_time")
            if not drive_id or raw_modified in (None, ""):
                continue
            try:
                canonical = normalize_source_modified_time(raw_modified)
            except (TypeError, ValueError):
                continue
            observed.setdefault(drive_id, {}).setdefault(snapshot_rank, set()).add(canonical)
    latest = {
        drive_id: snapshots[max(snapshots)]
        for drive_id, snapshots in observed.items()
        if snapshots
    }
    return {
        "values": {
            drive_id: next(iter(times))
            for drive_id, times in latest.items()
            if len(times) == 1
        },
        "conflicts": {
            drive_id: sorted(times)
            for drive_id, times in latest.items()
            if len(times) > 1
        },
    }


def _table_count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def build_source_modified_plan(
    conn,
    metadata_lookup,
    *,
    local_metadata=None,
    require_freshness=False,
) -> dict:
    inventory = discover_source_modified_targets(conn)
    local = local_metadata or {"values": {}, "conflicts": {}}
    local_values = dict(local.get("values") or {})
    local_conflicts = dict(local.get("conflicts") or {})
    proposals = []
    errors = []
    ambiguous = list(inventory["ambiguous"])
    local_recoverable = 0
    api_recoverable = 0
    freshness_revalidated = 0
    missing_modified_time = 0
    inaccessible = 0
    changed_expected = 0
    unchanged_expected = 0

    for target in inventory["targets"]:
        drive_id = target["drive_file_id"]
        if drive_id in local_conflicts and not require_freshness:
            ambiguous.append({
                "material_id": target["material_id"],
                "drive_file_id": drive_id,
                "reason": "conflicting_local_modified_time",
            })
            continue
        if drive_id in local_values or drive_id in local_conflicts:
            local_recoverable += 1

        metadata = None
        source = "local"
        raw_modified = local_values.get(drive_id)
        if require_freshness or not raw_modified:
            source = "api"
            try:
                metadata = metadata_lookup(drive_id)
                if not isinstance(metadata, dict):
                    raise ValueError("metadata response is not an object")
                if str(metadata.get("id") or "") != drive_id:
                    raise ValueError("metadata file ID mismatch")
                raw_modified = metadata.get("modifiedTime")
            except Exception as exc:
                inaccessible += 1
                errors.append({
                    "material_id": target["material_id"],
                    "drive_file_id": drive_id,
                    "classification": "INACCESSIBLE_METADATA",
                    "reason": str(exc)[:240],
                })
                continue
            api_recoverable += 1
            if require_freshness:
                freshness_revalidated += 1

        try:
            canonical_modified = normalize_source_modified_time(raw_modified)
        except (TypeError, ValueError) as exc:
            missing_modified_time += 1
            errors.append({
                "material_id": target["material_id"],
                "drive_file_id": drive_id,
                "classification": "MISSING_MODIFIED_TIME",
                "reason": str(exc)[:240],
            })
            continue

        if metadata is not None:
            raw_created = metadata.get("createdTime")
            try:
                canonical_created = normalize_source_modified_time(raw_created)
            except (TypeError, ValueError) as exc:
                errors.append({
                    "material_id": target["material_id"],
                    "drive_file_id": drive_id,
                    "classification": "INVALID_CREATED_TIME_CHECK",
                    "reason": str(exc)[:240],
                })
                continue
            stored_created = _normalize_existing_time(
                target["canonical_created_at"], assume_melbourne=True
            )
            if stored_created != canonical_created:
                errors.append({
                    "material_id": target["material_id"],
                    "drive_file_id": drive_id,
                    "classification": "CREATED_TIME_MISMATCH",
                    "stored_created_at": stored_created,
                    "drive_created_time": canonical_created,
                })
                continue

        current = _normalize_existing_time(target["current_source_modified_at"])
        unchanged = current == canonical_modified
        if unchanged:
            unchanged_expected += 1
        else:
            changed_expected += 1
        proposals.append({
            **target,
            "canonical_source_modified_at": canonical_modified,
            "metadata_source": source,
            "change_required": not unchanged,
        })

    accounting = {
        "total_material_rows": inventory["total_material_rows"],
        "target_total": inventory["target_total"],
        "exact_mappings": inventory["target_total"],
        "local_recoverable": local_recoverable,
        "api_recoverable": api_recoverable,
        "freshness_revalidated": freshness_revalidated,
        "unrecoverable": len(errors),
        "ambiguous": len(ambiguous),
        "critical_errors": len(errors),
        "missing_modified_time": missing_modified_time,
        "inaccessible": inaccessible,
        "changed_expected": changed_expected,
        "unchanged_expected": unchanged_expected,
    }
    return {
        "format_version": 1,
        "inventory_hash": inventory["inventory_hash"],
        "targets": inventory["targets"],
        "proposals": proposals,
        "errors": errors,
        "ambiguous": ambiguous,
        "table_counts_before": {
            "content_scripts": _table_count(conn, "content_scripts"),
            "content_materials": _table_count(conn, "content_materials"),
        },
        "accounting": accounting,
    }


def validate_source_modified_plan(plan: dict, *, expected_target_count: int | None = None) -> None:
    if not isinstance(plan, dict) or int(plan.get("format_version") or 0) != 1:
        raise SourceModifiedBackfillConflict("unsupported source modified plan")
    if plan.get("ambiguous") or plan.get("errors"):
        raise SourceModifiedBackfillConflict("plan contains unresolved identity or metadata errors")
    expected_targets = list(plan.get("targets") or [])
    proposals = list(plan.get("proposals") or [])
    target_keys = [
        (item.get("material_id"), item.get("drive_file_id"), item.get("source_key"))
        for item in expected_targets
    ]
    proposal_keys = [
        (item.get("material_id"), item.get("drive_file_id"), item.get("source_key"))
        for item in proposals
    ]
    if (
        len(set(target_keys)) != len(target_keys)
        or len(set(proposal_keys)) != len(proposal_keys)
        or set(target_keys) != set(proposal_keys)
    ):
        raise SourceModifiedBackfillConflict("proposal coverage does not exactly match targets")
    accounting = plan.get("accounting") or {}
    target_total = len(expected_targets)
    if expected_target_count is not None and target_total != int(expected_target_count):
        raise SourceModifiedBackfillConflict(
            f"target count mismatch: expected {int(expected_target_count)}, observed {target_total}"
        )
    if any(
        int(accounting.get(key, -1)) != target_total
        for key in ("target_total", "exact_mappings", "freshness_revalidated")
    ):
        raise SourceModifiedBackfillConflict("plan accounting is not exact or freshness-complete")
    if int(accounting.get("critical_errors", -1)) != 0 or int(accounting.get("ambiguous", -1)) != 0:
        raise SourceModifiedBackfillConflict("plan accounting contains unresolved failures")
    if (
        int(accounting.get("changed_expected", -1))
        + int(accounting.get("unchanged_expected", -1))
        != target_total
    ):
        raise SourceModifiedBackfillConflict("plan accounting does not cover every target")


def apply_source_modified_plan(conn, plan: dict, *, expected_target_count: int | None = None) -> dict:
    validate_source_modified_plan(plan, expected_target_count=expected_target_count)
    expected_targets = list(plan.get("targets") or [])
    proposals = list(plan.get("proposals") or [])
    expected_counts = {key: int(value) for key, value in (plan.get("table_counts_before") or {}).items()}
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = discover_source_modified_targets(conn)
        if current["ambiguous"] or current["inventory_hash"] != plan.get("inventory_hash"):
            raise SourceModifiedBackfillConflict("Drive identity inventory changed after dry run")
        current_by_material = {item["material_id"]: item for item in current["targets"]}
        if len(current_by_material) != len(expected_targets):
            raise SourceModifiedBackfillConflict("target count changed after dry run")
        for expected in expected_targets:
            if current_by_material.get(expected["material_id"]) != expected:
                raise SourceModifiedBackfillConflict(
                    f"preimage changed for content material {expected['material_id']}"
                )
        for table, count in expected_counts.items():
            if _table_count(conn, table) != count:
                raise SourceModifiedBackfillConflict(f"{table} count changed after dry run")

        updated = 0
        for proposal in proposals:
            if not proposal.get("change_required"):
                continue
            cursor = conn.execute(
                """UPDATE content_materials
                      SET source_modified_at=?
                    WHERE id=? AND source_path=? AND source_modified_at IS ?""",
                (
                    proposal["canonical_source_modified_at"],
                    proposal["material_id"],
                    proposal["source_key"],
                    proposal["current_source_modified_at"],
                ),
            )
            if cursor.rowcount != 1:
                raise SourceModifiedBackfillConflict(
                    f"conditional update failed for content material {proposal['material_id']}"
                )
            updated += 1

        after = discover_source_modified_targets(conn)
        after_by_material = {item["material_id"]: item for item in after["targets"]}
        for proposal in proposals:
            observed = after_by_material.get(proposal["material_id"])
            if observed is None or observed["protected_hash"] != proposal["protected_hash"]:
                raise SourceModifiedBackfillConflict(
                    f"protected fields changed for content material {proposal['material_id']}"
                )
            if _normalize_existing_time(observed["current_source_modified_at"]) != proposal["canonical_source_modified_at"]:
                raise SourceModifiedBackfillConflict(
                    f"source modified time mismatch for content material {proposal['material_id']}"
                )
        counts_after = {table: _table_count(conn, table) for table in expected_counts}
        if counts_after != expected_counts:
            raise SourceModifiedBackfillConflict("content row counts changed during backfill")
        conn.commit()
        return {
            "actual_updated": updated,
            "unchanged": len(proposals) - updated,
            "content_created": 0,
            "content_deleted": 0,
            "table_counts_after": counts_after,
        }
    except Exception:
        conn.rollback()
        raise
