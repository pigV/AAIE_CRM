#!/usr/bin/env python3
"""Guarded control for the single explicit Student Case view permission."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORMAL_DATABASE_PATH = (PROJECT_ROOT / "database" / "aaie_crm.db").resolve()
TARGET_ROLE_ID = 1
TARGET_ROLE_NAME = "Admin"
VIEW_PERMISSION = "student_cases.view"
TARGET_ACCOUNT_NAMES = ("admin", "sonnyz")
GRANT_CONFIRMATION = "GRANT-STUDENT-CASE-VIEW"
REVOKE_CONFIRMATION = "REVOKE-STUDENT-CASE-VIEW"
STUDENT_CASE_PREFIX = "student_cases."
STUDENT_CASE_TABLES = (
    "student_case_sequences",
    "student_cases",
    "student_case_assignments",
    "case_events",
    "student_school_enrolments",
)
WRITE_SETTING_KEYS = (
    "TRANSFER_CASES_WRITE_ENABLED",
    "PROGRESSION_CASES_WRITE_ENABLED",
    "EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED",
    "CASE_COMPLETION_ENABLED",
)


class StudentCaseReadControlError(Exception):
    code = "student_case_read_control_failed"
    public_message = "Student Case read control failed."

    def __init__(self, detail: str | None = None):
        super().__init__(self.code)
        self.detail = detail


class DatabaseNotFoundError(StudentCaseReadControlError):
    code = "student_case_read_database_not_found"
    public_message = "The explicit database path does not exist."


class RoleNotFoundError(StudentCaseReadControlError):
    code = "student_case_read_role_not_found"
    public_message = "The target Admin role was not found."


class RoleMismatchError(StudentCaseReadControlError):
    code = "student_case_read_role_mismatch"
    public_message = "The target Admin role, operator, or accounts do not match."


class PermissionsInvalidError(StudentCaseReadControlError):
    code = "student_case_read_permissions_invalid"
    public_message = "The target role permissions are invalid."


class PermissionNotAllowedError(StudentCaseReadControlError):
    code = "student_case_read_permission_not_allowed"
    public_message = "Only student_cases.view may be controlled."


class DatabaseBusyError(StudentCaseReadControlError):
    code = "student_case_read_database_busy"
    public_message = "The database is busy; no permission change was committed."


class AuditFailedError(StudentCaseReadControlError):
    code = "student_case_read_audit_failed"
    public_message = "The permission audit failed; the change was rolled back."


class PermissionUpdateFailedError(StudentCaseReadControlError):
    code = "student_case_read_permission_update_failed"
    public_message = "The permission update failed and was rolled back."


class ConfirmationRequiredError(StudentCaseReadControlError):
    code = "student_case_read_confirmation_required"
    public_message = "Formal database permission control requires all safeguards."


class BusinessDataPresentError(StudentCaseReadControlError):
    code = "student_case_read_business_data_present"
    public_message = "Student Case business tables must be empty."


class WriteGateEnabledError(StudentCaseReadControlError):
    code = "student_case_read_write_gate_enabled"
    public_message = "All Student Case write gates must remain disabled."


class ControlArgumentError(StudentCaseReadControlError):
    code = "student_case_read_invalid_arguments"
    public_message = "Student Case read control arguments are invalid."


class ControlArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ControlArgumentError(message)


def canonical_permissions(raw_json: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PermissionsInvalidError() from exc
    if (
        not isinstance(parsed, list)
        or any(type(item) is not str for item in parsed)
        or len(set(parsed)) != len(parsed)
    ):
        raise PermissionsInvalidError()
    return tuple(parsed)


def canonical_permissions_sha256(permissions: tuple[str, ...]) -> str:
    encoded = json.dumps(
        list(permissions),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def database_sha256(database_path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(database_path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_database_path(database_path: str | Path) -> Path:
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise DatabaseNotFoundError()
    return path


def _is_same_file(first: Path, second: Path) -> bool:
    if first == second:
        return True
    try:
        return first.samefile(second)
    except OSError:
        return False


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _validated_role(
    connection: sqlite3.Connection,
    *,
    target_role_id: int,
    target_role_name: str,
) -> tuple[sqlite3.Row, tuple[str, ...], list[str]]:
    id_row = connection.execute(
        "SELECT id,name,permissions_json FROM roles WHERE id=?",
        (target_role_id,),
    ).fetchone()
    if id_row is None:
        raise RoleNotFoundError()
    name_rows = connection.execute(
        "SELECT id,name FROM roles WHERE name=?",
        (target_role_name,),
    ).fetchall()
    if (
        target_role_id != TARGET_ROLE_ID
        or target_role_name != TARGET_ROLE_NAME
        or id_row["name"] != target_role_name
        or len(name_rows) != 1
        or name_rows[0]["id"] != target_role_id
    ):
        raise RoleMismatchError()
    permissions = canonical_permissions(id_row["permissions_json"])
    if "*" not in permissions:
        raise PermissionsInvalidError()
    accounts = connection.execute(
        """SELECT username,is_active,deleted_at,role_id
           FROM users WHERE username IN (?,?) ORDER BY username""",
        TARGET_ACCOUNT_NAMES,
    ).fetchall()
    if (
        len(accounts) != 2
        or tuple(row["username"] for row in accounts)
        != tuple(sorted(TARGET_ACCOUNT_NAMES))
        or any(
            row["is_active"] != 1
            or row["deleted_at"] is not None
            or row["role_id"] != target_role_id
            for row in accounts
        )
    ):
        raise RoleMismatchError()
    return id_row, permissions, [row["username"] for row in accounts]


def _role_snapshot(connection: sqlite3.Connection) -> dict[int, str]:
    return {
        int(row["id"]): row["permissions_json"]
        for row in connection.execute(
            "SELECT id,permissions_json FROM roles ORDER BY id"
        )
    }


def _write_gate_state(connection: sqlite3.Connection) -> dict[str, bool]:
    rows = {
        row["key"]: row["value"]
        for row in connection.execute(
            """SELECT key,value FROM system_settings
               WHERE key IN (?,?,?,?)""",
            WRITE_SETTING_KEYS,
        )
    }
    return {key: rows.get(key) == "true" for key in WRITE_SETTING_KEYS}


def _student_case_counts(connection: sqlite3.Connection) -> dict[str, int]:
    try:
        return {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in STUDENT_CASE_TABLES
        }
    except sqlite3.Error as exc:
        raise StudentCaseReadControlError() from exc


def _other_role_case_permissions(
    connection: sqlite3.Connection,
    *,
    target_role_id: int,
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for row in connection.execute(
        "SELECT id,name,permissions_json FROM roles WHERE id<>? ORDER BY id",
        (target_role_id,),
    ):
        permissions = canonical_permissions(row["permissions_json"])
        case_permissions = [
            item for item in permissions if item.startswith(STUDENT_CASE_PREFIX)
        ]
        if case_permissions:
            result[row["name"]] = case_permissions
    return result


def _validate_permission_boundary(
    connection: sqlite3.Connection,
    permissions: tuple[str, ...],
    *,
    target_role_id: int,
) -> None:
    target_case_permissions = {
        item for item in permissions if item.startswith(STUDENT_CASE_PREFIX)
    }
    if not target_case_permissions.issubset({VIEW_PERMISSION}):
        raise PermissionNotAllowedError()
    if _other_role_case_permissions(
        connection,
        target_role_id=target_role_id,
    ):
        raise PermissionNotAllowedError()


def _validate_empty_and_read_only(connection: sqlite3.Connection) -> None:
    if any(_write_gate_state(connection).values()):
        raise WriteGateEnabledError()
    if any(_student_case_counts(connection).values()):
        raise BusinessDataPresentError()


def _validated_operator(
    connection: sqlite3.Connection,
    *,
    operator_user_id: int,
    target_role_id: int,
) -> sqlite3.Row:
    operator = connection.execute(
        """SELECT id,username,role_id,is_active,deleted_at
           FROM users WHERE id=?""",
        (operator_user_id,),
    ).fetchone()
    if (
        operator is None
        or operator["role_id"] != target_role_id
        or operator["is_active"] != 1
        or operator["deleted_at"] is not None
    ):
        raise RoleMismatchError()
    return operator


def inspect_database(
    database_path: str | Path,
    *,
    target_role_id: int = TARGET_ROLE_ID,
    target_role_name: str = TARGET_ROLE_NAME,
) -> dict[str, object]:
    path = _existing_database_path(database_path)
    connection = _connect_read_only(path)
    try:
        role, permissions, accounts = _validated_role(
            connection,
            target_role_id=target_role_id,
            target_role_name=target_role_name,
        )
        return {
            "ok": True,
            "role": {
                "id": int(role["id"]),
                "name": role["name"],
                "permissions": list(permissions),
            },
            "target_enabled_accounts": accounts,
            "view_granted": VIEW_PERMISSION in permissions,
        }
    finally:
        connection.close()


def verify_database(
    database_path: str | Path,
    *,
    target_role_id: int = TARGET_ROLE_ID,
    target_role_name: str = TARGET_ROLE_NAME,
) -> dict[str, object]:
    path = _existing_database_path(database_path)
    connection = _connect_read_only(path)
    try:
        role, permissions, accounts = _validated_role(
            connection,
            target_role_id=target_role_id,
            target_role_name=target_role_name,
        )
        other_permissions = _other_role_case_permissions(
            connection,
            target_role_id=target_role_id,
        )
        _validate_empty_and_read_only(connection)
        _validate_permission_boundary(
            connection,
            permissions,
            target_role_id=target_role_id,
        )
        return {
            "ok": True,
            "role": {
                "id": int(role["id"]),
                "name": role["name"],
                "permissions": list(permissions),
            },
            "target_enabled_accounts": accounts,
            "view_granted": VIEW_PERMISSION in permissions,
            "write_gates": _write_gate_state(connection),
            "student_case_counts": _student_case_counts(connection),
            "other_role_student_case_permissions": other_permissions,
        }
    finally:
        connection.close()


def _validate_formal_guard(
    path: Path,
    *,
    formal_database_path: str | Path,
    operation: str,
    allow_formal_database: bool,
    confirmation: str | None,
    expected_database_sha256: str | None,
) -> None:
    formal_path = Path(formal_database_path).expanduser().resolve()
    if not _is_same_file(path, formal_path):
        return
    required_confirmation = (
        GRANT_CONFIRMATION if operation == "grant" else REVOKE_CONFIRMATION
    )
    if (
        not allow_formal_database
        or confirmation != required_confirmation
        or not expected_database_sha256
        or expected_database_sha256 != database_sha256(path)
    ):
        raise ConfirmationRequiredError()


def _audit_values(
    *,
    role_id: int,
    role_name: str,
    old_permissions: tuple[str, ...],
    new_permissions: tuple[str, ...],
    operator_user_id: int,
    operator_username: str,
    accounts: list[str],
    operation: str,
    correlation_id: str,
    reason: str,
    timestamp: str,
) -> tuple[str, str]:
    old_value = json.dumps(
        {
            "permissions": list(old_permissions),
            "role_id": role_id,
            "role_name": role_name,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    new_value = json.dumps(
        {
            "actor_user_id": operator_user_id,
            "actor_username": operator_username,
            "correlation_id": correlation_id,
            "operation": operation,
            "permissions": list(new_permissions),
            "reason": reason,
            "role_id": role_id,
            "role_name": role_name,
            "target_enabled_accounts": accounts,
            "timestamp": timestamp,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return old_value, new_value


def _controlled_change(
    database_path: str | Path,
    *,
    change: str,
    operator_user_id: int,
    correlation_id: str,
    reason: str,
    target_role_id: int,
    target_role_name: str,
    permission: str,
    timestamp: str | None,
    timeout: float,
    formal_database_path: str | Path,
    allow_formal_database: bool,
    confirmation: str | None,
    expected_database_sha256: str | None,
) -> dict[str, object]:
    path = _existing_database_path(database_path)
    if permission != VIEW_PERMISSION:
        raise PermissionNotAllowedError()
    if (
        type(operator_user_id) is not int
        or operator_user_id <= 0
        or not correlation_id
        or not reason
    ):
        raise ControlArgumentError()
    _validate_formal_guard(
        path,
        formal_database_path=formal_database_path,
        operation=change,
        allow_formal_database=allow_formal_database,
        confirmation=confirmation,
        expected_database_sha256=expected_database_sha256,
    )
    stamp = timestamp or datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    connection = sqlite3.connect(
        path,
        timeout=timeout,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    stage = "begin"
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        role, permissions, accounts = _validated_role(
            connection,
            target_role_id=target_role_id,
            target_role_name=target_role_name,
        )
        _validate_permission_boundary(
            connection,
            permissions,
            target_role_id=target_role_id,
        )
        _validate_empty_and_read_only(connection)
        operator = _validated_operator(
            connection,
            operator_user_id=operator_user_id,
            target_role_id=target_role_id,
        )
        if change == "grant":
            changed = VIEW_PERMISSION not in permissions
            new_permissions = (
                permissions + (VIEW_PERMISSION,) if changed else permissions
            )
            no_op_reason = "already_granted"
            audit_operation = "grant_student_case_view"
        else:
            changed = VIEW_PERMISSION in permissions
            new_permissions = tuple(
                item for item in permissions if item != VIEW_PERMISSION
            )
            no_op_reason = "already_revoked"
            audit_operation = "revoke_student_case_view"
        if not changed:
            connection.rollback()
            return {
                "ok": True,
                "changed": False,
                "role_id": target_role_id,
                "permission": VIEW_PERMISSION,
                "reason": no_op_reason,
            }
        role_snapshot = _role_snapshot(connection)
        stage = "update"
        cursor = connection.execute(
            """UPDATE roles SET permissions_json=?
               WHERE id=? AND name=?""",
            (
                json.dumps(
                    list(new_permissions),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                target_role_id,
                target_role_name,
            ),
        )
        if cursor.rowcount != 1:
            raise PermissionUpdateFailedError()
        stored = connection.execute(
            "SELECT permissions_json FROM roles WHERE id=?",
            (target_role_id,),
        ).fetchone()
        if stored is None or canonical_permissions(
            stored["permissions_json"]
        ) != new_permissions:
            raise PermissionUpdateFailedError()
        other_after = _role_snapshot(connection)
        if {
            key: value
            for key, value in other_after.items()
            if key != target_role_id
        } != {
            key: value
            for key, value in role_snapshot.items()
            if key != target_role_id
        }:
            raise PermissionUpdateFailedError()
        old_value, new_value = _audit_values(
            role_id=target_role_id,
            role_name=target_role_name,
            old_permissions=permissions,
            new_permissions=new_permissions,
            operator_user_id=operator_user_id,
            operator_username=operator["username"],
            accounts=accounts,
            operation=audit_operation,
            correlation_id=correlation_id,
            reason=reason,
            timestamp=stamp,
        )
        stage = "audit"
        connection.execute(
            """INSERT INTO audit_logs(
                   user_id,username,action_type,module_name,module_key,
                   record_id,old_value,new_value,device_info,status,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                operator_user_id,
                operator["username"],
                audit_operation,
                "user_management",
                "student_case_read_control",
                str(target_role_id),
                old_value,
                new_value,
                f"correlation:{correlation_id}",
                "success",
                stamp,
            ),
        )
        stage = "postcondition"
        _validate_empty_and_read_only(connection)
        _validate_permission_boundary(
            connection,
            new_permissions,
            target_role_id=target_role_id,
        )
        connection.commit()
        return {
            "ok": True,
            "changed": True,
            "role_id": target_role_id,
            "permission": VIEW_PERMISSION,
        }
    except StudentCaseReadControlError:
        connection.rollback()
        raise
    except sqlite3.OperationalError as exc:
        connection.rollback()
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            raise DatabaseBusyError() from exc
        if stage == "audit":
            raise AuditFailedError() from exc
        if stage == "update":
            raise PermissionUpdateFailedError() from exc
        raise StudentCaseReadControlError() from exc
    except sqlite3.Error as exc:
        connection.rollback()
        if stage == "audit":
            raise AuditFailedError() from exc
        if stage == "update":
            raise PermissionUpdateFailedError() from exc
        raise StudentCaseReadControlError() from exc
    finally:
        connection.close()


