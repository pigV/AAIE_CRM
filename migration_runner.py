"""Standalone, explicit, checksum-verified SQLite migration runner.

The module has no import-time filesystem or database side effects. Callers must
always provide an existing database path and a migration directory.

Migration packages use two files:

* ``<version>_<name>.sql`` for forward-only SQL.
* ``checks/<version>_<name>_forward_check.sql`` for read-only checks.

The stored checksum covers both files so neither may change after application.
Each migration is committed in its own ``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_FORMAL_DATABASE_PATH = PROJECT_ROOT / "database" / "aaie_crm.db"
FORMAL_CONFIRMATION = "APPLY-STUDENT-CASE-PHASE1"
PACKAGE_DELIMITER = b"\0FORWARD_CHECK\0"
MIGRATION_FILE_RE = re.compile(
    r"^(?P<version>\d{8}_\d{3})_(?P<name>[a-z0-9][a-z0-9_]*)\.sql$"
)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_COMMENT_RE = re.compile(r"--[^\n]*(?:\n|$)")
MIGRATION_MODE_RE = re.compile(
    r"^\s*--\s*migration_mode:\s*(?P<mode>[a-z0-9_]+)\s*$",
    re.MULTILINE,
)
NORMAL_MIGRATION_MODE = "normal"
SQLITE_FK_OFF_REBUILD_MODE = "sqlite_fk_off_rebuild"
SPECIAL_MIGRATION_MODES = {
    ("20260907_035", "visa_management_authorities"): SQLITE_FK_OFF_REBUILD_MODE,
    (
        "20260730_006",
        "rebuild_case_arrangement_confirmations",
    ): SQLITE_FK_OFF_REBUILD_MODE,
    (
        "20260730_007",
        "transfer_completion_contract",
    ): SQLITE_FK_OFF_REBUILD_MODE,
    (
        "20260804_010",
        "progression_signed_offer",
    ): SQLITE_FK_OFF_REBUILD_MODE,
    (
        "20260813_020",
        "student_lifecycle_draft_semantics",
    ): SQLITE_FK_OFF_REBUILD_MODE,
    (
        "20260816_023",
        "dashboard_canonical_states",
    ): SQLITE_FK_OFF_REBUILD_MODE,
    (
        "20260820_027",
        "transfer_contract_ownership",
    ): SQLITE_FK_OFF_REBUILD_MODE,
    (
        "20260824_029",
        "contract_template_superseded_status",
    ): SQLITE_FK_OFF_REBUILD_MODE,
    (
        "20260904_035",
        "transfer_v2_final_schema",
    ): SQLITE_FK_OFF_REBUILD_MODE,
}
FORBIDDEN_UPGRADE_KEYWORDS = {
    "ATTACH",
    "BEGIN",
    "COMMIT",
    "DETACH",
    "PRAGMA",
    "RELEASE",
    "ROLLBACK",
    "SAVEPOINT",
    "VACUUM",
}


class MigrationError(RuntimeError):
    """Base class for controlled migration failures."""

    code = "migration_apply_failed"
    public_message = "Migration failed; no changes were applied."
    execution_state = "not_applied"
    recovery_required = False


class DatabasePathError(MigrationError):
    """The explicitly supplied database path is not usable."""

    code = "migration_database_not_found"
    public_message = "The requested database file was not found."


class MigrationDiscoveryError(MigrationError):
    """Migration packages are missing, duplicated, or malformed."""

    public_message = "The migration package is invalid."


class MigrationIOError(MigrationError):
    """A migration package or guarded database file could not be read."""

    code = "migration_io_error"
    public_message = "A migration file could not be read safely."


class MigrationDuplicateVersionError(MigrationDiscoveryError):
    """Two discovered migration packages declare the same version."""

    code = "migration_duplicate_version"
    public_message = "A duplicate migration version was found."


class MigrationVerificationError(MigrationError):
    """Migration files and the database registry do not agree."""

    public_message = "Migration verification failed."


class MigrationChecksumError(MigrationVerificationError):
    """An applied migration package changed after registration."""

    code = "migration_checksum_mismatch"
    public_message = "A migration checksum does not match the registry."


class MigrationOrderError(MigrationVerificationError):
    """Applied or discovered versions are not in valid order."""

    code = "migration_invalid_order"
    public_message = "Migration versions are not in a valid order."


class MigrationSafetyError(MigrationError):
    """A high-risk apply request did not satisfy safety gates."""

    public_message = "Migration safety requirements were not satisfied."


class MigrationFormalDatabaseConfirmationError(MigrationSafetyError):
    """A formal database apply request did not satisfy every explicit gate."""

    code = "migration_formal_database_confirmation_required"
    public_message = "Formal database confirmation requirements were not met."


class MigrationExecutionError(MigrationError):
    """Upgrade SQL failed and its migration transaction was rolled back."""

    public_message = "Migration apply failed; no changes were applied."


class MigrationDatabaseBusyError(MigrationExecutionError):
    """SQLite could not acquire the write lock within the configured timeout."""

    code = "migration_database_busy"
    public_message = "Database is busy or locked; migration was not applied."


class MigrationForwardCheckError(MigrationExecutionError):
    """A required forward check failed."""

    code = "migration_forward_check_failed"
    public_message = "Migration forward verification failed."


class MigrationForeignKeyError(MigrationExecutionError):
    """A migration introduced a new foreign-key violation."""


class MigrationSpecialModeNotAllowedError(MigrationDiscoveryError):
    """An unapproved migration attempted to request a special mode."""

    code = "migration_special_mode_not_allowed"
    public_message = "The migration requested an unapproved execution mode."


class MigrationSpecialModeMismatchError(MigrationDiscoveryError):
    """A version-bound special migration declaration is missing or wrong."""

    code = "migration_special_mode_mismatch"
    public_message = "The migration execution mode does not match its whitelist."


class MigrationRebuildBackupRequiredError(MigrationSafetyError):
    """A special table rebuild lacks a verified independent backup."""

    code = "migration_rebuild_backup_required"
    public_message = "A verified backup is required before a table rebuild."


class MigrationForeignKeysOffError(MigrationExecutionError):
    """The dedicated rebuild connection could not disable FK enforcement."""

    code = "migration_fk_off_failed"
    public_message = "Foreign-key enforcement could not be disabled safely."


class MigrationForeignKeysRestoreError(MigrationExecutionError):
    """A fresh normal connection could not confirm FK enforcement."""

    code = "migration_fk_restore_failed"
    public_message = "Foreign-key enforcement could not be restored safely."


class MigrationRebuildReferenceMismatchError(MigrationForeignKeyError):
    """A rebuild changed the pre-existing foreign-key violation set."""

    code = "migration_rebuild_reference_mismatch"
    public_message = "The table rebuild changed foreign-key references."


class MigrationPostCommitForeignKeyError(MigrationError):
    """Committed rebuild failed its post-commit FK validation."""

    code = "migration_post_commit_fk_check_failed"
    public_message = (
        "The committed rebuild failed foreign-key validation; recovery is required."
    )
    execution_state = "applied_but_post_validation_failed"
    recovery_required = True


class MigrationPostCommitQuickCheckError(MigrationError):
    """Committed rebuild failed its post-commit SQLite quick check."""

    code = "migration_post_commit_quick_check_failed"
    public_message = (
        "The committed rebuild failed SQLite validation; recovery is required."
    )
    execution_state = "applied_but_post_validation_failed"
    recovery_required = True


class MigrationAppliedUnregisteredError(MigrationError):
    """The forward contract exists without its immutable registry row."""

    code = "migration_applied_unregistered"
    public_message = (
        "The migration schema exists without registration; recovery is required."
    )
    execution_state = "applied_but_registration_failed"
    recovery_required = True


class MigrationRecoveryRequiredError(MigrationError):
    """A commit outcome requires restore/recovery before further migration."""

    code = "migration_recovery_required"
    public_message = "Migration recovery is required before any further apply."
    execution_state = "recovery_required"
    recovery_required = True


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    upgrade_path: Path
    forward_check_path: Path
    checksum_sha256: str
    mode: str = NORMAL_MIGRATION_MODE

    def as_dict(self) -> dict:
        result = {
            "version": self.version,
            "name": self.name,
            "checksum_sha256": self.checksum_sha256,
            "upgrade_path": str(self.upgrade_path),
            "forward_check_path": str(self.forward_check_path),
        }
        if self.mode != NORMAL_MIGRATION_MODE:
            result["mode"] = self.mode
        return result


@dataclass(frozen=True)
class AppliedMigration:
    rowid: int
    version: str
    name: str
    checksum_sha256: str
    applied_at: str
    execution_ms: int
    applied_by: str
    source_commit: str


REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL CHECK (length(checksum_sha256) = 64),
    applied_at TEXT NOT NULL,
    execution_ms INTEGER NOT NULL DEFAULT 0 CHECK (execution_ms >= 0),
    applied_by TEXT NOT NULL,
    source_commit TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS schema_migrations_no_update
BEFORE UPDATE ON schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'schema_migrations rows are immutable');
END;

CREATE TRIGGER IF NOT EXISTS schema_migrations_no_delete
BEFORE DELETE ON schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'schema_migrations rows are immutable');
END;
"""


