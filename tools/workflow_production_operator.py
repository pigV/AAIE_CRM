#!/usr/bin/env python3
"""Unified JSON CLI for guarded Workflow Production operations."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import workflow_production_operator as operator


class StableJsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, json.dumps({"ok": False, "error": "invalid_arguments"}, sort_keys=True) + "\n")


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        capture_output=True, check=True,
    ).stdout.strip()


def _git_clean() -> bool:
    return not subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=ROOT, text=True, capture_output=True, check=True,
    ).stdout.strip()


def _migration_checksum(database: Path) -> str:
    with operator.connect_read_only(database) as connection:
        row = connection.execute(
            "SELECT checksum_sha256 FROM schema_migrations WHERE version='20260811_017'"
        ).fetchone()
    return str(row[0]) if row else ""


def _add_common(parser: argparse.ArgumentParser, *, artifact_output: bool = False) -> None:
    parser.add_argument("--database", required=True)
    parser.add_argument("--approved-candidate-commit", required=True)
    parser.add_argument("--audit-output", required=True)
    if artifact_output:
        parser.add_argument("--artifact-output", required=True)


def _add_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--expected-database-sha256", required=True)
    parser.add_argument("--expected-operator-checksum", required=True)
    parser.add_argument("--expected-environment", required=True, choices=("production",))


def _add_mutation(parser: argparse.ArgumentParser, *, artifact: bool = False) -> None:
    _add_common(parser)
    _add_identity(parser)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--confirm-target", required=True)
    parser.add_argument("--confirm-operation", required=True)
    if artifact:
        parser.add_argument("--artifact", required=True)
        parser.add_argument("--artifact-checksum", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = StableJsonArgumentParser(prog="workflow-production-operator")
    sub = parser.add_subparsers(
        dest="command", required=True, parser_class=StableJsonArgumentParser,
    )
    _add_common(sub.add_parser("status"))
    dry = sub.add_parser("backfill-dry-run")
    _add_common(dry, artifact_output=True)
    _add_identity(dry)
    _add_mutation(sub.add_parser("backfill-apply"), artifact=True)
    activation_dry = sub.add_parser("v2-activation-dry-run")
    _add_common(activation_dry, artifact_output=True)
    _add_identity(activation_dry)
    activation_dry.add_argument("--expected-metrics", required=True)
    _add_mutation(sub.add_parser("v2-activate"), artifact=True)
    _add_mutation(sub.add_parser("v2-disable"))
    return parser


def _context(args) -> operator.ValidatedProductionExecutionContext:
    database = Path(args.database).resolve()
    return operator.validate_execution_identity(
        database_path=database,
        formal_database_path=operator.FORMAL_DATABASE_PATH,
        expected_database_sha256=args.expected_database_sha256,
        approved_candidate_commit=args.approved_candidate_commit,
        actual_candidate_commit=_git_head(),
        worktree_clean=_git_clean(),
        expected_operator_version=operator.OPERATOR_IMPLEMENTATION_VERSION,
        expected_operator_checksum=args.expected_operator_checksum,
        actual_operator_checksum=operator.implementation_checksum(),
        expected_environment=args.expected_environment,
        actual_environment="production" if database == operator.FORMAL_DATABASE_PATH else "rehearsal",
        expected_migration_checksum=operator.MIGRATION_017_PACKAGE_SHA256,
        actual_migration_checksum=_migration_checksum(database),
        expected_human_decision_checksum=operator.APPROVED_HUMAN_DECISION_SHA256,
        actual_human_decision_checksum=operator.database_sha256(operator.FROZEN_DECISIONS_PATH),
    )


def _write_artifact(path: str, artifact: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _audit(args, action: str, result: str, **values) -> None:
    operator.append_operator_audit(
        args.audit_output,
        {"action": action, "result": result, "candidate_commit": getattr(args, "approved_candidate_commit", ""), **values},
    )


def run(args) -> dict[str, object]:
    today = date.today()
    if args.command == "status":
        result = operator.production_status(
            args.database,
            candidate_commit=args.approved_candidate_commit,
            worktree_clean=_git_clean(),
            today=today,
        )
        _audit(args, "status", "success", database_sha256=result["database_sha256"])
        return result
    if args.command == "backfill-dry-run":
        _context(args)
        result = operator.build_backfill_dry_run(
            args.database,
            approved_candidate_commit=args.approved_candidate_commit,
            today=today,
        )
        _write_artifact(args.artifact_output, result)
        _audit(args, args.command, "success", database_sha256=result["database_sha256"], artifact_checksum=result["artifact_checksum"])
        return result
    if args.command == "v2-activation-dry-run":
        _context(args)
        metrics = json.loads(Path(args.expected_metrics).read_text(encoding="utf-8"))
        result = operator.build_activation_dry_run(
            args.database,
            approved_candidate_commit=args.approved_candidate_commit,
            expected_metrics=metrics,
            today=today,
        )
        _write_artifact(args.artifact_output, result)
        _audit(args, args.command, "success", database_sha256=result["database_sha256"], artifact_checksum=result["artifact_checksum"])
        return result
    context = _context(args)
    if args.command in {"backfill-apply", "v2-activate"}:
        artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
        function = operator.apply_production_backfill if args.command == "backfill-apply" else operator.activate_production_v2
        return function(
            context, artifact,
            artifact_checksum=args.artifact_checksum,
            target_confirmation=args.confirm_target,
            operation_confirmation=args.confirm_operation,
            actor=args.operator,
            audit_path=args.audit_output,
            today=today,
        )
    return operator.disable_production_v2(
        context,
        target_confirmation=args.confirm_target,
        operation_confirmation=args.confirm_operation,
        actor=args.operator,
        audit_path=args.audit_output,
    )


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (operator.OperatorGateError, OSError, sqlite3.Error, ValueError) as exc:
        code = exc.code if isinstance(exc, operator.OperatorGateError) else "operator_execution_failed"
        try:
            if getattr(args, "audit_output", None):
                _audit(args, getattr(args, "command", "unknown"), "rejected", failure_code=code)
        except (OSError, ValueError):
            code = "operator_audit_failed"
        print(json.dumps({"ok": False, "error": code}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
