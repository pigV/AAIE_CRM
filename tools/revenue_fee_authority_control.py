#!/usr/bin/env python3
"""Controlled Fee Rule initialization and onboarding proposal command.

Production mutation stays locked unless a later, separate user approval is
represented by the exact confirmation phrase. Preview is always read-only and
proposal files are created with non-overwrite semantics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from revenue.client_fee_rules import FeeRuleAuthorityService  # noqa: E402
from revenue.controlled_onboarding import ControlledOnboardingService  # noqa: E402
from revenue.repository import RevenueRepository, canonical_json  # noqa: E402


CANONICAL_PRODUCTION_DB = Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db")
PRODUCTION_CONFIRMATION = "I_HAVE_SEPARATE_USER_APPROVAL_FOR_PRODUCTION_MUTATION"


def _inside_workspace(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT.resolve())
        return True
    except ValueError:
        return False


def _require_workspace_output(path: Path):
    if not _inside_workspace(path):
        raise ValueError("proposal/evidence output must stay inside the isolated CRM worktree")


def _is_production(path: Path) -> bool:
    return path.resolve() == CANONICAL_PRODUCTION_DB.resolve()


def _require_mutation_authority(path: Path, confirmation: str | None):
    if _is_production(path) and confirmation != PRODUCTION_CONFIRMATION:
        raise PermissionError(
            "Production mutation is locked pending separate user approval and controlled writer maintenance"
        )


def _require_exact_admin(connection: sqlite3.Connection, user_id: int) -> str:
    row = connection.execute(
        """SELECT r.name role_name,u.is_active,u.deleted_at
             FROM users u JOIN roles r ON r.id=u.role_id
            WHERE u.id=?""",
        (int(user_id),),
    ).fetchone()
    if (
        row is None
        or str(row["role_name"]) != "Admin"
        or int(row["is_active"] or 0) != 1
        or row["deleted_at"] is not None
    ):
        raise PermissionError("command requires an exact active Admin database identity")
    return "Admin"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_only_connection(path: Path):
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def initialize(args) -> dict:
    database = Path(args.database).resolve()
    _require_mutation_authority(database, args.confirm_production_authority)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor_role = _require_exact_admin(connection, args.admin_user_id)
        result = FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
            actor_user_id=args.admin_user_id,
            actor_role=actor_role,
            idempotency_key=args.idempotency_key,
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def preview(args) -> dict:
    database = Path(args.database).resolve()
    output = Path(args.output).resolve()
    _require_workspace_output(output)
    metadata = {
        "source": args.source_label,
        "database_filename": database.name,
        "database_size_bytes": database.stat().st_size,
        "source_sha256": _sha256_file(database) if args.hash_source else None,
    }
    connection = _read_only_connection(database)
    try:
        actor_role = _require_exact_admin(connection, args.admin_user_id)
        proposal = ControlledOnboardingService(RevenueRepository(connection)).preview(
            actor_user_id=args.admin_user_id,
            actor_role=actor_role,
            source_snapshot_metadata=metadata,
        )
    finally:
        connection.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(canonical_json(proposal))
        handle.write("\n")
    return {
        "proposal_uid": proposal["proposal_uid"],
        "proposal_sha256": proposal["proposal_sha256"],
        "output": str(output),
        "summary": proposal["summary"],
    }


def apply(args) -> dict:
    database = Path(args.database).resolve()
    proposal_path = Path(args.proposal).resolve()
    _require_mutation_authority(database, args.confirm_production_authority)
    if not _inside_workspace(proposal_path):
        raise ValueError("approved proposal must be read from the isolated CRM worktree")
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor_role = _require_exact_admin(connection, args.admin_user_id)
        result = ControlledOnboardingService(RevenueRepository(connection)).apply(
            proposal,
            actor_user_id=args.admin_user_id,
            actor_role=actor_role,
            idempotency_key=args.idempotency_key,
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subcommands = result.add_subparsers(dest="command", required=True)
    for name, handler in (("initialize", initialize), ("apply", apply)):
        command = subcommands.add_parser(name)
        command.add_argument("--database", required=True)
        command.add_argument("--admin-user-id", type=int, required=True)
        command.add_argument("--idempotency-key", required=True)
        command.add_argument("--confirm-production-authority")
        command.set_defaults(handler=handler)
    proposal = subcommands.add_parser("preview")
    proposal.add_argument("--database", required=True)
    proposal.add_argument("--output", required=True)
    proposal.add_argument("--admin-user-id", type=int, required=True)
    proposal.add_argument("--source-label", default="production-derived-isolated")
    proposal.add_argument("--hash-source", action="store_true")
    proposal.set_defaults(handler=preview)
    subcommands.choices["apply"].add_argument("--proposal", required=True)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    result = args.handler(args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
