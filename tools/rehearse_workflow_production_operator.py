#!/usr/bin/env python3
"""Run the approved 14-step operator flow on a new isolated snapshot copy."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import shutil
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from migration_runner import apply_migrations
import workflow_production_operator as operator


def _context(database: Path, candidate_commit: str) -> operator.ValidatedProductionExecutionContext:
    """Test-only formal-path injection, guarded against the compiled formal DB."""

    fingerprint = operator.database_sha256(database)
    return operator.validate_execution_identity(
        database_path=database,
        formal_database_path=database,
        expected_database_sha256=fingerprint,
        approved_candidate_commit=candidate_commit,
        actual_candidate_commit=candidate_commit,
        worktree_clean=True,
        expected_operator_version=operator.OPERATOR_IMPLEMENTATION_VERSION,
        expected_operator_checksum=operator.implementation_checksum(),
        actual_operator_checksum=operator.implementation_checksum(),
        expected_environment="production",
        actual_environment="production",
        expected_migration_checksum=operator.MIGRATION_017_PACKAGE_SHA256,
        actual_migration_checksum=operator.MIGRATION_017_PACKAGE_SHA256,
        expected_human_decision_checksum=operator.APPROVED_HUMAN_DECISION_SHA256,
        actual_human_decision_checksum=operator.database_sha256(operator.FROZEN_DECISIONS_PATH),
    )


def _confirmations(action: str, context: operator.ValidatedProductionExecutionContext) -> tuple[str, str]:
    short = context.state_fingerprint[:12]
    operation = {
        "backfill-apply": "APPLY_PRODUCTION_BACKFILL",
        "v2-activate": "ACTIVATE_WORKFLOW_V2",
        "v2-disable": "DISABLE_WORKFLOW_V2",
    }[action]
    return f"CONFIRM_PRODUCTION_TARGET_{short}", f"{operation}_{short}"


def _status(database: Path, candidate_commit: str, today: date) -> dict[str, object]:
    return operator.production_status(
        database,
        candidate_commit=candidate_commit,
        worktree_clean=True,
        today=today,
    )


def run_rehearsal(
    source_database: str | Path,
    target_database: str | Path,
    *,
    candidate_commit: str,
    expected_metrics: dict[str, object],
    audit_path: str | Path,
    today: date,
) -> dict[str, object]:
    source = Path(source_database).resolve()
    target = Path(target_database).resolve()
    if not source.is_file():
        raise operator.OperatorGateError("rehearsal_source_missing")
    if target.exists():
        raise operator.OperatorGateError("rehearsal_target_must_not_exist")
    if source == operator.FORMAL_DATABASE_PATH or target == operator.FORMAL_DATABASE_PATH:
        raise operator.OperatorGateError("production_database_forbidden_in_rehearsal")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)

    steps: list[dict[str, object]] = []
    initial_sha = operator.database_sha256(target)
    initial = _status(target, candidate_commit, today)
    steps.append({"step": 1, "name": "status", "v2_state": initial["v2_state"]})
    steps.append({"step": 2, "name": "migration_readiness", "migration_017_applied": initial["migration_017_applied"]})

    migration = apply_migrations(
        target,
        ROOT / "migrations",
        applied_by="Workflow Operator Rehearsal",
        source_commit=candidate_commit,
        formal_database_path=None,
    )
    steps.append({"step": 3, "name": "migration_017", "applied": migration["applied"]})

    before_dry = operator.database_sha256(target)
    artifact = operator.build_backfill_dry_run(
        target, approved_candidate_commit=candidate_commit, today=today,
    )
    if operator.database_sha256(target) != before_dry:
        raise operator.OperatorGateError("dry_run_modified_database")
    steps.append({"step": 4, "name": "backfill_dry_run", "database_unchanged": True, "artifact_checksum": artifact["artifact_checksum"]})

    context = _context(target, candidate_commit)
    first_confirms = _confirmations("backfill-apply", context)
    first = operator.apply_production_backfill(
        context, artifact,
        artifact_checksum=str(artifact["artifact_checksum"]),
        target_confirmation=first_confirms[0], operation_confirmation=first_confirms[1],
        actor="Workflow Operator Rehearsal", audit_path=audit_path, today=today,
    )
    steps.append({"step": 5, "name": "backfill_apply", "row_counts": first["row_counts"]})

    second_artifact = operator.build_backfill_dry_run(
        target, approved_candidate_commit=candidate_commit, today=today,
    )
    context = _context(target, candidate_commit)
    second_confirms = _confirmations("backfill-apply", context)
    second = operator.apply_production_backfill(
        context, second_artifact,
        artifact_checksum=str(second_artifact["artifact_checksum"]),
        target_confirmation=second_confirms[0], operation_confirmation=second_confirms[1],
        actor="Workflow Operator Rehearsal", audit_path=audit_path, today=today,
    )
    steps.append({"step": 6, "name": "backfill_second_apply", "row_counts": second["row_counts"]})

    reconciled = _status(target, candidate_commit, today)
    steps.append({"step": 7, "name": "reconciliation", "p0": reconciled["p0"], "p1_blockers": reconciled["p1_blockers"]})

    activation = operator.build_activation_dry_run(
        target, approved_candidate_commit=candidate_commit,
        expected_metrics=expected_metrics, today=today,
    )
    steps.append({"step": 8, "name": "v2_activation_dry_run", "ready": activation["ready"]})
    context = _context(target, candidate_commit)
    activate_confirms = _confirmations("v2-activate", context)
    activated = operator.activate_production_v2(
        context, activation,
        artifact_checksum=str(activation["artifact_checksum"]),
        target_confirmation=activate_confirms[0], operation_confirmation=activate_confirms[1],
        actor="Workflow Operator Rehearsal", audit_path=audit_path, today=today,
    )
    steps.append({"step": 9, "name": "v2_activate", "read_mode": activated["read_mode"]})
    canonical = _status(target, candidate_commit, today)
    steps.append({"step": 10, "name": "canonical_smoke", "metrics_match": canonical["metrics"] == expected_metrics})

    context = _context(target, candidate_commit)
    disable_confirms = _confirmations("v2-disable", context)
    disabled = operator.disable_production_v2(
        context,
        target_confirmation=disable_confirms[0], operation_confirmation=disable_confirms[1],
        actor="Workflow Operator Rehearsal", audit_path=audit_path,
    )
    steps.append({"step": 11, "name": "v2_disable", "read_mode": disabled["read_mode"]})
    legacy = _status(target, candidate_commit, today)
    steps.append({"step": 12, "name": "legacy_compatible_smoke", "read_mode": legacy["v2_state"]})

    reactivation = operator.build_activation_dry_run(
        target, approved_candidate_commit=candidate_commit,
        expected_metrics=expected_metrics, today=today,
    )
    steps.append({"step": 13, "name": "v2_activation_dry_run_again", "ready": reactivation["ready"]})
    context = _context(target, candidate_commit)
    reactivate_confirms = _confirmations("v2-activate", context)
    reactivated = operator.activate_production_v2(
        context, reactivation,
        artifact_checksum=str(reactivation["artifact_checksum"]),
        target_confirmation=reactivate_confirms[0], operation_confirmation=reactivate_confirms[1],
        actor="Workflow Operator Rehearsal", audit_path=audit_path, today=today,
    )
    final = _status(target, candidate_commit, today)
    steps.append({"step": 14, "name": "v2_reactivate_and_metrics", "read_mode": reactivated["read_mode"], "metrics_match": final["metrics"] == expected_metrics})

    zero_counts = second["row_counts"]
    zero_write = all(
        value == 0
        for group in (zero_counts["deterministic"], zero_counts["convergence"])
        for value in group.values()
    ) and zero_counts["human_decisions"]["applied_decisions"] == 0 and zero_counts["human_decisions"]["created_records"] == 0
    with sqlite3.connect(target) as connection:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        fk_count = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        db_audit_count = connection.execute(
            "SELECT count(*) FROM audit_logs WHERE module_name='workflow_production_operator'"
        ).fetchone()[0]
    return {
        "ok": len(steps) == 14 and activation["ready"] and reactivation["ready"]
        and canonical["metrics"] == expected_metrics and final["metrics"] == expected_metrics
        and zero_write and quick == "ok" and fk_count == 0,
        "source_sha256": initial_sha,
        "target": str(target),
        "steps": steps,
        "first_apply_counts": first["row_counts"],
        "second_apply_zero_writes": zero_write,
        "final_metrics": final["metrics"],
        "p0": final["p0"],
        "p1_blockers": final["p1_blockers"],
        "quick_check": quick,
        "foreign_key_violations": fk_count,
        "database_audit_records": db_audit_count,
        "file_audit_records": len(Path(audit_path).read_text(encoding="utf-8").splitlines()),
        "final_v2_state": final["v2_state"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--expected-metrics", required=True)
    parser.add_argument("--audit-output", required=True)
    args = parser.parse_args(argv)
    expected = json.loads(Path(args.expected_metrics).read_text(encoding="utf-8"))
    result = run_rehearsal(
        args.source, args.target,
        candidate_commit=args.candidate_commit,
        expected_metrics=expected,
        audit_path=args.audit_output,
        today=date.today(),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