def grant_view(
    database_path: str | Path,
    *,
    operator_user_id: int,
    correlation_id: str,
    reason: str,
    target_role_id: int = TARGET_ROLE_ID,
    target_role_name: str = TARGET_ROLE_NAME,
    permission: str = VIEW_PERMISSION,
    timestamp: str | None = None,
    timeout: float = 5.0,
    formal_database_path: str | Path = FORMAL_DATABASE_PATH,
    allow_formal_database: bool = False,
    confirmation: str | None = None,
    expected_database_sha256: str | None = None,
) -> dict[str, object]:
    return _controlled_change(
        database_path,
        change="grant",
        operator_user_id=operator_user_id,
        correlation_id=correlation_id,
        reason=reason,
        target_role_id=target_role_id,
        target_role_name=target_role_name,
        permission=permission,
        timestamp=timestamp,
        timeout=timeout,
        formal_database_path=formal_database_path,
        allow_formal_database=allow_formal_database,
        confirmation=confirmation,
        expected_database_sha256=expected_database_sha256,
    )


def revoke_view(
    database_path: str | Path,
    *,
    operator_user_id: int,
    correlation_id: str,
    reason: str,
    target_role_id: int = TARGET_ROLE_ID,
    target_role_name: str = TARGET_ROLE_NAME,
    permission: str = VIEW_PERMISSION,
    timestamp: str | None = None,
    timeout: float = 5.0,
    formal_database_path: str | Path = FORMAL_DATABASE_PATH,
    allow_formal_database: bool = False,
    confirmation: str | None = None,
    expected_database_sha256: str | None = None,
) -> dict[str, object]:
    return _controlled_change(
        database_path,
        change="revoke",
        operator_user_id=operator_user_id,
        correlation_id=correlation_id,
        reason=reason,
        target_role_id=target_role_id,
        target_role_name=target_role_name,
        permission=permission,
        timestamp=timestamp,
        timeout=timeout,
        formal_database_path=formal_database_path,
        allow_formal_database=allow_formal_database,
        confirmation=confirmation,
        expected_database_sha256=expected_database_sha256,
    )


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database",
        required=True,
        help="Explicit existing SQLite database path; there is no default.",
    )
    parser.add_argument("--json", action="store_true")


