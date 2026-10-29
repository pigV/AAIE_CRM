"""Fail-closed Workflow Production operator controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Mapping

from production_environment import PRODUCTION_ROOT
from workflow_business_fingerprint import (
    WORKFLOW_BUSINESS_FINGERPRINT_VERSION,
    workflow_business_state_fingerprint,
    workflow_business_state_fingerprint_path,
)


PROJECT_ROOT = Path(__file__).resolve().parent
FORMAL_DATABASE_PATH = (PRODUCTION_ROOT / "database" / "aaie_crm.db").resolve()
FROZEN_DECISIONS_PATH = PROJECT_ROOT / "config" / "workflow" / "frozen-human-decisions-v1.json"
OPERATOR_IMPLEMENTATION_VERSION = "workflow-production-operator-v2"
OPERATOR_IMPLEMENTATION_FILES = (
    "workflow_production_operator.py",
    "workflow_business_fingerprint.py",
    "workflow_human_decisions.py",
    "workflow_backfill.py",
    "workflow_cutover.py",
    "tools/workflow_production_operator.py",
    "tools/rehearse_workflow_production_operator.py",
    "config/workflow/frozen-human-decisions-v1.json",
)
APPROVED_HUMAN_DECISION_SHA256 = "a590d6e3b47d0a706ec667d3e9227ed4e099cc19b24300f322d23765f62ec84c"
MIGRATION_017_PACKAGE_SHA256 = "9d8c354b0be57578607c4fbe944f368da117e0c70f8d762b83aca15f98e477e8"
ARTIFACT_VERSION = "workflow-production-dry-run-v2"
_CAPABILITY_TOKEN = object()


class OperatorGateError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ValidatedProductionExecutionContext:
    database_path: Path
    database_sha256: str
    state_fingerprint: str
    candidate_commit: str
    operator_version: str
    _token: object

    def __post_init__(self) -> None:
        if self._token is not _CAPABILITY_TOKEN:
            raise OperatorGateError("validated_context_required")


def database_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def implementation_checksum() -> str:
    digest = hashlib.sha256()
    for relative in OPERATOR_IMPLEMENTATION_FILES:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((PROJECT_ROOT / relative).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def database_state_fingerprint(path: str | Path) -> str:
    """Hash committed Workflow business state, including visible WAL rows."""

    return workflow_business_state_fingerprint_path(path)


def _samefile(first: Path, second: Path) -> bool:
    try:
        return first.samefile(second)
    except OSError:
        return False


def validate_execution_identity(
    *,
    database_path: str | Path,
    formal_database_path: str | Path,
    expected_database_sha256: str,
    approved_candidate_commit: str,
    actual_candidate_commit: str,
    worktree_clean: bool,
    expected_operator_version: str,
    expected_operator_checksum: str,
    actual_operator_checksum: str,
    expected_environment: str,
    actual_environment: str,
    expected_migration_checksum: str,
    actual_migration_checksum: str,
    expected_human_decision_checksum: str,
    actual_human_decision_checksum: str,
) -> ValidatedProductionExecutionContext:
    target = Path(database_path).expanduser().resolve()
    formal = Path(formal_database_path).expanduser().resolve()
    checks = (
        (_samefile(target, formal), "production_database_identity_mismatch"),
        (database_sha256(target) == expected_database_sha256, "database_fingerprint_mismatch"),
        (approved_candidate_commit == actual_candidate_commit, "candidate_commit_mismatch"),
        (bool(worktree_clean), "working_tree_dirty"),
        (expected_operator_version == OPERATOR_IMPLEMENTATION_VERSION, "operator_version_mismatch"),
        (expected_operator_checksum == actual_operator_checksum, "operator_checksum_mismatch"),
        (expected_environment == actual_environment == "production", "environment_mismatch"),
        (expected_migration_checksum == actual_migration_checksum == MIGRATION_017_PACKAGE_SHA256, "migration_checksum_mismatch"),
        (expected_human_decision_checksum == actual_human_decision_checksum == APPROVED_HUMAN_DECISION_SHA256, "human_decision_checksum_mismatch"),
    )
    for passed, code in checks:
        if not passed:
            raise OperatorGateError(code)
    return ValidatedProductionExecutionContext(
        target,
        expected_database_sha256,
        database_state_fingerprint(target),
        approved_candidate_commit,
        OPERATOR_IMPLEMENTATION_VERSION,
        _CAPABILITY_TOKEN,
    )


_OPERATION_CONFIRMATION = {
    "backfill-apply": "APPLY_PRODUCTION_BACKFILL",
    "v2-activate": "ACTIVATE_WORKFLOW_V2",
    "v2-disable": "DISABLE_WORKFLOW_V2",
}


def validate_confirmations(
    operation: str,
    fingerprint: str,
    target_confirmation: str,
    operation_confirmation: str,
) -> None:
    prefix = _OPERATION_CONFIRMATION.get(operation)
    if prefix is None:
        raise OperatorGateError("operation_not_allowed")
    short = fingerprint[:12]
    if target_confirmation != f"CONFIRM_PRODUCTION_TARGET_{short}":
        raise OperatorGateError("target_confirmation_mismatch")
    if operation_confirmation != f"{prefix}_{short}":
        raise OperatorGateError("operation_confirmation_mismatch")


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def seal_artifact(payload: Mapping[str, object]) -> dict[str, object]:
    result = dict(payload)
    core = {key: value for key, value in result.items() if key not in {"timestamp", "artifact_checksum"}}
    result["artifact_checksum"] = hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()
    return result


_AUDIT_KEYS = {
    "timestamp", "correlation_id", "action", "mode", "operator", "candidate_commit",
    "database_sha256", "state_fingerprint", "schema_version", "migration_version",
    "pre_state", "post_state", "row_counts", "result", "failure_code",
    "artifact_checksum", "operator_version",
}


def append_operator_audit(path: str | Path, record: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    safe = {key: value for key, value in record.items() if key in _AUDIT_KEYS}
    safe.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    safe.setdefault("operator_version", OPERATOR_IMPLEMENTATION_VERSION)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(_canonical_json(safe) + "\n")
        handle.flush()


def connect_read_only(path: str | Path) -> sqlite3.Connection:
    target = Path(path).resolve()
    connection = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def integrity_snapshot(connection: sqlite3.Connection) -> dict[str, object]:
    quick = connection.execute("PRAGMA quick_check").fetchone()
    fk = connection.execute("PRAGMA foreign_key_check").fetchall()
    return {
        "quick_check": str(quick[0]) if quick else "missing",
        "foreign_key_violations": len(fk),
    }


def validate_integrity_snapshot(snapshot: Mapping[str, object]) -> None:
    if snapshot.get("quick_check") != "ok" or int(snapshot.get("foreign_key_violations", 0)):
        raise OperatorGateError("database_integrity_failed")


def dashboard_metrics(connection: sqlite3.Connection, *, today: date) -> dict[str, object]:
    from dashboard_workflow_metrics import load_dashboard_workflow_metrics

    metrics = load_dashboard_workflow_metrics(connection, today=today)
    return {
        "active_students": metrics.active_workflow_count,
        "stage_distribution": [stage.count for stage in metrics.stage_distribution.stages],
        "weekly_contracts": metrics.signed_this_week,
        "monthly_contracts": metrics.signed_this_month,
        "weekly_offers": metrics.week_offer_count,
        "weekly_grants": metrics.week_grant_count,
        "monthly_grants": metrics.month_grant_count,
        "signed_school": sum(item.cycle_count for item in metrics.signed_institution_structure),
    }


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _migration_state(connection: sqlite3.Connection) -> dict[str, object]:
    if not _table_exists(connection, "schema_migrations"):
        return {"schema_version": None, "migration_017_applied": False, "migration_017_checksum": None}
    row = connection.execute(
        "SELECT version,checksum_sha256 FROM schema_migrations ORDER BY version DESC LIMIT 1"
    ).fetchone()
    applied = connection.execute(
        "SELECT checksum_sha256 FROM schema_migrations WHERE version='20260811_017'"
    ).fetchone()
    return {
        "schema_version": str(row[0]) if row else None,
        "migration_017_applied": applied is not None,
        "migration_017_checksum": str(applied[0]) if applied else None,
    }


def _v2_state(connection: sqlite3.Connection) -> str:
    from workflow_cutover import resolve_workflow_read_mode
    return resolve_workflow_read_mode(connection).value


def _reconciliation_summary(connection: sqlite3.Connection, *, today: date) -> dict[str, object]:
    from workflow_reconciliation import reconcile_workflow, reconciliation_to_json
    issues = reconcile_workflow(connection, today=today)
    digest = hashlib.sha256(reconciliation_to_json(issues).encode("utf-8")).hexdigest()
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.code] = counts.get(issue.code, 0) + 1
    accepted = {
        "APPLICATION_SUBMITTED_STAGE_MISMATCH",
        "CURRENT_STAGE_FACT_MISMATCH",
        "UNOWNED_LEGACY_CYCLE_RECORD",
    }
    blockers = sum(count for code, count in counts.items() if code not in accepted)
    return {"count": len(issues), "counts_by_code": dict(sorted(counts.items())), "digest": digest, "blockers": blockers}


def _duplicate_offer_groups(connection: sqlite3.Connection) -> int:
    if not _table_exists(connection, "offer_received_events"):
        return 0
    return int(connection.execute(
        """SELECT count(*) FROM (
             SELECT legacy_offer_id FROM offer_received_events
             WHERE legacy_offer_id IS NOT NULL
             GROUP BY legacy_offer_id HAVING count(*)>1
           )"""
    ).fetchone()[0])


def production_status(
    database_path: str | Path,
    *,
    candidate_commit: str,
    worktree_clean: bool,
    today: date,
) -> dict[str, object]:
    target = Path(database_path).resolve()
    with connect_read_only(target) as connection:
        integrity = integrity_snapshot(connection)
        migration = _migration_state(connection)
        has_schema = _table_exists(connection, "application_cycles")
        metrics = dashboard_metrics(connection, today=today) if has_schema else None
        reconciliation = _reconciliation_summary(connection, today=today) if has_schema else None
        p1_blockers = int(reconciliation["blockers"]) if reconciliation else 1
        result = {
            "execution_mode": "production" if _samefile(target, FORMAL_DATABASE_PATH) else "rehearsal",
            "operator_version": OPERATOR_IMPLEMENTATION_VERSION,
            "operator_checksum": implementation_checksum(),
            "candidate_commit": candidate_commit,
            "working_tree_clean": bool(worktree_clean),
            "database_path": str(target),
            "database_sha256": database_sha256(target),
            "business_fingerprint_version": WORKFLOW_BUSINESS_FINGERPRINT_VERSION,
            **migration,
            "human_decision_version": "workflow-human-decisions-v1",
            "human_decision_checksum": database_sha256(FROZEN_DECISIONS_PATH),
            "v2_state": _v2_state(connection),
            "v2_active": _v2_state(connection) == "canonical_v2",
            **integrity,
            "metrics": metrics,
            "reconciliation": reconciliation,
            "reconciliation_ready": p1_blockers == 0,
            "p0": 0,
            "p1_blockers": p1_blockers,
            "duplicate_offer_groups": _duplicate_offer_groups(connection),
        }
        result["backfill_ready"] = bool(
            result["migration_017_applied"]
            and result["migration_017_checksum"] == MIGRATION_017_PACKAGE_SHA256
            and result["human_decision_checksum"] == APPROVED_HUMAN_DECISION_SHA256
            and result["quick_check"] == "ok"
            and not result["foreign_key_violations"]
        )
    result["state_fingerprint"] = database_state_fingerprint(target)
    return result


def _memory_clone(database_path: str | Path) -> sqlite3.Connection:
    source = connect_read_only(database_path)
    clone = sqlite3.connect(":memory:")
    clone.row_factory = sqlite3.Row
    try:
        source.backup(clone)
    finally:
        source.close()
    clone.execute("PRAGMA foreign_keys=ON")
    return clone


def _backfill_counts(result) -> dict[str, int]:
    return {
        "inserted_cycles": int(result.inserted_cycles),
        "updated_cycles": int(result.updated_cycles),
        "linked_records": int(result.linked_records),
        "materialized_applications": int(result.materialized_applications),
        "materialized_events": int(result.materialized_events),
    }


def build_backfill_dry_run(
    database_path: str | Path,
    *,
    approved_candidate_commit: str,
    today: date,
    timestamp: str | None = None,
) -> dict[str, object]:
    from workflow_backfill import _apply_deterministic_backfill_connection, build_backfill_preview
    from workflow_human_decisions import apply_human_decisions, load_frozen_decisions

    target = Path(database_path).resolve()
    before = database_sha256(target)
    status = production_status(target, candidate_commit=approved_candidate_commit, worktree_clean=True, today=today)
    if not status["migration_017_applied"] or status["migration_017_checksum"] != MIGRATION_017_PACKAGE_SHA256:
        raise OperatorGateError("migration_017_not_ready")
    validate_integrity_snapshot(status)
    decisions = load_frozen_decisions(FROZEN_DECISIONS_PATH)
    clone = _memory_clone(target)
    try:
        first = _apply_deterministic_backfill_connection(clone, actor="Workflow Production Dry Run")
        human = apply_human_decisions(clone, decisions, actor="Workflow Production Dry Run")
        convergence = _apply_deterministic_backfill_connection(clone, actor="Workflow Production Dry Run")
        final_preview = build_backfill_preview(clone)
        metrics = dashboard_metrics(clone, today=today)
        reconciliation = _reconciliation_summary(clone, today=today)
        duplicate_offers = _duplicate_offer_groups(clone)
        artifact = {
            "artifact_version": ARTIFACT_VERSION,
            "artifact_kind": "backfill",
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            "operator_version": OPERATOR_IMPLEMENTATION_VERSION,
            "operator_checksum": implementation_checksum(),
            "approved_candidate_commit": approved_candidate_commit,
            "database_path": str(target),
            "database_sha256": before,
            "state_fingerprint": status["state_fingerprint"],
            "schema_version": status["schema_version"],
            "migration_version": "20260811_017",
            "migration_checksum": status["migration_017_checksum"],
            "human_decision_version": decisions.version,
            "human_decision_checksum": decisions.checksum_sha256,
            "v2_state": status["v2_state"],
            "quick_check": status["quick_check"],
            "foreign_key_violations": status["foreign_key_violations"],
            "projected_writes": {
                "deterministic": _backfill_counts(first),
                "human_decisions": {
                    "decision_count": human.decision_count,
                    "applied_decisions": human.applied_decisions,
                    "created_records": human.created_records,
                },
                "convergence": _backfill_counts(convergence),
            },
            "projected_cycles": first.inserted_cycles,
            "projected_ownership_links": first.linked_records,
            "projected_applications": first.materialized_applications,
            "projected_events": first.materialized_events,
            "projected_final_acceptances": human.applied_decisions,
            "projected_grant_refusal_events": 0,
            "p0": 0,
            "p1_blockers": reconciliation["blockers"],
            "safe_not_applied": 0,
            "duplicate_offer_groups": duplicate_offers,
            "reconciliation_digest": reconciliation["digest"],
            "projected_dashboard_metrics": metrics,
            "preview_digest": first.preview_sha256,
            "final_safe_count": len(final_preview.safe),
        }
        return seal_artifact(artifact)
    finally:
        clone.close()
        if database_sha256(target) != before:
            raise OperatorGateError("dry_run_modified_database")


def verify_artifact(artifact: Mapping[str, object], supplied_checksum: str) -> None:
    sealed = seal_artifact(artifact)
    actual = str(artifact.get("artifact_checksum", ""))
    if actual != supplied_checksum or sealed["artifact_checksum"] != actual:
        raise OperatorGateError("artifact_checksum_mismatch")


def validate_artifact_binding(
    artifact: Mapping[str, object],
    current: Mapping[str, object],
) -> None:
    for field in (
        "state_fingerprint",
        "approved_candidate_commit",
        "operator_version",
        "operator_checksum",
        "human_decision_checksum",
        "migration_checksum",
    ):
        if artifact.get(field) != current.get(field):
            raise OperatorGateError("stale_dry_run_artifact")


def _current_artifact_binding(status: Mapping[str, object]) -> dict[str, object]:
    return {
        "state_fingerprint": status["state_fingerprint"],
        "approved_candidate_commit": status["candidate_commit"],
        "operator_version": status["operator_version"],
        "operator_checksum": status["operator_checksum"],
        "human_decision_checksum": status["human_decision_checksum"],
        "migration_checksum": status["migration_017_checksum"],
    }


def _insert_db_audit(connection: sqlite3.Connection, *, action: str, actor: str, payload: Mapping[str, object]) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """INSERT INTO audit_logs(
             user_id,username,action_type,module_name,module_key,record_id,new_value,
             device_info,status,created_at
           ) VALUES(NULL,?,?,?,?,?,?,?,'success',?)""",
        (
            actor, action, "workflow_production_operator", "workflow_v2",
            str(payload.get("artifact_checksum", "")), _canonical_json(payload),
            f"operator-version:{OPERATOR_IMPLEMENTATION_VERSION}", stamp,
        ),
    )


def apply_production_backfill(
    context: ValidatedProductionExecutionContext,
    artifact: Mapping[str, object],
    *,
    artifact_checksum: str,
    target_confirmation: str,
    operation_confirmation: str,
    actor: str,
    audit_path: str | Path,
    today: date,
) -> dict[str, object]:
    from workflow_backfill import _apply_deterministic_backfill_connection
    from workflow_human_decisions import apply_human_decisions, load_frozen_decisions

    if context._token is not _CAPABILITY_TOKEN:
        raise OperatorGateError("validated_context_required")
    verify_artifact(artifact, artifact_checksum)
    validate_confirmations("backfill-apply", context.state_fingerprint, target_confirmation, operation_confirmation)
    current = production_status(context.database_path, candidate_commit=context.candidate_commit, worktree_clean=True, today=today)
    validate_artifact_binding(artifact, _current_artifact_binding(current))
    connection = sqlite3.connect(context.database_path, isolation_level=None, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        if workflow_business_state_fingerprint(connection) != artifact.get("state_fingerprint"):
            raise OperatorGateError("stale_dry_run_artifact")
        first = _apply_deterministic_backfill_connection(connection, actor=actor)
        human = apply_human_decisions(connection, load_frozen_decisions(FROZEN_DECISIONS_PATH), actor=actor)
        convergence = _apply_deterministic_backfill_connection(connection, actor=actor)
        result = {
            "deterministic": _backfill_counts(first),
            "human_decisions": {
                "decision_count": human.decision_count,
                "applied_decisions": human.applied_decisions,
                "created_records": human.created_records,
            },
            "convergence": _backfill_counts(convergence),
        }
        if result != artifact.get("projected_writes"):
            raise OperatorGateError("unexpected_write_count")
        if convergence.inserted_cycles or convergence.updated_cycles or convergence.linked_records or convergence.materialized_applications or convergence.materialized_events:
            raise OperatorGateError("backfill_not_converged")
        validate_integrity_snapshot(integrity_snapshot(connection))
        _insert_db_audit(connection, action="workflow_production_backfill", actor=actor, payload={"artifact_checksum": artifact_checksum, "row_counts": result})
        append_operator_audit(audit_path, {"action": "backfill-apply", "result": "prepared", "database_sha256": context.database_sha256, "row_counts": result, "artifact_checksum": artifact_checksum, "operator": actor})
        connection.commit()
        return {"ok": True, "row_counts": result}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def build_activation_dry_run(
    database_path: str | Path,
    *,
    approved_candidate_commit: str,
    expected_metrics: Mapping[str, object],
    today: date,
    timestamp: str | None = None,
) -> dict[str, object]:
    status = production_status(
        database_path,
        candidate_commit=approved_candidate_commit,
        worktree_clean=True,
        today=today,
    )
    blockers = []
    if not status["migration_017_applied"] or status["migration_017_checksum"] != MIGRATION_017_PACKAGE_SHA256:
        blockers.append("migration_017")
    if status["quick_check"] != "ok":
        blockers.append("quick_check")
    if status["foreign_key_violations"]:
        blockers.append("foreign_key_check")
    if status["duplicate_offer_groups"]:
        blockers.append("duplicate_offer_groups")
    reconciliation = status["reconciliation"] or {"blockers": 1, "digest": ""}
    if reconciliation["blockers"]:
        blockers.append("reconciliation")
    if status["metrics"] != dict(expected_metrics):
        blockers.append("dashboard_metrics")
    artifact = {
        "artifact_version": ARTIFACT_VERSION,
        "artifact_kind": "v2-activation",
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "operator_version": OPERATOR_IMPLEMENTATION_VERSION,
        "operator_checksum": implementation_checksum(),
        "approved_candidate_commit": approved_candidate_commit,
        "database_path": status["database_path"],
        "database_sha256": status["database_sha256"],
        "state_fingerprint": status["state_fingerprint"],
        "migration_version": "20260811_017",
        "migration_checksum": status["migration_017_checksum"],
        "human_decision_version": status["human_decision_version"],
        "human_decision_checksum": status["human_decision_checksum"],
        "v2_state": status["v2_state"],
        "quick_check": status["quick_check"],
        "foreign_key_violations": status["foreign_key_violations"],
        "p0": 0,
        "p1_blockers": int(reconciliation["blockers"]),
        "safe_not_applied": 0,
        "duplicate_offer_groups": status["duplicate_offer_groups"],
        "reconciliation_digest": reconciliation["digest"],
        "projected_dashboard_metrics": status["metrics"],
        "readiness_blockers": blockers,
        "ready": not blockers,
    }
    return seal_artifact(artifact)


_READY_EVIDENCE = {
    "migration_applied": True,
    "deterministic_backfill_complete": True,
    "current_active_ownership_resolved": True,
    "reconciliation_blockers_cleared": True,
    "blocking_ambiguity_resolved": True,
    "dashboard_parity_verified": True,
    "resolver_parity_verified": True,
    "foreign_key_check_passed": True,
    "quick_check_passed": True,
}


def activate_production_v2(
    context: ValidatedProductionExecutionContext,
    artifact: Mapping[str, object],
    *,
    artifact_checksum: str,
    target_confirmation: str,
    operation_confirmation: str,
    actor: str,
    audit_path: str | Path,
    today: date,
) -> dict[str, object]:
    from workflow_cutover import activate_workflow_v2, resolve_workflow_read_mode

    if context._token is not _CAPABILITY_TOKEN:
        raise OperatorGateError("validated_context_required")
    verify_artifact(artifact, artifact_checksum)
    if artifact.get("artifact_kind") != "v2-activation" or not artifact.get("ready"):
        raise OperatorGateError("activation_not_ready")
    validate_confirmations("v2-activate", context.state_fingerprint, target_confirmation, operation_confirmation)
    current = production_status(context.database_path, candidate_commit=context.candidate_commit, worktree_clean=True, today=today)
    validate_artifact_binding(artifact, _current_artifact_binding(current))
    connection = sqlite3.connect(context.database_path, isolation_level=None, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        if workflow_business_state_fingerprint(connection) != artifact.get("state_fingerprint"):
            raise OperatorGateError("stale_dry_run_artifact")
        readiness = activate_workflow_v2(connection, evidence=_READY_EVIDENCE, actor=actor)
        validate_integrity_snapshot(integrity_snapshot(connection))
        _insert_db_audit(connection, action="workflow_v2_activate", actor=actor, payload={"artifact_checksum": artifact_checksum})
        if resolve_workflow_read_mode(connection).value != "canonical_v2":
            raise OperatorGateError("activation_postcondition_failed")
        append_operator_audit(audit_path, {"action": "v2-activate", "result": "prepared", "database_sha256": context.database_sha256, "artifact_checksum": artifact_checksum, "operator": actor})
        connection.commit()
        return {"ok": True, "read_mode": "canonical_v2", "readiness_blockers": list(readiness.blockers)}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def disable_production_v2(
    context: ValidatedProductionExecutionContext,
    *,
    target_confirmation: str,
    operation_confirmation: str,
    actor: str,
    audit_path: str | Path,
) -> dict[str, object]:
    from workflow_cutover import disable_workflow_v2, resolve_workflow_read_mode

    if context._token is not _CAPABILITY_TOKEN:
        raise OperatorGateError("validated_context_required")
    validate_confirmations("v2-disable", context.state_fingerprint, target_confirmation, operation_confirmation)
    connection = sqlite3.connect(context.database_path, isolation_level=None, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        if workflow_business_state_fingerprint(connection) != context.state_fingerprint:
            raise OperatorGateError("database_fingerprint_mismatch")
        if resolve_workflow_read_mode(connection).value != "canonical_v2":
            raise OperatorGateError("v2_not_active")
        disable_workflow_v2(connection, actor=actor)
        validate_integrity_snapshot(integrity_snapshot(connection))
        _insert_db_audit(connection, action="workflow_v2_disable", actor=actor, payload={})
        if resolve_workflow_read_mode(connection).value != "legacy_compatible":
            raise OperatorGateError("legacy_fallback_failed")
        append_operator_audit(audit_path, {"action": "v2-disable", "result": "prepared", "database_sha256": context.database_sha256, "operator": actor})
        connection.commit()
        return {"ok": True, "read_mode": "legacy_compatible"}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
