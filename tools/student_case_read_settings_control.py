#!/usr/bin/env python3
"""Guarded control for the fixed Student Case READ settings."""

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
READ_SETTING_KEY = "STUDENT_CASES_READ_ENABLED"
WRITE_SETTING_KEYS = (
    "TRANSFER_CASES_WRITE_ENABLED",
    "PROGRESSION_CASES_WRITE_ENABLED",
    "EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED",
    "CASE_COMPLETION_ENABLED",
)
SETTING_KEYS = (READ_SETTING_KEY, *WRITE_SETTING_KEYS)
STUDENT_CASE_TABLES = (
    "student_case_sequences",
    "student_cases",
    "student_case_assignments",
    "case_events",
    "student_school_enrolments",
)
INITIALISE_CONFIRMATION = "INITIALISE-STUDENT-CASE-READ-SETTINGS-FALSE"
ENABLE_CONFIRMATION = "ENABLE-STUDENT-CASE-READ"
DISABLE_CONFIRMATION = "DISABLE-STUDENT-CASE-READ"


class StudentCaseReadSettingsControlError(Exception):
    code = "student_case_read_settings_control_failed"
    public_message = "Student Case READ settings control failed."

    def __init__(self, detail: str | None = None):
        super().__init__(self.code)
        self.detail = detail


class DatabaseNotFoundError(StudentCaseReadSettingsControlError):
    code = "student_case_read_settings_database_not_found"
    public_message = "The explicit database path does not exist."


class InvalidArgumentsError(StudentCaseReadSettingsControlError):
    code = "student_case_read_settings_invalid_arguments"
    public_message = "Student Case READ settings arguments are invalid."


class OperatorInvalidError(StudentCaseReadSettingsControlError):
    code = "student_case_read_settings_operator_invalid"
    public_message = "The settings operator is invalid."


class StateInvalidError(StudentCaseReadSettingsControlError):
    code = "student_case_read_settings_state_invalid"
    public_message = "The Student Case READ settings state is invalid."


class KeyNotAllowedError(StudentCaseReadSettingsControlError):
    code = "student_case_read_settings_key_not_allowed"
    public_message = "Only the fixed Student Case READ settings are allowed."


class WriteGateEnabledError(StudentCaseReadSettingsControlError):
    code = "student_case_read_write_gate_enabled"
    public_message = "A Student Case write gate is enabled."


class BusinessDataPresentError(StudentCaseReadSettingsControlError):
    code = "student_case_read_settings_business_data_present"
    public_message = "Student Case business tables must be empty."


class DatabaseBusyError(StudentCaseReadSettingsControlError):
    code = "student_case_read_settings_database_busy"
    public_message = "The database is busy; no setting change was committed."


class AuditFailedError(StudentCaseReadSettingsControlError):
    code = "student_case_read_settings_audit_failed"
    public_message = "The settings audit failed; the change was rolled back."


class UpdateFailedError(StudentCaseReadSettingsControlError):
    code = "student_case_read_settings_update_failed"
    public_message = "The settings update failed and was rolled back."


class ConfirmationRequiredError(StudentCaseReadSettingsControlError):
    code = "student_case_read_settings_confirmation_required"
    public_message = "Formal database settings control requires all safeguards."


class ControlArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InvalidArgumentsError(message)


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


def _settings_snapshot(connection: sqlite3.Connection) -> dict[str, str | None]:
    rows = {
        row["key"]: row["value"]
        for row in connection.execute(
            """SELECT key,value FROM system_settings
               WHERE key IN (?,?,?,?,?)""",
            SETTING_KEYS,
        )
    }
    return {key: rows.get(key) for key in sorted(SETTING_KEYS)}


def _student_case_counts(connection: sqlite3.Connection) -> dict[str, int]:
    try:
        return {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in STUDENT_CASE_TABLES
        }
    except sqlite3.Error as exc:
        raise StudentCaseReadSettingsControlError() from exc


def _validate_empty_business_tables(connection: sqlite3.Connection) -> None:
    if any(_student_case_counts(connection).values()):
        raise BusinessDataPresentError()


def _validate_operator(
    connection: sqlite3.Connection,
    operator_user_id: int,
) -> sqlite3.Row:
    operator = connection.execute(
        """SELECT id,username,role_id,is_active,deleted_at
           FROM users WHERE id=?""",
        (operator_user_id,),
    ).fetchone()
    if (
        operator is None
        or operator["role_id"] != 1
        or operator["is_active"] != 1
        or operator["deleted_at"] is not None
    ):
        raise OperatorInvalidError()
    return operator


