#!/usr/bin/env python3
"""Explicit command-line entry point for the standalone migration runner."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from migration_runner import (  # noqa: E402
    DEFAULT_FORMAL_DATABASE_PATH,
    FORMAL_CONFIRMATION,
    MigrationError,
    MigrationIOError,
    apply_migrations,
    migration_error_from_sqlite,
    migration_status,
    plan_migrations,
    verify_migrations,
)


class MigrationArgumentError(MigrationError):
    """Command-line arguments did not satisfy the explicit runner contract."""

    code = "migration_invalid_arguments"
    public_message = "Migration command arguments are invalid or incomplete."


class MigrationArgumentParser(argparse.ArgumentParser):
    """Argument parser that leaves rendering to the stable CLI error boundary."""

    def error(self, message: str) -> None:
        raise MigrationArgumentError(message)


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database",
        required=True,
        help="Existing SQLite database path; there is no default.",
    )
    parser.add_argument(
        "--migrations-dir",
        required=True,
        help="Directory containing ordered SQL files and checks/.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = MigrationArgumentParser(
        description=(
            "Inspect, plan, apply, or verify explicit SQLite migrations. "
            "The application never runs this command automatically."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command, help_text in (
        ("status", "Show applied and pending migration versions without writes."),
        ("plan", "Show the validated apply plan without writes."),
        ("verify", "Verify registered checksums, order, files, and forward checks."),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        _add_common_arguments(command_parser)
        if command == "plan":
            command_parser.add_argument("--through", help="Exact final migration version; validate full history and plan only this prefix.")

    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply pending migrations transactionally to the explicit database.",
    )
    _add_common_arguments(apply_parser)
    apply_parser.add_argument("--through", help="Exact final migration version; later migrations remain pending. Default: apply all pending.")
    apply_parser.add_argument("--applied-by", required=True)
    apply_parser.add_argument("--source-commit", required=True)
    apply_parser.add_argument(
        "--allow-formal-database",
        action="store_true",
        help="First formal-database apply safety gate.",
    )
    apply_parser.add_argument(
        "--confirm",
        help=f"Formal apply requires the exact value {FORMAL_CONFIRMATION}.",
    )
    apply_parser.add_argument(
        "--expected-database-sha256",
        help="Formal apply requires the current main database file SHA-256.",
    )
    apply_parser.add_argument(
        "--rebuild-backup",
        help="Independent SQLite online backup required by guarded rebuild migrations.",
    )
    apply_parser.add_argument(
        "--rebuild-backup-manifest",
        help="Verified backup manifest required by guarded rebuild migrations.",
    )
    return parser


def _print_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    print(f"database: {result['database']}")
    for key in ("applied", "already_applied", "verified", "pending", "will_apply"):
        if key not in result:
            continue
        values = result[key]
        if values and isinstance(values[0], dict):
            print(f"{key}:")
            for item in values:
                checksum = item.get("checksum_sha256", "")
                name = item.get("name", "")
                print(
                    f"  - {item['version']} {name} "
                    f"checksum={checksum}".rstrip()
                )
        else:
            display = ", ".join(str(item) for item in values)
            print(f"{key}: {display or '-'}")
    if "foreign_key_violations" in result:
        print(
            "foreign_key_violations: "
            f"{len(result['foreign_key_violations'])}"
        )


def _print_error(error: MigrationError, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": error.code,
                        "message": error.public_message,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    print(
        f"migration error [{error.code}]: {error.public_message}",
        file=sys.stderr,
    )


def main(argv=None) -> int:
    parser = build_parser()
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    try:
        args = parser.parse_args(raw_argv)
    except MigrationArgumentError as exc:
        _print_error(exc, "--json" in raw_argv)
        return 2
    try:
        if args.command == "status":
            result = migration_status(args.database, args.migrations_dir)
        elif args.command == "plan":
            result = plan_migrations(args.database, args.migrations_dir, through=args.through)
        elif args.command == "verify":
            result = verify_migrations(args.database, args.migrations_dir)
        else:
            if bool(args.rebuild_backup) != bool(args.rebuild_backup_manifest):
                raise MigrationArgumentError(
                    "rebuild backup and manifest must be supplied together"
                )
            rebuild_backup_metadata = None
            if args.rebuild_backup:
                rebuild_backup_metadata = {
                    "backup_path": args.rebuild_backup,
                    "manifest_path": args.rebuild_backup_manifest,
                }
            result = apply_migrations(
                args.database,
                args.migrations_dir,
                applied_by=args.applied_by,
                source_commit=args.source_commit,
                formal_database_path=DEFAULT_FORMAL_DATABASE_PATH,
                allow_formal_database=args.allow_formal_database,
                confirmation=args.confirm,
                expected_database_sha256=args.expected_database_sha256,
                required_backup_metadata=rebuild_backup_metadata,
                through=args.through,
            )
    except MigrationError as exc:
        _print_error(exc, args.json)
        return 2
    except sqlite3.Error as exc:
        _print_error(migration_error_from_sqlite(exc), args.json)
        return 2
    except (OSError, UnicodeError):
        _print_error(MigrationIOError(), args.json)
        return 2
    _print_result(result, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