def _require_existing_file(path: str | Path, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.exists():
        raise DatabasePathError(f"{label} does not exist: {candidate}")
    if not candidate.is_file():
        raise DatabasePathError(f"{label} is not a file: {candidate}")
    return candidate.resolve()


def _require_migrations_directory(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.exists():
        raise MigrationDiscoveryError(
            f"migrations directory does not exist: {candidate}"
        )
    if not candidate.is_dir():
        raise MigrationDiscoveryError(
            f"migrations path is not a directory: {candidate}"
        )
    return candidate.resolve()


def _package_checksum(upgrade_path: Path, forward_check_path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(upgrade_path.read_bytes())
    digest.update(PACKAGE_DELIMITER)
    digest.update(forward_check_path.read_bytes())
    return digest.hexdigest()


def _migration_mode(upgrade_path: Path, version: str, name: str) -> str:
    try:
        sql = upgrade_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise MigrationIOError(
            f"could not read migration mode declaration: {upgrade_path.name}"
        ) from exc
    declarations = MIGRATION_MODE_RE.findall(sql)
    if len(declarations) > 1:
        raise MigrationSpecialModeMismatchError(
            f"migration has multiple mode declarations: {version}"
        )
    declared = declarations[0] if declarations else NORMAL_MIGRATION_MODE
    approved = SPECIAL_MIGRATION_MODES.get((version, name))
    if approved is None and declared != NORMAL_MIGRATION_MODE:
        raise MigrationSpecialModeNotAllowedError(
            f"migration is not approved for special mode: {version}"
        )
    if approved is not None and declared != approved:
        raise MigrationSpecialModeMismatchError(
            f"migration mode does not match whitelist: {version}"
        )
    return approved or NORMAL_MIGRATION_MODE


def discover_migrations(migrations_dir: str | Path) -> list[Migration]:
    """Discover and validate migration packages without opening a database."""

    root = _require_migrations_directory(migrations_dir)
    migrations: list[Migration] = []
    seen_versions: set[str] = set()
    seen_names: set[str] = set()

    for upgrade_path in sorted(root.glob("*.sql"), key=lambda item: item.name):
        if upgrade_path.name.startswith("._"):
            continue
        match = MIGRATION_FILE_RE.fullmatch(upgrade_path.name)
        if not match:
            raise MigrationDiscoveryError(
                f"invalid migration filename: {upgrade_path.name}"
            )
        version = match.group("version")
        name = match.group("name")
        if version in seen_versions:
            raise MigrationDuplicateVersionError(
                f"duplicate migration version: {version}"
            )
        if name in seen_names:
            raise MigrationDiscoveryError(f"duplicate migration name: {name}")
        forward_check_path = (
            root / "checks" / f"{upgrade_path.stem}_forward_check.sql"
        )
        if not forward_check_path.is_file():
            raise MigrationDiscoveryError(
                f"missing forward check for migration {version}: "
                f"{forward_check_path}"
            )
        if not upgrade_path.read_bytes().strip():
            raise MigrationDiscoveryError(
                f"migration SQL is empty: {upgrade_path.name}"
            )
        if not forward_check_path.read_bytes().strip():
            raise MigrationDiscoveryError(
                f"forward check is empty: {forward_check_path.name}"
            )
        migrations.append(
            Migration(
                version=version,
                name=name,
                upgrade_path=upgrade_path.resolve(),
                forward_check_path=forward_check_path.resolve(),
                checksum_sha256=_package_checksum(
                    upgrade_path, forward_check_path
                ),
                mode=_migration_mode(upgrade_path, version, name),
            )
        )
        seen_versions.add(version)
        seen_names.add(name)

    versions = [migration.version for migration in migrations]
    if versions != sorted(versions):
        raise MigrationOrderError("migration versions are not strictly ordered")
    return migrations


def _strip_sql_comments(sql: str) -> str:
    without_blocks = BLOCK_COMMENT_RE.sub("", sql)
    return LINE_COMMENT_RE.sub("", without_blocks).strip()


def split_sql_statements(sql: str) -> list[str]:
    """Split SQL safely using sqlite3's parser completeness check."""

    statements: list[str] = []
    buffer = ""
    for character in sql:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            if _strip_sql_comments(buffer):
                statements.append(buffer.strip())
            buffer = ""
    if _strip_sql_comments(buffer):
        raise MigrationDiscoveryError(
            "SQL contains an incomplete statement; terminate it with a semicolon"
        )
    return statements


def _first_keyword(statement: str) -> str:
    content = _strip_sql_comments(statement)
    match = re.match(r"([A-Za-z]+)", content)
    return match.group(1).upper() if match else ""


def _validate_upgrade_statement(statement: str) -> None:
    keyword = _first_keyword(statement)
    if not keyword:
        raise MigrationDiscoveryError("migration contains an empty SQL statement")
    if keyword in FORBIDDEN_UPGRADE_KEYWORDS:
        raise MigrationDiscoveryError(
            f"migration statement is controlled by the runner and is forbidden: "
            f"{keyword}"
        )


def _sqlite_is_busy(error: sqlite3.Error) -> bool:
    error_code = getattr(error, "sqlite_errorcode", None)
    if isinstance(error_code, int) and error_code & 0xFF in (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    ):
        return True
    message = str(error).strip().lower()
    return message in {
        "database is busy",
        "database is locked",
        "database table is locked",
        "database schema is locked",
    }


def migration_error_from_sqlite(
    error: sqlite3.Error,
    *,
    context: str = "migration operation",
) -> MigrationExecutionError:
    """Convert an SQLite exception to a controlled migration error."""

    if _sqlite_is_busy(error):
        return MigrationDatabaseBusyError(
            f"{context} could not acquire the database lock"
        )
    return MigrationExecutionError(f"{context} failed: {error}")


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    uri = f"{database_path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _connect_write(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(database_path),
        timeout=5.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _mark_execution_state(
    error: MigrationError,
    execution_state: str,
    *,
    recovery_required: bool = False,
) -> MigrationError:
    error.execution_state = execution_state
    error.recovery_required = recovery_required
    return error


def _quick_check(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA quick_check").fetchone()
    return str(row[0]) if row is not None else ""


def _verify_foreign_keys_enabled(database_path: Path) -> None:
    try:
        with closing(_connect_write(database_path)) as connection:
            enabled = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    except sqlite3.Error as exc:
        raise MigrationForeignKeysRestoreError(
            "could not open a normal FK-enforcing connection"
        ) from exc
    if enabled != 1:
        raise MigrationForeignKeysRestoreError(
            "normal migration connection did not restore foreign_keys=ON"
        )


def _validate_rebuild_backup_metadata(
    database_path: Path,
    metadata: Mapping[str, object] | None,
) -> None:
    if not isinstance(metadata, Mapping):
        raise MigrationRebuildBackupRequiredError(
            "special rebuild requires backup metadata"
        )
    try:
        backup_path = _require_existing_file(
            str(metadata["backup_path"]),
            "rebuild backup",
        )
        manifest_path = _require_existing_file(
            str(metadata["manifest_path"]),
            "rebuild backup manifest",
        )
    except (KeyError, TypeError, DatabasePathError) as exc:
        raise MigrationRebuildBackupRequiredError(
            "special rebuild backup metadata is incomplete"
        ) from exc
    if backup_path == database_path:
        raise MigrationRebuildBackupRequiredError(
            "rebuild backup must be independent from the target database"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationRebuildBackupRequiredError(
            "rebuild backup manifest could not be verified"
        ) from exc
    if not isinstance(manifest, dict):
        raise MigrationRebuildBackupRequiredError(
            "rebuild backup manifest must be an object"
        )
    expected = {
        "database_path": str(database_path),
        "backup_path": str(backup_path),
        "backup_method": "sqlite_online_backup",
        "quick_check": "ok",
        "integrity_check": "ok",
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise MigrationRebuildBackupRequiredError(
            "rebuild backup manifest does not match the target and backup"
        )
    if not str(manifest.get("created_at", "")).strip():
        raise MigrationRebuildBackupRequiredError(
            "rebuild backup manifest has no creation timestamp"
        )
    source_sha256 = str(manifest.get("source_database_sha256", ""))
    backup_sha256 = str(manifest.get("backup_sha256", ""))
    if (
        len(source_sha256) != 64
        or len(backup_sha256) != 64
        or source_sha256 != _database_sha256(database_path)
        or backup_sha256 != _database_sha256(backup_path)
    ):
        raise MigrationRebuildBackupRequiredError(
            "rebuild backup hashes do not match the manifest"
        )
    try:
        with closing(_connect_read_only(backup_path)) as backup:
            quick = _quick_check(backup)
            integrity_row = backup.execute("PRAGMA integrity_check").fetchone()
            integrity = str(integrity_row[0]) if integrity_row else ""
    except sqlite3.Error as exc:
        raise MigrationRebuildBackupRequiredError(
            "rebuild backup could not be opened independently"
        ) from exc
    if quick.lower() != "ok" or integrity.lower() != "ok":
        raise MigrationRebuildBackupRequiredError(
            "rebuild backup integrity checks did not pass"
        )


def _registry_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table' AND name='schema_migrations'"""
    ).fetchone()
    return row is not None


def _ensure_registry(connection: sqlite3.Connection) -> None:
    for statement in split_sql_statements(REGISTRY_DDL):
        connection.execute(statement)


def _validate_registry_contract(connection: sqlite3.Connection) -> None:
    expected_columns = [
        ("version", "TEXT", 0, 1),
        ("name", "TEXT", 1, 0),
        ("checksum_sha256", "TEXT", 1, 0),
        ("applied_at", "TEXT", 1, 0),
        ("execution_ms", "INTEGER", 1, 0),
        ("applied_by", "TEXT", 1, 0),
        ("source_commit", "TEXT", 1, 0),
    ]
    actual_columns = [
        (
            str(row["name"]),
            str(row["type"]).upper(),
            int(row["notnull"]),
            int(row["pk"]),
        )
        for row in connection.execute("PRAGMA table_info(schema_migrations)")
    ]
    if actual_columns != expected_columns:
        raise MigrationVerificationError(
            "schema_migrations columns do not match the runner contract"
        )

    trigger_names = {
        str(row["name"])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='trigger' AND tbl_name='schema_migrations'"""
        )
    }
    expected_triggers = {
        "schema_migrations_no_update",
        "schema_migrations_no_delete",
    }
    if not expected_triggers.issubset(trigger_names):
        raise MigrationVerificationError(
            "schema_migrations immutable protection trigger is missing"
        )


def _applied_migrations(connection: sqlite3.Connection) -> list[AppliedMigration]:
    if not _registry_exists(connection):
        return []
    _validate_registry_contract(connection)
    rows = connection.execute(
        """SELECT rowid,version,name,checksum_sha256,applied_at,execution_ms,
                  applied_by,source_commit
           FROM schema_migrations ORDER BY rowid"""
    ).fetchall()
    return [
        AppliedMigration(
            rowid=int(row["rowid"]),
            version=str(row["version"]),
            name=str(row["name"]),
            checksum_sha256=str(row["checksum_sha256"]),
            applied_at=str(row["applied_at"]),
            execution_ms=int(row["execution_ms"]),
            applied_by=str(row["applied_by"]),
            source_commit=str(row["source_commit"]),
        )
        for row in rows
    ]


def _validate_applied_state(
    migrations: list[Migration], applied: list[AppliedMigration]
) -> None:
    applied_versions = [item.version for item in applied]
    if applied_versions != sorted(applied_versions):
        raise MigrationOrderError(
            "schema_migrations row order is not strictly increasing"
        )

    migration_by_version = {item.version: item for item in migrations}
    for record in applied:
        migration = migration_by_version.get(record.version)
        if migration is None:
            raise MigrationVerificationError(
                f"applied migration file is missing: {record.version}"
            )
        if migration.name != record.name:
            raise MigrationVerificationError(
                f"applied migration name changed for {record.version}"
            )
        if migration.checksum_sha256 != record.checksum_sha256:
            raise MigrationChecksumError(
                f"checksum mismatch for applied migration {record.version}"
            )

    discovered_versions = [item.version for item in migrations]
    expected_prefix = discovered_versions[: len(applied_versions)]
    if applied_versions != expected_prefix:
        raise MigrationOrderError(
            "applied migrations are not a prefix of discovered versions"
        )


def _validate_through_boundary(
    versions: list[str], applied_versions: list[str], through: str | None
) -> None:
    """A boundary selects a prefix; it never skips or rewrites migration history."""
    if through is None:
        return
    if not isinstance(through, str) or re.fullmatch(r"\d{8}_\d{3}", through) is None or through not in versions:
        raise MigrationOrderError("through must name an exact discovered migration version")
    if applied_versions and through < applied_versions[-1]:
        raise MigrationOrderError("through cannot precede the latest applied migration")


def _migration_summary(migration: Migration) -> dict:
    result = {
        "version": migration.version,
        "name": migration.name,
        "checksum_sha256": migration.checksum_sha256,
    }
    if migration.mode != NORMAL_MIGRATION_MODE:
        result["mode"] = migration.mode
    return result


def _read_state(
    database_path: str | Path, migrations_dir: str | Path
) -> tuple[Path, list[Migration], list[AppliedMigration]]:
    database = _require_existing_file(database_path, "database")
    migrations = discover_migrations(migrations_dir)
    with closing(_connect_read_only(database)) as connection:
        applied = _applied_migrations(connection)
    _validate_applied_state(migrations, applied)
    return database, migrations, applied


def migration_status(
    database_path: str | Path, migrations_dir: str | Path
) -> dict:
    """Return applied and pending versions without writing the database."""

    database, migrations, applied = _read_state(database_path, migrations_dir)
    applied_versions = {item.version for item in applied}
    return {
        "database": str(database),
        "applied": [
            {
                "version": item.version,
                "name": item.name,
                "checksum_sha256": item.checksum_sha256,
                "applied_at": item.applied_at,
                "execution_ms": item.execution_ms,
                "applied_by": item.applied_by,
                "source_commit": item.source_commit,
            }
            for item in applied
        ],
        "pending": [
            _migration_summary(item)
            for item in migrations
            if item.version not in applied_versions
        ],
    }


def plan_migrations(
    database_path: str | Path, migrations_dir: str | Path, *, through: str | None = None
) -> dict:
    """Return a non-mutating execution plan, optionally bounded by an exact version."""

    status = migration_status(database_path, migrations_dir)
    _validate_through_boundary(
        [item["version"] for item in status["applied"] + status["pending"]],
        [item["version"] for item in status["applied"]], through,
    )
    result = {
        "database": status["database"],
        "already_applied": status["applied"],
        "will_apply": [item for item in status["pending"] if through is None or item["version"] <= through],
    }
    if through is not None:
        result.update(through=through, pending=[item for item in status["pending"] if item["version"] > through])
    return result


def _foreign_key_rows(connection: sqlite3.Connection) -> tuple[tuple, ...]:
    rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    return tuple(
        sorted(
            (
                str(row[0]),
                int(row[1]),
                str(row[2]),
                int(row[3]),
            )
            for row in rows
        )
    )


def foreign_key_violations(database_path: str | Path) -> tuple[tuple, ...]:
    """Return a stable read-only snapshot of current FK violations."""

    database = _require_existing_file(database_path, "database")
    with closing(_connect_read_only(database)) as connection:
        return _foreign_key_rows(connection)


def _run_forward_check(
    connection: sqlite3.Connection, migration: Migration, *, read_only: bool
) -> None:
    statements = split_sql_statements(
        migration.forward_check_path.read_text(encoding="utf-8")
    )
    if not statements:
        raise MigrationForwardCheckError(
            f"forward check has no statements: {migration.version}"
        )

    if not read_only:
        connection.execute("PRAGMA query_only=ON")
    try:
        for statement in statements:
            cursor = connection.execute(statement)
            if cursor.description is None:
                raise MigrationForwardCheckError(
                    f"forward check is not read-only: {migration.version}"
                )
            rows = cursor.fetchall()
            if not rows or any(row[0] not in (1, True, "ok", "OK") for row in rows):
                raise MigrationForwardCheckError(
                    f"forward check failed: {migration.version}"
                )
    except sqlite3.Error as exc:
        raise MigrationForwardCheckError(
            f"forward check failed for {migration.version}: {exc}"
        ) from exc
    finally:
        if not read_only:
            connection.execute("PRAGMA query_only=OFF")


def verify_migrations(
    database_path: str | Path, migrations_dir: str | Path
) -> dict:
    """Verify registry/files and the latest cumulative forward contract.

    Every forward check runs transactionally when its migration is applied.
    A later migration's check is the cumulative contract for that schema
    version; historical checks may intentionally assert that future objects
    do not exist and therefore must not be replayed against a newer schema.
    """

    database = _require_existing_file(database_path, "database")
    migrations = discover_migrations(migrations_dir)
    with closing(_connect_read_only(database)) as connection:
        applied = _applied_migrations(connection)
        _validate_applied_state(migrations, applied)
        migration_by_version = {item.version: item for item in migrations}
        if applied:
            record = applied[-1]
            _run_forward_check(
                connection,
                migration_by_version[record.version],
                read_only=True,
            )
        foreign_keys = _foreign_key_rows(connection)
    applied_versions = {item.version for item in applied}
    return {
        "database": str(database),
        "verified": [item.version for item in applied],
        "pending": [
            _migration_summary(item)
            for item in migrations
            if item.version not in applied_versions
        ],
        "foreign_key_violations": [list(item) for item in foreign_keys],
    }


def _database_sha256(database_path: Path) -> str:
    digest = hashlib.sha256()
    with database_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _guard_formal_database(
    database_path: Path,
    formal_database_path: str | Path | None,
    *,
    allow_formal_database: bool,
    confirmation: str | None,
    expected_database_sha256: str | None,
) -> None:
    if formal_database_path is None:
        return
    formal_path = Path(formal_database_path).expanduser().resolve()
    if database_path != formal_path:
        return
    if not allow_formal_database:
        raise MigrationFormalDatabaseConfirmationError(
            "formal database apply is blocked without --allow-formal-database"
        )
    if confirmation != FORMAL_CONFIRMATION:
        raise MigrationFormalDatabaseConfirmationError(
            f"formal database apply requires confirmation {FORMAL_CONFIRMATION}"
        )
    if not expected_database_sha256:
        raise MigrationFormalDatabaseConfirmationError(
            "formal database apply requires --expected-database-sha256"
        )
    actual_sha256 = _database_sha256(database_path)
    if actual_sha256 != expected_database_sha256:
        raise MigrationFormalDatabaseConfirmationError(
            "formal database SHA-256 does not match the expected value"
        )


def _execute_upgrade(
    connection: sqlite3.Connection, migration: Migration
) -> None:
    sql = migration.upgrade_path.read_text(encoding="utf-8")
    statements = split_sql_statements(sql)
    if not statements:
        raise MigrationDiscoveryError(
            f"migration has no executable statements: {migration.version}"
        )
    for statement in statements:
        _validate_upgrade_statement(statement)
        connection.execute(statement)


def _insert_applied_migration(
    connection: sqlite3.Connection,
    migration: Migration,
    *,
    execution_ms: int,
    applied_by: str,
    source_commit: str,
) -> None:
    connection.execute(
        """INSERT INTO schema_migrations(
               version,name,checksum_sha256,applied_at,execution_ms,
               applied_by,source_commit
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            migration.version,
            migration.name,
            migration.checksum_sha256,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            execution_ms,
            applied_by,
            source_commit,
        ),
    )


def _apply_one(
    connection: sqlite3.Connection,
    migration: Migration,
    *,
    applied_by: str,
    source_commit: str,
) -> None:
    started = time.perf_counter()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_registry(connection)
        before_foreign_keys = set(_foreign_key_rows(connection))
        _execute_upgrade(connection, migration)
        _run_forward_check(connection, migration, read_only=False)
        after_foreign_keys = set(_foreign_key_rows(connection))
        added_foreign_keys = after_foreign_keys - before_foreign_keys
        if added_foreign_keys:
            raise MigrationForeignKeyError(
                f"migration {migration.version} introduced foreign-key "
                f"violations: {sorted(added_foreign_keys)}"
            )
        execution_ms = max(0, int((time.perf_counter() - started) * 1000))
        _insert_applied_migration(
            connection,
            migration,
            execution_ms=execution_ms,
            applied_by=applied_by,
            source_commit=source_commit,
        )
        connection.commit()
    except MigrationError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise migration_error_from_sqlite(
            exc,
            context=f"migration {migration.version}",
        ) from exc
    except Exception:
        connection.rollback()
        raise


def _bootstrap_registry(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_registry(connection)
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise migration_error_from_sqlite(
            exc,
            context="schema_migrations bootstrap",
        ) from exc


def _special_forward_contract_satisfied(
    database_path: Path,
    migration: Migration,
) -> bool:
    try:
        with closing(_connect_read_only(database_path)) as connection:
            _run_forward_check(connection, migration, read_only=True)
        return True
    except MigrationForwardCheckError:
        return False


def _special_registry_matches(
    database_path: Path,
    migration: Migration,
) -> bool:
    try:
        with closing(_connect_read_only(database_path)) as connection:
            if not _registry_exists(connection):
                return False
            row = connection.execute(
                """SELECT name,checksum_sha256
                   FROM schema_migrations WHERE version=?""",
                (migration.version,),
            ).fetchone()
            return (
                row is not None
                and str(row["name"]) == migration.name
                and str(row["checksum_sha256"]) == migration.checksum_sha256
            )
    except sqlite3.Error:
        return False


def _begin_special_migration(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")


def _disable_foreign_keys_for_rebuild(
    connection: sqlite3.Connection,
) -> None:
    connection.execute("PRAGMA foreign_keys=OFF")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise MigrationForeignKeysOffError(
            "dedicated rebuild connection retained foreign_keys=ON"
        )


def _commit_special_migration(connection: sqlite3.Connection) -> None:
    connection.commit()


def _post_validate_special_migration(
    database_path: Path,
    migration: Migration,
    before_foreign_keys: set[tuple],
) -> None:
    _verify_foreign_keys_enabled(database_path)
    try:
        with closing(_connect_write(database_path)) as connection:
            after_foreign_keys = set(_foreign_key_rows(connection))
            if after_foreign_keys != before_foreign_keys:
                raise MigrationPostCommitForeignKeyError(
                    "committed rebuild changed foreign-key violations"
                )
            if _quick_check(connection).lower() != "ok":
                raise MigrationPostCommitQuickCheckError(
                    "committed rebuild failed quick_check"
                )
            _run_forward_check(connection, migration, read_only=False)
            row = connection.execute(
                """SELECT name,checksum_sha256
                   FROM schema_migrations WHERE version=?""",
                (migration.version,),
            ).fetchone()
            if (
                row is None
                or str(row["name"]) != migration.name
                or str(row["checksum_sha256"]) != migration.checksum_sha256
            ):
                raise MigrationAppliedUnregisteredError(
                    "committed rebuild registry row is missing or mismatched"
                )
    except MigrationError:
        raise
    except sqlite3.Error as exc:
        raise MigrationPostCommitQuickCheckError(
            "committed rebuild post-validation failed"
        ) from exc


def _apply_special_rebuild(
    database_path: Path,
    migration: Migration,
    *,
    applied_by: str,
    source_commit: str,
    required_backup_metadata: Mapping[str, object] | None,
) -> None:
    if migration.mode != SQLITE_FK_OFF_REBUILD_MODE:
        raise MigrationSpecialModeMismatchError(
            f"unsupported special migration mode: {migration.mode}"
        )
    _validate_rebuild_backup_metadata(
        database_path,
        required_backup_metadata,
    )
    if _special_forward_contract_satisfied(database_path, migration):
        raise MigrationAppliedUnregisteredError(
            f"special migration schema exists without registry: {migration.version}"
        )

    started = time.perf_counter()
    connection: sqlite3.Connection | None = None
    committed = False
    commit_attempted = False
    before_foreign_keys: set[tuple] = set()
    try:
        connection = sqlite3.connect(
            str(database_path),
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        _disable_foreign_keys_for_rebuild(connection)
        if migration.version in {
            "20260813_020",
            "20260816_023",
            "20260820_027",
            "20260824_029",
        }:
            connection.execute("PRAGMA legacy_alter_table=ON")
        if _quick_check(connection).lower() != "ok":
            raise MigrationExecutionError(
                "target database quick_check failed before rebuild"
            )
        before_foreign_keys = set(_foreign_key_rows(connection))
        _begin_special_migration(connection)
        _ensure_registry(connection)
        _execute_upgrade(connection, migration)
        _run_forward_check(connection, migration, read_only=False)
        after_foreign_keys = set(_foreign_key_rows(connection))
        if after_foreign_keys != before_foreign_keys:
            raise MigrationRebuildReferenceMismatchError(
                "rebuild changed foreign-key violations before commit"
            )
        if _quick_check(connection).lower() != "ok":
            raise MigrationExecutionError(
                "rebuild quick_check failed before commit"
            )
        execution_ms = max(0, int((time.perf_counter() - started) * 1000))
        _insert_applied_migration(
            connection,
            migration,
            execution_ms=execution_ms,
            applied_by=applied_by,
            source_commit=source_commit,
        )
        commit_attempted = True
        _commit_special_migration(connection)
        committed = True
    except MigrationError as exc:
        if connection is not None and not committed:
            connection.rollback()
        if not committed:
            _mark_execution_state(exc, "rolled_back")
            if connection is not None:
                connection.close()
                connection = None
            _verify_foreign_keys_enabled(database_path)
        raise
    except sqlite3.Error as exc:
        if connection is not None and not committed:
            connection.rollback()
        if commit_attempted:
            if connection is not None:
                connection.close()
                connection = None
            if _special_registry_matches(database_path, migration):
                error = MigrationRecoveryRequiredError(
                    "special migration commit outcome requires recovery"
                )
                _verify_foreign_keys_enabled(database_path)
                raise error from exc
        error = migration_error_from_sqlite(
            exc,
            context=f"special migration {migration.version}",
        )
        if not committed:
            _mark_execution_state(error, "rolled_back")
            if connection is not None:
                connection.close()
                connection = None
            _verify_foreign_keys_enabled(database_path)
        raise error from exc
    finally:
        if connection is not None:
            connection.close()

    try:
        _post_validate_special_migration(
            database_path,
            migration,
            before_foreign_keys,
        )
    except MigrationError as exc:
        _mark_execution_state(
            exc,
            "applied_but_post_validation_failed",
            recovery_required=True,
        )
        raise


def apply_migrations(
    database_path: str | Path,
    migrations_dir: str | Path,
    *,
    applied_by: str,
    source_commit: str,
    formal_database_path: str | Path | None = DEFAULT_FORMAL_DATABASE_PATH,
    allow_formal_database: bool = False,
    confirmation: str | None = None,
    expected_database_sha256: str | None = None,
    required_backup_metadata: Mapping[str, object] | None = None,
    through: str | None = None,
) -> dict:
    """Apply a validated pending prefix; default remains all pending migrations."""

    database = _require_existing_file(database_path, "database")
    if not str(applied_by).strip():
        raise MigrationSafetyError("applied_by must be non-empty")
    if not str(source_commit).strip():
        raise MigrationSafetyError("source_commit must be non-empty")
    _guard_formal_database(
        database,
        formal_database_path,
        allow_formal_database=allow_formal_database,
        confirmation=confirmation,
        expected_database_sha256=expected_database_sha256,
    )
    migrations = discover_migrations(migrations_dir)
    if through is not None:
        # Reject invalid boundaries before even opening a write connection.
        # Validate the entire history, including files beyond the requested end.
        with closing(_connect_read_only(database)) as preflight:
            prior = _applied_migrations(preflight)
        _validate_applied_state(migrations, prior)
        _validate_through_boundary([item.version for item in migrations], [item.version for item in prior], through)

    connection: sqlite3.Connection | None = None
    execution_states: dict[str, str] = {}
    try:
        connection = _connect_write(database)
        applied = _applied_migrations(connection)
        _validate_applied_state(migrations, applied)
        _validate_through_boundary([item.version for item in migrations], [item.version for item in applied], through)
        applied_versions = {item.version for item in applied}
        remaining = [item.version for item in migrations if item.version not in applied_versions and through is not None and item.version > through]
        pending = [
            migration
            for migration in migrations
            if migration.version not in applied_versions and (through is None or migration.version <= through)
        ]
        if not pending and not _registry_exists(connection):
            _bootstrap_registry(connection)
        applied_now: list[str] = []
        for migration in pending:
            if migration.mode == SQLITE_FK_OFF_REBUILD_MODE:
                connection.close()
                connection = None
                _apply_special_rebuild(
                    database,
                    migration,
                    applied_by=str(applied_by).strip(),
                    source_commit=str(source_commit).strip(),
                    required_backup_metadata=required_backup_metadata,
                )
                execution_states[migration.version] = "applied_and_registered"
                connection = _connect_write(database)
            else:
                _apply_one(
                    connection,
                    migration,
                    applied_by=str(applied_by).strip(),
                    source_commit=str(source_commit).strip(),
                )
            applied_now.append(migration.version)
    except MigrationError:
        raise
    except sqlite3.Error as exc:
        raise migration_error_from_sqlite(exc) from exc
    finally:
        if connection is not None:
            connection.close()

    result = {
        "database": str(database),
        "applied": applied_now,
        "already_applied": [item.version for item in applied],
        "pending": remaining,
    }
    if through is not None:
        result["through"] = through
    if execution_states:
        result["execution_states"] = execution_states
    return result