def _validate_operation_arguments(
    operator_user_id: int,
    correlation_id: str,
    reason: str,
    source_commit: str,
) -> None:
    if (
        type(operator_user_id) is not int
        or operator_user_id <= 0
        or not isinstance(correlation_id, str)
        or not correlation_id.strip()
        or not isinstance(reason, str)
        or not reason.strip()
        or not isinstance(source_commit, str)
        or not source_commit.strip()
    ):
        raise InvalidArgumentsError()


def _validate_formal_guard(
    path: Path,
    *,
    formal_database_path: str | Path,
    confirmation_required: str,
    allow_formal_database: bool,
    confirmation: str | None,
    expected_database_sha256: str | None,
) -> None:
    formal_path = Path(formal_database_path).expanduser().resolve()
    if not _is_same_file(path, formal_path):
        return
    if (
        not allow_formal_database
        or confirmation != confirmation_required
        or not expected_database_sha256
        or expected_database_sha256 != database_sha256(path)
    ):
        raise ConfirmationRequiredError()


def _validate_complete_safe_settings(
    settings: dict[str, str | None],
) -> None:
    if set(settings) != set(SETTING_KEYS) or any(
        settings[key] not in {"false", "true"} for key in SETTING_KEYS
    ):
        raise StateInvalidError()
    if any(settings[key] != "false" for key in WRITE_SETTING_KEYS):
        raise WriteGateEnabledError()


def _json_value(key: str, value: str | None) -> str:
    return json.dumps(
        {"key": key, "value": value},
        ensure_ascii=False,
        sort_keys=True,
    )