def _add_write_arguments(parser: argparse.ArgumentParser, confirmation: str) -> None:
    _add_common_arguments(parser)
    parser.add_argument("--operator-user-id", required=True, type=int)
    parser.add_argument("--target-role-id", required=True, type=int)
    parser.add_argument("--target-role-name", required=True)
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--allow-formal-database", action="store_true")
    parser.add_argument(
        "--confirm",
        help=f"Formal database operation requires {confirmation}.",
    )
    parser.add_argument("--expected-database-sha256")


def build_parser() -> argparse.ArgumentParser:
    parser = ControlArgumentParser(
        description=(
            "Inspect, verify, grant, or revoke only student_cases.view. "
            "No general permission editor is provided."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("inspect", "verify"):
        command_parser = subparsers.add_parser(command)
        _add_common_arguments(command_parser)
    grant_parser = subparsers.add_parser("grant-view")
    _add_write_arguments(grant_parser, GRANT_CONFIRMATION)
    revoke_parser = subparsers.add_parser("revoke-view")
    _add_write_arguments(revoke_parser, REVOKE_CONFIRMATION)
    return parser


def _print_result(result: dict[str, object], as_json: bool) -> None:
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=None if as_json else 2,
            sort_keys=True,
        )
    )


def _print_error(error: StudentCaseReadControlError, as_json: bool) -> None:
    payload = {
        "ok": False,
        "error": {
            "code": error.code,
            "message": error.public_message,
        },
    }
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        file=sys.stdout if as_json else sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    try:
        args = build_parser().parse_args(raw_argv)
        if args.command == "inspect":
            result = inspect_database(args.database)
        elif args.command == "verify":
            result = verify_database(args.database)
        else:
            function = grant_view if args.command == "grant-view" else revoke_view
            result = function(
                args.database,
                operator_user_id=args.operator_user_id,
                target_role_id=args.target_role_id,
                target_role_name=args.target_role_name,
                correlation_id=args.correlation_id,
                reason=args.reason,
                allow_formal_database=args.allow_formal_database,
                confirmation=args.confirm,
                expected_database_sha256=args.expected_database_sha256,
            )
    except StudentCaseReadControlError as exc:
        _print_error(exc, "--json" in raw_argv)
        return 2
    except (sqlite3.Error, OSError, UnicodeError):
        _print_error(StudentCaseReadControlError(), "--json" in raw_argv)
        return 2
    _print_result(result, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