def _insert_audit(
    connection: sqlite3.Connection,
    *,
    operator: sqlite3.Row,
    action_type: str,
    operation: str,
    key: str,
    old_value: str | None,
    new_value: str,
    correlation_id: str,
    reason: str,
    source_commit: str,
    timestamp: str,
) -> None:
    payload = {
        "actor_user_id": int(operator["id"]),
        "actor_username": operator["username"],
        "correlation_id": correlation_id,
        "key": key,
        "new_value": new_value,
        "old_value": old_value,
        "operation": operation,
        "reason": reason,
        "result": "success",
        "source_commit": source_commit,
        "timestamp": timestamp,
    }
    connection.execute(
        """INSERT INTO audit_logs(
               user_id,username,action_type,module_name,module_key,
               record_id,old_value,new_value,device_info,status,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            operator["id"],
            operator["username"],
            action_type,
            "system_settings",
            "student_case_read_settings_control",
            key,
            _json_value(key, old_value),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            f"correlation:{correlation_id}",
            "success",
            timestamp,
        ),
    )


def inspect_database(database_path: str | Path) -> dict[str, object]:
    path = _existing_database_path(database_path)
    connection = _connect_read_only(path)
    try:
        settings = _settings_snapshot(connection)
        return {
            "ok": True,
            "settings": settings,
            "resolved_flags": {
                key: settings[key] == "true" for key in sorted(SETTING_KEYS)
            },
            "student_case_counts": _student_case_counts(connection),
        }
    finally:
        connection.close()


def verify_database(database_path: str | Path) -> dict[str, object]:
    path = _existing_database_path(database_path)
    connection = _connect_read_only(path)
    try:
        settings = _settings_snapshot(connection)
        if any(settings[key] is None for key in SETTING_KEYS):
            raise StateInvalidError()
        _validate_complete_safe_settings(settings)
        _validate_empty_business_tables(connection)
        return {
            "ok": True,
            "settings": settings,
            "resolved_flags": {
                key: settings[key] == "true" for key in sorted(SETTING_KEYS)
            },
            "student_case_counts": _student_case_counts(connection),
        }
    finally:
        connection.close()


def _controlled_change(
    database_path: str | Path,
    *,
    operation: str,
    operator_user_id: int,
    correlation_id: str,
    reason: str,
    source_commit: str,
    timestamp: str | None,
    timeout: float,
    formal_database_path: str | Path,
    allow_formal_database: bool,
    confirmation: str | None,
    expected_database_sha256: str | None,
) -> dict[str, object]:
    path = _existing_database_path(database_path)
    _validate_operation_arguments(
        operator_user_id,
        correlation_id,
        reason,
        source_commit,
    )
    confirmation_required = {
        "initialise_false": INITIALISE_CONFIRMATION,
        "enable_read": ENABLE_CONFIRMATION,
        "disable_read": DISABLE_CONFIRMATION,
    }.get(operation)
    if confirmation_required is None:
        raise KeyNotAllowedError()
    _validate_formal_guard(
        path,
        formal_database_path=formal_database_path,
        confirmation_required=confirmation_required,
        allow_formal_database=allow_formal_database,
        confirmation=confirmation,
        expected_database_sha256=expected_database_sha256,
    )
    stamp = timestamp or datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    connection = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    connection.row_factory = sqlite3.Row
    stage = "begin"
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        operator = _validate_operator(connection, operator_user_id)
        _validate_empty_business_tables(connection)
        settings = _settings_snapshot(connection)

        if operation == "initialise_false":
            existing = {
                key: value for key, value in settings.items() if value is not None
            }
            if any(value != "false" for value in existing.values()):
                raise StateInvalidError()
            missing = [key for key in SETTING_KEYS if settings[key] is None]
            if not missing:
                connection.rollback()
                return {
                    "ok": True,
                    "changed": False,
                    "reason": "already_initialised_false",
                }
            for key in missing:
                stage = "update"
                connection.execute(
                    """INSERT INTO system_settings(
                           key,value,updated_by,updated_at
                       ) VALUES(?,?,?,?)""",
                    (key, "false", operator_user_id, stamp),
                )
                stage = "audit"
                _insert_audit(
                    connection,
                    operator=operator,
                    action_type="initialise_student_case_read_setting",
                    operation=operation,
                    key=key,
                    old_value=None,
                    new_value="false",
                    correlation_id=correlation_id,
                    reason=reason,
                    source_commit=source_commit,
                    timestamp=stamp,
                )
            result = {
                "ok": True,
                "changed": True,
                "settings": {key: "false" for key in SETTING_KEYS},
            }
        else:
            _validate_complete_safe_settings(settings)
            old_value = settings[READ_SETTING_KEY]
            new_value = "true" if operation == "enable_read" else "false"
            if old_value == new_value:
                connection.rollback()
                return {
                    "ok": True,
                    "changed": False,
                    "reason": (
                        "already_enabled"
                        if new_value == "true"
                        else "already_disabled"
                    ),
                }
            stage = "update"
            cursor = connection.execute(
                """UPDATE system_settings
                   SET value=?,updated_by=?,updated_at=?
                   WHERE key=? AND value=?""",
                (
                    new_value,
                    operator_user_id,
                    stamp,
                    READ_SETTING_KEY,
                    old_value,
                ),
            )
            if cursor.rowcount != 1:
                raise UpdateFailedError()
            stage = "audit"
            _insert_audit(
                connection,
                operator=operator,
                action_type=(
                    "enable_student_case_read"
                    if operation == "enable_read"
                    else "disable_student_case_read"
                ),
                operation=operation,
                key=READ_SETTING_KEY,
                old_value=old_value,
                new_value=new_value,
                correlation_id=correlation_id,
                reason=reason,
                source_commit=source_commit,
                timestamp=stamp,
            )
            result = {
                "ok": True,
                "changed": True,
                "key": READ_SETTING_KEY,
                "old_value": old_value,
                "new_value": new_value,
            }

        stage = "postcondition"
        stored = _settings_snapshot(connection)
        _validate_complete_safe_settings(stored)
        _validate_empty_business_tables(connection)
        expected_read = (
            "true" if operation == "enable_read" else "false"
        )
        if stored[READ_SETTING_KEY] != expected_read:
            raise UpdateFailedError()
        connection.commit()
        return result
    except StudentCaseReadSettingsControlError:
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
            raise UpdateFailedError() from exc
        raise StudentCaseReadSettingsControlError() from exc
    except sqlite3.Error as exc:
        connection.rollback()
        if stage == "audit":
            raise AuditFailedError() from exc
        if stage == "update":
            raise UpdateFailedError() from exc
        raise StudentCaseReadSettingsControlError() from exc
    finally:
        connection.close()


def initialise_false(
    database_path: str | Path,
    *,
    operator_user_id: int,
    correlation_id: str,
    reason: str,
    source_commit: str,
    timestamp: str | None = None,
    timeout: float = 5.0,
    formal_database_path: str | Path = FORMAL_DATABASE_PATH,
    allow_formal_database: bool = False,
    confirmation: str | None = None,
    expected_database_sha256: str | None = None,
) -> dict[str, object]:
    return _controlled_change(
        database_path,
        operation="initialise_false",
        operator_user_id=operator_user_id,
        correlation_id=correlation_id,
        reason=reason,
        source_commit=source_commit,
        timestamp=timestamp,
        timeout=timeout,
        formal_database_path=formal_database_path,
        allow_formal_database=allow_formal_database,
        confirmation=confirmation,
        expected_database_sha256=expected_database_sha256,
    )


def enable_read(
    database_path: str | Path,
    *,
    operator_user_id: int,
    correlation_id: str,
    reason: str,
    source_commit: str,
    timestamp: str | None = None,
    timeout: float = 5.0,
    formal_database_path: str | Path = FORMAL_DATABASE_PATH,
    allow_formal_database: bool = False,
    confirmation: str | None = None,
    expected_database_sha256: str | None = None,
) -> dict[str, object]:
    return _controlled_change(
        database_path,
        operation="enable_read",
        operator_user_id=operator_user_id,
        correlation_id=correlation_id,
        reason=reason,
        source_commit=source_commit,
        timestamp=timestamp,
        timeout=timeout,
        formal_database_path=formal_database_path,
        allow_formal_database=allow_formal_database,
        confirmation=confirmation,
        expected_database_sha256=expected_database_sha256,
    )


def disable_read(
    database_path: str | Path,
    *,
    operator_user_id: int,
    correlation_id: str,
    reason: str,
    source_commit: str,
    timestamp: str | None = None,
    timeout: float = 5.0,
    formal_database_path: str | Path = FORMAL_DATABASE_PATH,
    allow_formal_database: bool = False,
    confirmation: str | None = None,
    expected_database_sha256: str | None = None,
) -> dict[str, object]:
    return _controlled_change(
        database_path,
        operation="disable_read",
        operator_user_id=operator_user_id,
        correlation_id=correlation_id,
        reason=reason,
        source_commit=source_commit,
        timestamp=timestamp,
        timeout=timeout,
        formal_database_path=formal_database_path,
        allow_formal_database=allow_formal_database,
        confirmation=confirmation,
        expected_database_sha256=expected_database_sha256,
    )


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", required=True)
    parser.add_argument("--json", action="store_true")


def _add_write_arguments(
    parser: argparse.ArgumentParser,
    confirmation: str,
) -> None:
    _add_common_arguments(parser)
    parser.add_argument("--operator-user-id", required=True, type=int)
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--allow-formal-database", action="store_true")
    parser.add_argument(
        "--confirm",
        help=f"Formal database operation requires {confirmation}.",
    )
    parser.add_argument("--expected-database-sha256")


def build_parser() -> argparse.ArgumentParser:
    parser = ControlArgumentParser(
        description=(
            "Inspect, verify, initialise false, enable READ, or disable READ "
            "for the five fixed Student Case settings."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("inspect", "verify"):
        command_parser = subparsers.add_parser(command)
        _add_common_arguments(command_parser)
    initialise_parser = subparsers.add_parser("initialise-false")
    _add_write_arguments(initialise_parser, INITIALISE_CONFIRMATION)
    enable_parser = subparsers.add_parser("enable-read")
    _add_write_arguments(enable_parser, ENABLE_CONFIRMATION)
    disable_parser = subparsers.add_parser("disable-read")
    _add_write_arguments(disable_parser, DISABLE_CONFIRMATION)
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


def _print_error(
    error: StudentCaseReadSettingsControlError,
    as_json: bool,
) -> None:
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
            function = {
                "initialise-false": initialise_false,
                "enable-read": enable_read,
                "disable-read": disable_read,
            }[args.command]
            result = function(
                args.database,
                operator_user_id=args.operator_user_id,
                correlation_id=args.correlation_id,
                reason=args.reason,
                source_commit=args.source_commit,
                allow_formal_database=args.allow_formal_database,
                confirmation=args.confirm,
                expected_database_sha256=args.expected_database_sha256,
            )
    except StudentCaseReadSettingsControlError as exc:
        _print_error(exc, "--json" in raw_argv)
        return 2
    except (sqlite3.Error, OSError, UnicodeError):
        _print_error(
            StudentCaseReadSettingsControlError(),
            "--json" in raw_argv,
        )
        return 2
    _print_result(result, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
