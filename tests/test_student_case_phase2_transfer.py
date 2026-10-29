from contextlib import closing
from dataclasses import replace
from http import HTTPStatus
import hashlib
import importlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

import migration_runner
from student_case_feature_flags import StudentCaseFeatureFlags
import student_case_permissions
import student_case_service
from tests import student_lifecycle_test_support as lifecycle_support


try:
    transfer_case_errors = importlib.import_module("transfer_case_errors")
    transfer_case_repository = importlib.import_module(
        "transfer_case_repository"
    )
    transfer_case_service = importlib.import_module("transfer_case_service")
except ModuleNotFoundError:
    transfer_case_errors = None
    transfer_case_repository = None
    transfer_case_service = None

try:
    transfer_arrangement_dto = importlib.import_module(
        "transfer_arrangement_dto"
    )
    transfer_arrangement_errors = importlib.import_module(
        "transfer_arrangement_errors"
    )
    transfer_arrangement_repository = importlib.import_module(
        "transfer_arrangement_repository"
    )
    transfer_arrangement_service = importlib.import_module(
        "transfer_arrangement_service"
    )
except ModuleNotFoundError:
    transfer_arrangement_dto = None
    transfer_arrangement_errors = None
    transfer_arrangement_repository = None
    transfer_arrangement_service = None

try:
    transfer_case_views = importlib.import_module("transfer_case_views")
except ModuleNotFoundError:
    transfer_case_views = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
MIGRATION_PATH = (
    MIGRATIONS_DIR / "20260729_002_student_case_phase2_transfer.sql"
)
FORWARD_CHECK_PATH = (
    MIGRATIONS_DIR
    / "checks"
    / "20260729_002_student_case_phase2_transfer_forward_check.sql"
)
TITLE_MIGRATION_PATH = (
    MIGRATIONS_DIR / "20260729_003_transfer_case_title.sql"
)
TITLE_FORWARD_CHECK_PATH = (
    MIGRATIONS_DIR
    / "checks"
    / "20260729_003_transfer_case_title_forward_check.sql"
)
PROOF_MIGRATION_PATH = (
    MIGRATIONS_DIR / "20260729_004_transfer_payment_requirement_proofs.sql"
)
PROOF_FORWARD_CHECK_PATH = (
    MIGRATIONS_DIR
    / "checks"
    / "20260729_004_transfer_payment_requirement_proofs_forward_check.sql"
)
ARRANGEMENTS_MIGRATION_PATH = (
    MIGRATIONS_DIR / "20260729_005_transfer_arrangements.sql"
)
ARRANGEMENTS_FORWARD_CHECK_PATH = (
    MIGRATIONS_DIR
    / "checks"
    / "20260729_005_transfer_arrangements_forward_check.sql"
)
CONFIRMATION_REBUILD_MIGRATION_PATH = (
    MIGRATIONS_DIR
    / "20260730_006_rebuild_case_arrangement_confirmations.sql"
)
CONFIRMATION_REBUILD_FORWARD_CHECK_PATH = (
    MIGRATIONS_DIR
    / "checks"
    / "20260730_006_rebuild_case_arrangement_confirmations_forward_check.sql"
)
COMPLETION_MIGRATION_PATH = (
    MIGRATIONS_DIR / "20260730_007_transfer_completion_contract.sql"
)
COMPLETION_FORWARD_CHECK_PATH = (
    MIGRATIONS_DIR
    / "checks"
    / "20260730_007_transfer_completion_contract_forward_check.sql"
)
PROGRESSION_MIGRATION_PATH = (
    MIGRATIONS_DIR / "20260801_008_progression_case_management.sql"
)
PROGRESSION_FORWARD_CHECK_PATH = (
    MIGRATIONS_DIR
    / "checks"
    / "20260801_008_progression_case_management_forward_check.sql"
)
PHASE1_MIGRATION_PATH = (
    MIGRATIONS_DIR / "20260728_001_student_case_phase1.sql"
)
PHASE1_FORWARD_CHECK_PATH = (
    MIGRATIONS_DIR
    / "checks"
    / "20260728_001_student_case_phase1_forward_check.sql"
)
T0_BACKUP_PATH = (
    PROJECT_ROOT
    / "database"
    / "backups"
    / "aaie_crm_student_case_phase2_transfer_before_t0_20260729_111631.db"
)
T0_BACKUP_SHA256 = (
    "d8ee140b29185e16d1e51c61f47f94f437e28b53c7a0f6c7f9eb166bb4c618c6"
)

PHASE1_TABLES = {
    "student_case_sequences",
    "student_cases",
    "student_case_assignments",
    "case_events",
    "student_school_enrolments",
}
PHASE2_TABLES = {
    "transfer_cases",
    "transfer_school_applications",
    "case_record_links",
    "case_material_items",
    "case_payment_requirements",
    "case_arrangement_confirmations",
    "case_completion_confirmations",
    "student_source_tags",
    "external_transfer_case_requests",
    "case_payment_requirement_proofs",
    "transfer_guardianship_arrangements",
    "transfer_accommodation_arrangements",
    "case_arrangement_evidence_links",
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_all_test_migrations_with_rebuild_backup(
    database_path, root, *, label, include_023=True,
    before_transfer_cutover=None
):
    migration_dir = Path(root) / f"{label}-migrations-through-005"
    checks = migration_dir / "checks"
    checks.mkdir(parents=True)
    for source in (
        PHASE1_MIGRATION_PATH,
        MIGRATION_PATH,
        TITLE_MIGRATION_PATH,
        PROOF_MIGRATION_PATH,
        ARRANGEMENTS_MIGRATION_PATH,
    ):
        shutil.copy2(source, migration_dir / source.name)
    for source in (
        PHASE1_FORWARD_CHECK_PATH,
        FORWARD_CHECK_PATH,
        TITLE_FORWARD_CHECK_PATH,
        PROOF_FORWARD_CHECK_PATH,
        ARRANGEMENTS_FORWARD_CHECK_PATH,
    ):
        shutil.copy2(source, checks / source.name)

    through_022_result = migration_runner.apply_migrations(
        database_path,
        migration_dir,
        applied_by="phase2-transfer-test",
        source_commit="phase2-transfer-test",
        formal_database_path=None,
    )

    database_path = Path(database_path).resolve()

    def rebuild_backup(stage):
        backup_path = (
            Path(root) / f"{label}-before-{stage}.db"
        ).resolve()
        with closing(sqlite3.connect(database_path)) as source:
            with closing(sqlite3.connect(backup_path)) as target:
                source.backup(target)
        with closing(sqlite3.connect(backup_path)) as backup:
            quick_check = backup.execute("PRAGMA quick_check").fetchone()[0]
            integrity_check = backup.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
        manifest_path = backup_path.with_suffix(".manifest.json")
        manifest_path.write_text(
            json.dumps(
                {
                    "database_path": str(database_path),
                    "backup_path": str(backup_path),
                    "backup_method": "sqlite_online_backup",
                    "created_at": "2026-07-30T07:00:00+10:00",
                    "source_database_sha256": sha256(database_path),
                    "backup_sha256": sha256(backup_path),
                    "quick_check": quick_check,
                    "integrity_check": integrity_check,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return {
            "backup_path": str(backup_path),
            "manifest_path": str(manifest_path),
        }

    migration_through_006 = (
        Path(root) / f"{label}-migrations-through-006"
    )
    checks_006 = migration_through_006 / "checks"
    shutil.copytree(migration_dir, migration_through_006)
    shutil.copy2(
        CONFIRMATION_REBUILD_MIGRATION_PATH,
        migration_through_006 / CONFIRMATION_REBUILD_MIGRATION_PATH.name,
    )
    shutil.copy2(
        CONFIRMATION_REBUILD_FORWARD_CHECK_PATH,
        checks_006 / CONFIRMATION_REBUILD_FORWARD_CHECK_PATH.name,
    )
    migration_runner.apply_migrations(
        database_path,
        migration_through_006,
        applied_by="phase2-transfer-test",
        source_commit="phase2-transfer-test",
        formal_database_path=None,
        required_backup_metadata=rebuild_backup("006"),
    )
    migrations_before_010 = (
        Path(root) / f"{label}-migrations-before-010"
    )
    checks_before_010 = migrations_before_010 / "checks"
    checks_before_010.mkdir(parents=True)
    for migration in migration_runner.discover_migrations(MIGRATIONS_DIR):
        if migration.version >= "20260804_010":
            break
        shutil.copy2(
            migration.upgrade_path,
            migrations_before_010 / migration.upgrade_path.name,
        )
        shutil.copy2(
            migration.forward_check_path,
            checks_before_010 / migration.forward_check_path.name,
        )
    migration_runner.apply_migrations(
        database_path,
        migrations_before_010,
        applied_by="phase2-transfer-test",
        source_commit="phase2-transfer-test",
        formal_database_path=None,
        required_backup_metadata=rebuild_backup("007"),
    )
    migrations_before_020 = (
        Path(root) / f"{label}-migrations-before-020"
    )
    checks_before_020 = migrations_before_020 / "checks"
    checks_before_020.mkdir(parents=True)
    for migration in migration_runner.discover_migrations(MIGRATIONS_DIR):
        if migration.version >= "20260813_020":
            break
        shutil.copy2(
            migration.upgrade_path,
            migrations_before_020 / migration.upgrade_path.name,
        )
        shutil.copy2(
            migration.forward_check_path,
            checks_before_020 / migration.forward_check_path.name,
        )
    migration_runner.apply_migrations(
        database_path,
        migrations_before_020,
        applied_by="phase2-transfer-test",
        source_commit="phase2-transfer-test",
        formal_database_path=None,
        required_backup_metadata=rebuild_backup("010"),
    )
    migrations_before_023 = (
        Path(root) / f"{label}-migrations-before-023"
    )
    checks_before_023 = migrations_before_023 / "checks"
    checks_before_023.mkdir(parents=True)
    for migration in migration_runner.discover_migrations(MIGRATIONS_DIR):
        if migration.version >= "20260816_023":
            break
        shutil.copy2(
            migration.upgrade_path,
            migrations_before_023 / migration.upgrade_path.name,
        )
        shutil.copy2(
            migration.forward_check_path,
            checks_before_023 / migration.forward_check_path.name,
        )
    migration_runner.apply_migrations(
        database_path,
        migrations_before_023,
        applied_by="phase2-transfer-test",
        source_commit="phase2-transfer-test",
        formal_database_path=None,
        required_backup_metadata=rebuild_backup("020"),
    )
    if not include_023:
        return through_022_result
    # Later cross-module migrations run against the application's base schema,
    # where Reminder Center already exists. This transfer-only fixture creates
    # the minimal pre-026 shape before asking the runner to apply every package.
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS reminders(
                 id INTEGER PRIMARY KEY,
                 reminder_type TEXT NOT NULL,
                 status TEXT NOT NULL DEFAULT 'Pending',
                 due_date TEXT,
                 due_datetime TEXT,
                 reminder_datetime TEXT,
                 snooze_until TEXT,
                 completed_at TEXT,
                 completed_by INTEGER,
                 dismissed_at TEXT,
                 dismissed_by INTEGER,
                 source_type TEXT,
                 source_record_id TEXT,
                 related_record_id TEXT,
                 notes TEXT,
                 auto_generated INTEGER NOT NULL DEFAULT 0
               )"""
        )
    migrations_before_027 = (
        Path(root) / f"{label}-migrations-before-027"
    )
    checks_before_027 = migrations_before_027 / "checks"
    checks_before_027.mkdir(parents=True)
    for migration in migration_runner.discover_migrations(MIGRATIONS_DIR):
        if migration.version >= "20260820_027":
            break
        shutil.copy2(
            migration.upgrade_path,
            migrations_before_027 / migration.upgrade_path.name,
        )
        shutil.copy2(
            migration.forward_check_path,
            checks_before_027 / migration.forward_check_path.name,
        )
    migration_runner.apply_migrations(
        database_path,
        migrations_before_027,
        applied_by="phase2-transfer-test",
        source_commit="phase2-transfer-test",
        formal_database_path=None,
        required_backup_metadata=rebuild_backup("023"),
    )
    migrations_before_029 = (
        Path(root) / f"{label}-migrations-before-029"
    )
    checks_before_029 = migrations_before_029 / "checks"
    checks_before_029.mkdir(parents=True)
    for migration in migration_runner.discover_migrations(MIGRATIONS_DIR):
        if migration.version >= "20260824_029":
            break
        shutil.copy2(
            migration.upgrade_path,
            migrations_before_029 / migration.upgrade_path.name,
        )
        shutil.copy2(
            migration.forward_check_path,
            checks_before_029 / migration.forward_check_path.name,
        )
    migration_runner.apply_migrations(
        database_path,
        migrations_before_029,
        applied_by="phase2-transfer-test",
        source_commit="phase2-transfer-test",
        formal_database_path=None,
        required_backup_metadata=rebuild_backup("027"),
    )
    migrations_before_035 = (
        Path(root) / f"{label}-migrations-before-035"
    )
    checks_before_035 = migrations_before_035 / "checks"
    checks_before_035.mkdir(parents=True)
    for migration in migration_runner.discover_migrations(MIGRATIONS_DIR):
        if migration.version >= "20260904_035":
            break
        shutil.copy2(
            migration.upgrade_path,
            migrations_before_035 / migration.upgrade_path.name,
        )
        shutil.copy2(
            migration.forward_check_path,
            checks_before_035 / migration.forward_check_path.name,
        )
    migration_runner.apply_migrations(
        database_path,
        migrations_before_035,
        applied_by="phase2-transfer-test",
        source_commit="phase2-transfer-test",
        formal_database_path=None,
        required_backup_metadata=rebuild_backup("029"),
    )
    if before_transfer_cutover is not None:
        # Create explicit synthetic legacy facts before the real Transfer 035
        # cutover so compatibility tests exercise the historical path.
        before_transfer_cutover()
    # The final catalogue contains two special rebuild packages. Apply the
    # first boundary with its own fresh backup; otherwise its committed write
    # makes one shared backup manifest stale before the second rebuild starts.
    migration_runner.apply_migrations(
        database_path,
        MIGRATIONS_DIR,
        applied_by="phase2-transfer-test",
        source_commit="phase2-transfer-test",
        formal_database_path=None,
        required_backup_metadata=rebuild_backup("035-transfer"),
        through="20260904_035",
    )
    # This cross-module fixture intentionally starts from a small legacy
    # schema. Materialize the application's canonical neighbouring columns
    # before the Visa rebuild, matching the real pre-Visa database shape.
    from database import SCHEMA
    with closing(sqlite3.connect(":memory:")) as canonical:
        canonical.executescript(SCHEMA)
        base_columns = {
            table: canonical.execute(f"PRAGMA table_info({table})").fetchall()
            for table in (
                "visa_cases", "payments", "student_documents", "visas",
                "users", "contracts", "students", "offers", "coe_oshc",
                "parent_form_tokens",
            )
        }
    with closing(sqlite3.connect(database_path)) as connection:
        for table, columns in base_columns.items():
            existing = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for _cid, name, column_type, _notnull, default, _pk in columns:
                if name in existing:
                    continue
                definition = column_type
                if default is not None:
                    definition += " DEFAULT " + str(default)
                connection.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}'
                )
        connection.execute(
            "UPDATE student_documents SET original_name='Document #' || id "
            "WHERE original_name IS NULL"
        )
        for statement in migration_runner.split_sql_statements(SCHEMA):
            if re.match(
                r"CREATE TABLE IF NOT EXISTS visa_evidence_profiles\b",
                statement.strip(),
                re.I,
            ):
                connection.execute(statement)
                break
        connection.commit()
        from tests.visa_test_schema import prepare_production_visa_schema
        prepare_production_visa_schema(connection)
    return migration_runner.apply_migrations(
        database_path,
        MIGRATIONS_DIR,
        applied_by="phase2-transfer-test",
        source_commit="phase2-transfer-test",
        formal_database_path=None,
        required_backup_metadata=rebuild_backup("035-visa"),
    )


class TransferPhase2MigrationTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "fixture.db"
        self._backup_counter = 0
        self._migration_case_counter = 0
        self._migration_stage_counter = 0
        self._create_legacy_fixture(self.database_path)

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def connect(path):
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _create_legacy_fixture(self, path):
        with closing(self.connect(path)) as connection:
            connection.executescript(
                """
                CREATE TABLE users(
                    id INTEGER PRIMARY KEY,
                    username TEXT,
                    display_name TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE reminders(
                    id INTEGER PRIMARY KEY,
                    reminder_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Pending',
                    due_date TEXT,
                    due_datetime TEXT,
                    reminder_datetime TEXT,
                    snooze_until TEXT,
                    completed_at TEXT,
                    completed_by INTEGER,
                    dismissed_at TEXT,
                    dismissed_by INTEGER,
                    source_type TEXT,
                    source_record_id TEXT,
                    related_record_id TEXT,
                    notes TEXT,
                    auto_generated INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE students(student_id TEXT PRIMARY KEY);
                CREATE TABLE parent_form_tokens(
                    token TEXT PRIMARY KEY,
                    student_id TEXT REFERENCES students(student_id),
                    invalidated INTEGER NOT NULL DEFAULT 0,
                    used INTEGER NOT NULL DEFAULT 0,
                    submitted_at TEXT
                );
                CREATE TABLE schools(id INTEGER PRIMARY KEY);
                CREATE TABLE school_cities(
                    id INTEGER PRIMARY KEY,
                    city_name TEXT NOT NULL
                );
                CREATE TABLE school_plan_reports(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL REFERENCES students(student_id)
                );
                CREATE TABLE school_plan_report_links(
                    id INTEGER PRIMARY KEY,
                    report_id INTEGER NOT NULL REFERENCES school_plan_reports(id)
                );
                CREATE TABLE school_plan_report_confirmations(
                    id INTEGER PRIMARY KEY,
                    report_id INTEGER NOT NULL REFERENCES school_plan_reports(id)
                );
                CREATE TABLE contracts(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT REFERENCES students(student_id),
                    contract_type TEXT,
                    contract_status TEXT,
                    signed_at TEXT,
                    fully_executed_at TEXT,
                    final_signed_file_path TEXT,
                    deleted_at TEXT,
                    voided_at TEXT
                );
                CREATE TABLE contract_signing_links(
                    id INTEGER PRIMARY KEY,
                    contract_id INTEGER REFERENCES contracts(id)
                );
                CREATE TABLE offers(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL REFERENCES students(student_id)
                );
                CREATE TABLE coe_oshc(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL REFERENCES students(student_id),
                    document_kind TEXT NOT NULL
                );
                CREATE TABLE visas(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL REFERENCES students(student_id),
                    visa_status TEXT
                );
                CREATE TABLE visa_cases (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(student_id),
  application_id TEXT, visa_case_name TEXT NOT NULL, visa_type TEXT, applicant_name TEXT,
  relationship_to_student TEXT, application_type TEXT, visa_status TEXT NOT NULL DEFAULT '准备中',
  submitted_date TEXT, grant_date TEXT, refusal_date TEXT, visa_start_date TEXT, visa_expiry_date TEXT,
  trn TEXT, hap_id TEXT, grant_number TEXT, visa_conditions TEXT,
  coe_expiry_date TEXT, oshc_expiry_date TEXT, caaw_expiry_date TEXT, s56_deadline TEXT,
  responsible_consultant_id INTEGER REFERENCES users(id), responsible_consultant_name TEXT,
  school_name TEXT, state TEXT, internal_notes TEXT, risk_level TEXT,
  final_result TEXT NOT NULL DEFAULT 'Pending', refusal_reason_category TEXT,
  is_included_in_analytics INTEGER NOT NULL DEFAULT 1, file_completion INTEGER NOT NULL DEFAULT 0,
  legacy_visa_id INTEGER REFERENCES visas(id), created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL, updated_by INTEGER REFERENCES users(id), updated_at TEXT NOT NULL
, case_type TEXT DEFAULT 'Student Visa', status TEXT DEFAULT 'Draft', visa_subclass TEXT, lodgement_date TEXT, expiry_date TEXT, s56_due_date TEXT, decision_status TEXT DEFAULT 'Pending', notes TEXT, archived_at TEXT, responsible_consultant TEXT, main_applicant TEXT, guardian_applicant TEXT, applicant_count INTEGER DEFAULT 1, case_owner TEXT, target_lodgement_date TEXT, actual_lodgement_date TEXT, result_date TEXT, result_status TEXT DEFAULT 'Pending');
                CREATE INDEX idx_visa_cases_application ON visa_cases(application_id);
                CREATE INDEX idx_visa_cases_expiry ON visa_cases(visa_expiry_date, coe_expiry_date, oshc_expiry_date, caaw_expiry_date, s56_deadline);
                CREATE INDEX idx_visa_cases_legacy ON visa_cases(legacy_visa_id);
                CREATE INDEX idx_visa_cases_status ON visa_cases(visa_status, final_result, risk_level);
                CREATE INDEX idx_visa_cases_student ON visa_cases(student_id, application_id, updated_at DESC);
                CREATE TABLE visa_evidence_profiles(
                    id INTEGER PRIMARY KEY,
                    visa_case_id INTEGER NOT NULL UNIQUE REFERENCES visa_cases(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE payments(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL REFERENCES students(student_id),
                    amount REAL,
                    paid_date TEXT,
                    receipt_file_path TEXT
                );
                CREATE TABLE student_documents(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL REFERENCES students(student_id),
                    document_type TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE content_scripts(
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL
                );
                CREATE TABLE content_materials(
                    id INTEGER PRIMARY KEY,
                    script_id INTEGER REFERENCES content_scripts(id),
                    source_path TEXT,
                    updated_at TEXT
                );
                CREATE TABLE roles(
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    permissions_json TEXT NOT NULL
                );
                CREATE TABLE system_settings(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE legacy_probe(
                    id INTEGER PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO users(id,username,display_name,is_active) VALUES
                    (1,'admin','Admin',1),
                    (2,'consultant','Consultant',1);
                INSERT INTO students(student_id)
                    VALUES('student-1'),('student-2'),('1001'),('1002');
                INSERT INTO schools(id) VALUES(10),(20);
                INSERT INTO contracts(id,student_id)
                    VALUES(100,'student-1'),(200,'student-2');
                INSERT INTO offers(id,student_id)
                    VALUES(1000,'student-1'),(1001,'student-1'),(2000,'student-2');
                INSERT INTO coe_oshc(id,student_id,document_kind) VALUES
                    (1100,'student-1','COE'),
                    (1101,'student-1','COE'),
                    (1200,'student-1','LetterOfConfirmation'),
                    (1201,'student-1','LetterOfConfirmation'),
                    (1300,'student-1','OSHC'),
                    (1301,'student-1','OSHC'),
                    (2100,'student-2','COE');
                INSERT INTO visas(id,student_id)
                    VALUES(1400,'student-1'),(1401,'student-1'),(2400,'student-2');
                INSERT INTO payments(id,student_id)
                    VALUES(1500,'student-1'),(1501,'student-1'),(2500,'student-2');
                INSERT INTO student_documents(id,student_id)
                    VALUES(1600,'student-1'),(1601,'student-1'),(2600,'student-2');
                INSERT INTO roles(id,name,permissions_json)
                    VALUES(1,'Admin','["*","student_cases.view"]');
                INSERT INTO system_settings(key,value) VALUES
                    ('STUDENT_CASES_READ_ENABLED','true'),
                    ('TRANSFER_CASES_WRITE_ENABLED','false'),
                    ('PROGRESSION_CASES_WRITE_ENABLED','false'),
                    ('EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED','false'),
                    ('CASE_COMPLETION_ENABLED','false');
                INSERT INTO legacy_probe(id,value) VALUES(1,'unchanged');
                """
            )
            connection.commit()

    def create_rebuild_backup_metadata(self, path):
        database_path = Path(path).resolve()
        self._backup_counter += 1
        backup_path = (
            self.root / f"rebuild-backup-{self._backup_counter:03d}.db"
        ).resolve()
        with closing(sqlite3.connect(database_path)) as source:
            with closing(sqlite3.connect(backup_path)) as target:
                source.backup(target)
        with closing(sqlite3.connect(backup_path)) as backup:
            quick_check = backup.execute("PRAGMA quick_check").fetchone()[0]
            integrity_check = backup.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
        manifest_path = backup_path.with_suffix(".manifest.json")
        manifest = {
            "database_path": str(database_path),
            "backup_path": str(backup_path),
            "backup_method": "sqlite_online_backup",
            "created_at": "2026-07-30T07:00:00+10:00",
            "source_database_sha256": sha256(database_path),
            "backup_sha256": sha256(backup_path),
            "quick_check": quick_check,
            "integrity_check": integrity_check,
        }
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True),
            encoding="utf-8",
        )
        return {
            "backup_path": str(backup_path),
            "manifest_path": str(manifest_path),
        }

    def apply(self, path=None, migrations_dir=None):
        database_path = Path(path or self.database_path)
        migration_directory = Path(
            migrations_dir or self.migration_dir_through_008()
        )
        migrations = migration_runner.discover_migrations(
            migration_directory
        )
        with closing(sqlite3.connect(database_path)) as connection:
            registry_exists = connection.execute(
                """SELECT count(*) FROM sqlite_master
                   WHERE type='table' AND name='schema_migrations'"""
            ).fetchone()[0]
            originally_applied = (
                [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                if registry_exists
                else []
            )

        applied_now = []
        execution_states = {}
        for migration in migrations:
            if migration.version in originally_applied or (
                migration.version in applied_now
            ):
                continue
            self._migration_stage_counter += 1
            stage = (
                self.root
                / f"migration-stage-{self._migration_stage_counter:03d}"
            )
            checks = stage / "checks"
            checks.mkdir(parents=True)
            for candidate in migrations:
                if candidate.version > migration.version:
                    continue
                shutil.copy2(
                    candidate.upgrade_path,
                    stage / candidate.upgrade_path.name,
                )
                shutil.copy2(
                    candidate.forward_check_path,
                    checks / candidate.forward_check_path.name,
                )
            backup_metadata = (
                self.create_rebuild_backup_metadata(database_path)
                if migration.mode
                == migration_runner.SQLITE_FK_OFF_REBUILD_MODE
                else None
            )
            result = migration_runner.apply_migrations(
                database_path,
                stage,
                applied_by="transfer-phase2-schema-test",
                source_commit="t1-test",
                formal_database_path=None,
                required_backup_metadata=backup_metadata,
            )
            applied_now.extend(result["applied"])
            execution_states.update(result.get("execution_states", {}))

        result = {
            "database": str(database_path.resolve()),
            "applied": applied_now,
            "already_applied": originally_applied,
            "pending": [],
        }
        if execution_states:
            result["execution_states"] = execution_states
        return result

    def apply_phase1_only(self, path=None):
        phase1_dir = self.root / "phase1_migrations"
        checks = phase1_dir / "checks"
        checks.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PHASE1_MIGRATION_PATH, phase1_dir / PHASE1_MIGRATION_PATH.name)
        shutil.copy2(
            PHASE1_FORWARD_CHECK_PATH,
            checks / PHASE1_FORWARD_CHECK_PATH.name,
        )
        return self.apply(path=path, migrations_dir=phase1_dir)

    def migration_dir_through_003(self):
        migration_dir = self.root / "migrations_through_003"
        checks = migration_dir / "checks"
        checks.mkdir(parents=True, exist_ok=True)
        for source in (
            PHASE1_MIGRATION_PATH,
            MIGRATION_PATH,
            TITLE_MIGRATION_PATH,
        ):
            shutil.copy2(source, migration_dir / source.name)
        for source in (
            PHASE1_FORWARD_CHECK_PATH,
            FORWARD_CHECK_PATH,
            TITLE_FORWARD_CHECK_PATH,
        ):
            shutil.copy2(source, checks / source.name)
        return migration_dir

    def migration_dir_through_004(self):
        migration_dir = self.root / "migrations_through_004"
        checks = migration_dir / "checks"
        checks.mkdir(parents=True, exist_ok=True)
        for source in (
            PHASE1_MIGRATION_PATH,
            MIGRATION_PATH,
            TITLE_MIGRATION_PATH,
            PROOF_MIGRATION_PATH,
        ):
            shutil.copy2(source, migration_dir / source.name)
        for source in (
            PHASE1_FORWARD_CHECK_PATH,
            FORWARD_CHECK_PATH,
            TITLE_FORWARD_CHECK_PATH,
            PROOF_FORWARD_CHECK_PATH,
        ):
            shutil.copy2(source, checks / source.name)
        return migration_dir

    def migration_dir_through_005(self):
        migration_dir = self.root / "migrations_through_005"
        checks = migration_dir / "checks"
        checks.mkdir(parents=True, exist_ok=True)
        for source in (
            PHASE1_MIGRATION_PATH,
            MIGRATION_PATH,
            TITLE_MIGRATION_PATH,
            PROOF_MIGRATION_PATH,
            ARRANGEMENTS_MIGRATION_PATH,
        ):
            shutil.copy2(source, migration_dir / source.name)
        for source in (
            PHASE1_FORWARD_CHECK_PATH,
            FORWARD_CHECK_PATH,
            TITLE_FORWARD_CHECK_PATH,
            PROOF_FORWARD_CHECK_PATH,
            ARRANGEMENTS_FORWARD_CHECK_PATH,
        ):
            shutil.copy2(source, checks / source.name)
        return migration_dir

    def migration_dir_through_006(self):
        migration_dir = self.root / "migrations_through_006"
        checks = migration_dir / "checks"
        checks.mkdir(parents=True, exist_ok=True)
        for source in (
            PHASE1_MIGRATION_PATH,
            MIGRATION_PATH,
            TITLE_MIGRATION_PATH,
            PROOF_MIGRATION_PATH,
            ARRANGEMENTS_MIGRATION_PATH,
            CONFIRMATION_REBUILD_MIGRATION_PATH,
        ):
            shutil.copy2(source, migration_dir / source.name)
        for source in (
            PHASE1_FORWARD_CHECK_PATH,
            FORWARD_CHECK_PATH,
            TITLE_FORWARD_CHECK_PATH,
            PROOF_FORWARD_CHECK_PATH,
            ARRANGEMENTS_FORWARD_CHECK_PATH,
            CONFIRMATION_REBUILD_FORWARD_CHECK_PATH,
        ):
            shutil.copy2(source, checks / source.name)
        return migration_dir

    def migration_dir_through_008(self):
        migration_dir = self.root / "migrations_through_008"
        checks = migration_dir / "checks"
        checks.mkdir(parents=True, exist_ok=True)
        for source in (
            PHASE1_MIGRATION_PATH,
            MIGRATION_PATH,
            TITLE_MIGRATION_PATH,
            PROOF_MIGRATION_PATH,
            ARRANGEMENTS_MIGRATION_PATH,
            CONFIRMATION_REBUILD_MIGRATION_PATH,
            COMPLETION_MIGRATION_PATH,
            PROGRESSION_MIGRATION_PATH,
        ):
            shutil.copy2(source, migration_dir / source.name)
        for source in (
            PHASE1_FORWARD_CHECK_PATH,
            FORWARD_CHECK_PATH,
            TITLE_FORWARD_CHECK_PATH,
            PROOF_FORWARD_CHECK_PATH,
            ARRANGEMENTS_FORWARD_CHECK_PATH,
            CONFIRMATION_REBUILD_FORWARD_CHECK_PATH,
            COMPLETION_FORWARD_CHECK_PATH,
            PROGRESSION_FORWARD_CHECK_PATH,
        ):
            shutil.copy2(source, checks / source.name)
        return migration_dir

    @staticmethod
    def table_names(connection):
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    @staticmethod
    def schema_snapshot(connection, excluded=()):
        placeholders = ",".join("?" for _ in excluded)
        predicate = (
            f"AND name NOT IN ({placeholders})" if placeholders else ""
        )
        return [
            tuple(row)
            for row in connection.execute(
                f"""SELECT type,name,tbl_name,sql
                    FROM sqlite_master
                    WHERE sql IS NOT NULL {predicate}
                    ORDER BY type,name""",
                tuple(excluded),
            )
        ]

    def insert_case(self, connection, student_id="student-1", sequence_no=1):
        draft_active_slot = (
            0
            if "student_lifecycle_idempotency_receipts"
            in self.table_names(connection)
            else 1
        )
        cursor = connection.execute(
            """INSERT INTO student_cases(
                   student_id,case_type,sequence_no,status,active_slot,
                   created_by,created_at,updated_by,updated_at
               ) VALUES(?, 'transfer', ?, 'draft', ?, 1, ?, 1, ?)""",
            (
                student_id,
                sequence_no,
                draft_active_slot,
                "2026-07-29T12:00:00+10:00",
                "2026-07-29T12:00:00+10:00",
            ),
        )
        return cursor.lastrowid

    def insert_transfer(self, connection, case_id, **overrides):
        values = {
            "case_id": case_id,
            "workflow_state": "draft",
            "current_school_name_snapshot": "Current School",
            "current_school_id": 10,
            "current_city_key": "melbourne",
            "current_school_sector": "public",
            "rule_status": "pending_confirmation",
            "rule_version": "phase2-v1",
            "created_at": "2026-07-29T12:00:00+10:00",
            "updated_at": "2026-07-29T12:00:00+10:00",
        }
        values.update(overrides)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO transfer_cases({columns}) VALUES({placeholders})",
            tuple(values.values()),
        )

    @staticmethod
    def insert_completion_confirmation(
        connection,
        *,
        case_id,
        action="complete",
        reverses_confirmation_id=None,
        idempotency_key="completion-key",
        effective_date="2026-08-01",
        reason=None,
    ):
        if action == "complete":
            state = {
                "workflow_state_before": "ready_to_complete",
                "workflow_state_after": "completed",
                "case_status_before": "in_progress",
                "case_status_after": "completed",
                "active_slot_before": 1,
                "active_slot_after": 0,
                "terminal_result_before": None,
                "terminal_result_after": "completed",
            }
        else:
            state = {
                "workflow_state_before": "completed",
                "workflow_state_after": "ready_to_complete",
                "case_status_before": "completed",
                "case_status_after": "in_progress",
                "active_slot_before": 0,
                "active_slot_after": 1,
                "terminal_result_before": "completed",
                "terminal_result_after": None,
            }
        values = {
            "case_id": case_id,
            "action": action,
            "reverses_confirmation_id": reverses_confirmation_id,
            "actor_user_id": 1,
            "confirmed_at": "2026-08-01T09:00:00+10:00",
            "reason": reason,
            "effective_date": effective_date,
            "idempotency_key": idempotency_key,
            **state,
            "current_enrolment_before_json": '{"id":7001}',
            "current_enrolment_after_json": '{"id":7001}',
            "upcoming_enrolment_before_json": '{"id":7002}',
            "upcoming_enrolment_after_json": '{"id":7002}',
            "readiness_snapshot_json": "[]",
            "school_state_before_json": "{}",
            "school_state_after_json": "{}",
            "metadata_json": "{}",
            "created_at": "2026-08-01T09:00:00+10:00",
        }
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        return connection.execute(
            f"""INSERT INTO case_completion_confirmations({columns})
                VALUES({placeholders})""",
            tuple(values.values()),
        ).lastrowid

    @staticmethod
    def insert_school_enrolment(
        connection,
        *,
        enrolment_id,
        student_id,
        role,
        source_case_id=None,
    ):
        return connection.execute(
            """INSERT INTO student_school_enrolments(
                   id,student_id,school_name_snapshot,enrolment_role,
                   source_case_id,status,created_by,created_at,
                   updated_by,updated_at
               ) VALUES(?,?,?,?,?,'active',1,?,1,?)""",
            (
                enrolment_id,
                student_id,
                f"{student_id} {role} school",
                role,
                source_case_id,
                "2026-08-01T08:00:00+10:00",
                "2026-08-01T08:00:00+10:00",
            ),
        ).lastrowid

    def insert_application(self, connection, case_id, **overrides):
        values = {
            "case_id": case_id,
            "school_id": 20,
            "school_name_snapshot": "Target School",
            "institution_group_id": 300,
            "city_key": "melbourne",
            "campus_key": "main",
            "school_sector": "private",
            "preference_order": 1,
            "application_status": "preparing",
            "is_final": 0,
            "created_by": 1,
            "created_at": "2026-07-29T12:00:00+10:00",
            "updated_at": "2026-07-29T12:00:00+10:00",
        }
        values.update(overrides)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        cursor = connection.execute(
            f"""INSERT INTO transfer_school_applications({columns})
                VALUES({placeholders})""",
            tuple(values.values()),
        )
        return cursor.lastrowid

    @staticmethod
    def insert_payment_requirement(
        connection,
        *,
        case_id,
        application_id,
        requirement_type="deposit",
    ):
        return connection.execute(
            """INSERT INTO case_payment_requirements(
                   case_id,transfer_application_id,requirement_type,status,
                   created_at,updated_at
               ) VALUES(?,?,?,'pending',?,?)""",
            (
                case_id,
                application_id,
                requirement_type,
                "2026-07-29T12:00:00+10:00",
                "2026-07-29T12:00:00+10:00",
            ),
        ).lastrowid

    @staticmethod
    def insert_payment_proof(
        connection,
        *,
        case_id,
        application_id,
        requirement_id,
        link_id,
        validity_status="current",
        superseded_by=None,
        proof_id=None,
    ):
        columns = [
            "case_id",
            "transfer_application_id",
            "payment_requirement_id",
            "case_record_link_id",
            "validity_status",
            "effective_from",
            "superseded_by",
            "created_by",
            "created_at",
        ]
        values = [
            case_id,
            application_id,
            requirement_id,
            link_id,
            validity_status,
            "2026-07-29T12:00:00+10:00",
            superseded_by,
            1,
            "2026-07-29T12:00:00+10:00",
        ]
        if proof_id is not None:
            columns.insert(0, "id")
            values.insert(0, proof_id)
        placeholders = ",".join("?" for _ in values)
        return connection.execute(
            f"""INSERT INTO case_payment_requirement_proofs(
                    {",".join(columns)}
                ) VALUES({placeholders})""",
            values,
        ).lastrowid

    def migrated_case_graph(self):
        self.apply()
        connection = self.connect(self.database_path)
        case_id = self.insert_case(connection)
        self.insert_transfer(connection, case_id)
        application_id = self.insert_application(connection, case_id)
        connection.commit()
        return connection, case_id, application_id

    @staticmethod
    def insert_guardianship_arrangement(connection, case_id, **overrides):
        values = {
            "case_id": case_id,
            "version_no": 1,
            "arrangement_mode": "pending_confirmation",
            "source_type": "staff_entered",
            "arrangement_status": "pending",
            "is_current": 1,
            "created_by": 1,
            "created_at": "2026-07-29T12:00:00+10:00",
            "updated_at": "2026-07-29T12:00:00+10:00",
        }
        values.update(overrides)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        return connection.execute(
            f"""INSERT INTO transfer_guardianship_arrangements({columns})
                VALUES({placeholders})""",
            tuple(values.values()),
        ).lastrowid

    @staticmethod
    def insert_accommodation_arrangement(connection, case_id, **overrides):
        values = {
            "case_id": case_id,
            "version_no": 1,
            "accommodation_type": "pending_confirmation",
            "source_type": "staff_entered",
            "arrangement_status": "pending",
            "is_current": 1,
            "created_by": 1,
            "created_at": "2026-07-29T12:00:00+10:00",
            "updated_at": "2026-07-29T12:00:00+10:00",
        }
        values.update(overrides)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        return connection.execute(
            f"""INSERT INTO transfer_accommodation_arrangements({columns})
                VALUES({placeholders})""",
            tuple(values.values()),
        ).lastrowid

    @staticmethod
    def insert_case_record_link(
        connection,
        case_id,
        record_type,
        record_id,
        *,
        validity_status="current",
    ):
        return connection.execute(
            """INSERT INTO case_record_links(
                   case_id,record_type,record_id,link_role,validity_status,
                   effective_from,created_by,created_at
               ) VALUES(?,?,?,?,?,?,1,?)""",
            (
                case_id,
                record_type,
                record_id,
                (
                    "carried_forward"
                    if validity_status == "carried_forward"
                    else "current"
                ),
                validity_status,
                "2026-07-29",
                "2026-07-29T12:00:00+10:00",
            ),
        ).lastrowid

    def insert_migration_case(self, connection, label):
        self._migration_case_counter += 1
        student_id = (
            f"migration-006-{self._migration_case_counter:03d}-{label}"
        )
        connection.execute(
            "INSERT INTO students(student_id) VALUES(?)",
            (student_id,),
        )
        case_id = self.insert_case(connection, student_id=student_id)
        self.insert_transfer(connection, case_id)
        return case_id, student_id

    @staticmethod
    def insert_arrangement_confirmation(
        connection,
        *,
        case_id,
        arrangement_type,
        arrangement_status,
        linked_record_id=None,
        document_id=None,
        guardianship_arrangement_id=None,
        accommodation_arrangement_id=None,
        requirement_reason=None,
        confirmed_by=None,
        confirmed_at=None,
        confirmation_id=None,
    ):
        values = {
            "case_id": case_id,
            "arrangement_type": arrangement_type,
            "arrangement_status": arrangement_status,
            "linked_record_id": linked_record_id,
            "document_id": document_id,
            "notes": f"{arrangement_type} {arrangement_status}",
            "confirmed_by": confirmed_by,
            "confirmed_at": confirmed_at,
            "created_at": "2026-07-30T07:10:00+10:00",
            "updated_at": "2026-07-30T07:11:00+10:00",
            "guardianship_arrangement_id": guardianship_arrangement_id,
            "accommodation_arrangement_id": accommodation_arrangement_id,
            "rule_version": "transfer-arrangements-v1",
            "requirement_reason": requirement_reason,
            "updated_by": 1,
        }
        if confirmation_id is not None:
            values = {"id": confirmation_id, **values}
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        return connection.execute(
            f"""INSERT INTO case_arrangement_confirmations({columns})
                VALUES({placeholders})""",
            tuple(values.values()),
        ).lastrowid

    def test_production_package_is_discoverable_with_stable_checksum(self):
        self.assertTrue(MIGRATION_PATH.is_file())
        self.assertTrue(FORWARD_CHECK_PATH.is_file())
        self.assertTrue(TITLE_MIGRATION_PATH.is_file())
        self.assertTrue(TITLE_FORWARD_CHECK_PATH.is_file())
        self.assertTrue(PROOF_MIGRATION_PATH.is_file())
        self.assertTrue(PROOF_FORWARD_CHECK_PATH.is_file())
        first = migration_runner.discover_migrations(MIGRATIONS_DIR)
        second = migration_runner.discover_migrations(MIGRATIONS_DIR)
        discovered = [(item.version, item.name) for item in first]
        self.assertEqual(
            discovered[:8],
            [
                ("20260728_001", "student_case_phase1"),
                ("20260729_002", "student_case_phase2_transfer"),
                ("20260729_003", "transfer_case_title"),
                ("20260729_004", "transfer_payment_requirement_proofs"),
                ("20260729_005", "transfer_arrangements"),
                (
                    "20260730_006",
                    "rebuild_case_arrangement_confirmations",
                ),
                (
                    "20260730_007",
                    "transfer_completion_contract",
                ),
                ("20260801_008", "progression_case_management"),
            ],
        )
        required_current_versions = {
            "20260811_017",
            "20260813_020",
            "20260813_022",
            "20260820_027",
            "20260822_028",
            "20260824_029",
            "20260824_030",
        }
        self.assertEqual(
            [
                item
                for item in discovered
                if item[0] in required_current_versions
            ],
            [
                ("20260811_017", "workflow_application_cycles"),
                ("20260813_020", "student_lifecycle_draft_semantics"),
                ("20260813_022", "school_enrolment_authority"),
                ("20260820_027", "transfer_contract_ownership"),
                ("20260822_028", "content_material_source_modified_at"),
                ("20260824_029", "contract_template_superseded_status"),
                ("20260824_030", "lifecycle_module_dispositions"),
            ],
        )
        self.assertEqual(
            first[4].checksum_sha256,
            second[4].checksum_sha256,
        )
        self.assertEqual(len(first[4].checksum_sha256), 64)

    def test_arrangements_migration_package_is_discoverable(self):
        self.assertTrue(ARRANGEMENTS_MIGRATION_PATH.is_file())
        self.assertTrue(ARRANGEMENTS_FORWARD_CHECK_PATH.is_file())
        migrations = migration_runner.discover_migrations(MIGRATIONS_DIR)
        self.assertEqual(
            (migrations[4].version, migrations[4].name),
            ("20260729_005", "transfer_arrangements"),
        )
        self.assertEqual(len(migrations[4].checksum_sha256), 64)

    def test_confirmation_rebuild_migration_package_is_special_and_discoverable(
        self,
    ):
        self.assertTrue(CONFIRMATION_REBUILD_MIGRATION_PATH.is_file())
        self.assertTrue(CONFIRMATION_REBUILD_FORWARD_CHECK_PATH.is_file())
        migration = next(
            item
            for item in migration_runner.discover_migrations(MIGRATIONS_DIR)
            if item.version == "20260730_006"
        )
        self.assertEqual(
            (migration.version, migration.name, migration.mode),
            (
                "20260730_006",
                "rebuild_case_arrangement_confirmations",
                migration_runner.SQLITE_FK_OFF_REBUILD_MODE,
            ),
        )
        self.assertEqual(len(migration.checksum_sha256), 64)

    def test_transfer_completion_checksum_change_is_rejected(self):
        self.apply()
        modified = self.root / "completion-checksum-change"
        checks = modified / "checks"
        checks.mkdir(parents=True)
        for migration in migration_runner.discover_migrations(MIGRATIONS_DIR):
            shutil.copy2(
                migration.upgrade_path,
                modified / migration.upgrade_path.name,
            )
            shutil.copy2(
                migration.forward_check_path,
                checks / migration.forward_check_path.name,
            )
        with (modified / COMPLETION_MIGRATION_PATH.name).open(
            "a",
            encoding="utf-8",
        ) as handle:
            handle.write("\n-- checksum-change-probe\n")

        with self.assertRaises(migration_runner.MigrationChecksumError):
            migration_runner.verify_migrations(self.database_path, modified)

    def test_transfer_completion_migration_package_is_special_and_discoverable(
        self,
    ):
        self.assertTrue(COMPLETION_MIGRATION_PATH.is_file())
        self.assertTrue(COMPLETION_FORWARD_CHECK_PATH.is_file())

        migration = next(
            item for item in migration_runner.discover_migrations(MIGRATIONS_DIR)
            if item.version == "20260730_007"
        )

        self.assertEqual(
            (migration.version, migration.name, migration.mode),
            (
                "20260730_007",
                "transfer_completion_contract",
                migration_runner.SQLITE_FK_OFF_REBUILD_MODE,
            ),
        )
        self.assertEqual(len(migration.checksum_sha256), 64)
        repeated = next(
            item for item in migration_runner.discover_migrations(MIGRATIONS_DIR)
            if item.version == "20260730_007"
        )
        self.assertEqual(
            repeated.checksum_sha256,
            migration.checksum_sha256,
        )

    def test_transfer_completion_rebuilds_empty_legacy_completion_contract(
        self,
    ):
        through_006 = self.migration_dir_through_006()
        self.apply(migrations_dir=through_006)
        with closing(self.connect(self.database_path)) as connection:
            case_id = self.insert_case(connection)
            self.insert_transfer(connection, case_id)
            connection.commit()

        result = self.apply()

        self.assertEqual(
            result["applied"], ["20260730_007", "20260801_008"]
        )
        with closing(self.connect(self.database_path)) as connection:
            transfer = connection.execute(
                """SELECT current_school_enrolment_id,
                          upcoming_school_enrolment_id,relationship_status
                   FROM transfer_cases WHERE case_id=?""",
                (case_id,),
            ).fetchone()
            self.assertEqual(tuple(transfer), (None, None, "needs_repair"))
            completion_columns = {
                row["name"]: row
                for row in connection.execute(
                    "PRAGMA table_info('case_completion_confirmations')"
                )
            }
            self.assertEqual(
                set(completion_columns),
                {
                    "id",
                    "case_id",
                    "action",
                    "reverses_confirmation_id",
                    "actor_user_id",
                    "confirmed_at",
                    "reason",
                    "effective_date",
                    "idempotency_key",
                    "workflow_state_before",
                    "workflow_state_after",
                    "case_status_before",
                    "case_status_after",
                    "active_slot_before",
                    "active_slot_after",
                    "terminal_result_before",
                    "terminal_result_after",
                    "current_enrolment_before_json",
                    "current_enrolment_after_json",
                    "upcoming_enrolment_before_json",
                    "upcoming_enrolment_after_json",
                    "readiness_snapshot_json",
                    "school_state_before_json",
                    "school_state_after_json",
                    "metadata_json",
                    "created_at",
                },
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM case_completion_confirmations"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )

    def test_transfer_completion_relationship_contract_is_enforced(self):
        self.apply()
        with closing(self.connect(self.database_path)) as connection:
            case_id = self.insert_case(connection)
            self.insert_transfer(connection, case_id)
            other_case_id = self.insert_case(
                connection,
                student_id="student-2",
                sequence_no=2,
            )
            self.insert_transfer(connection, other_case_id)
            self.insert_school_enrolment(
                connection,
                enrolment_id=7001,
                student_id="student-1",
                role="current",
            )
            self.insert_school_enrolment(
                connection,
                enrolment_id=7002,
                student_id="student-1",
                role="upcoming",
                source_case_id=case_id,
            )
            self.insert_school_enrolment(
                connection,
                enrolment_id=7003,
                student_id="student-2",
                role="current",
            )
            connection.execute(
                """UPDATE transfer_cases
                   SET current_school_enrolment_id=7001,
                       relationship_status='current_ready'
                   WHERE case_id=?""",
                (case_id,),
            )
            connection.execute(
                """UPDATE transfer_cases
                   SET upcoming_school_enrolment_id=7002,
                       relationship_status='ready'
                   WHERE case_id=?""",
                (case_id,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE student_school_enrolments
                       SET status='ended',enrolment_role='historical'
                       WHERE id=7001"""
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE student_school_enrolments
                       SET source_case_id=NULL WHERE id=7002"""
                )
            for status in ("transitioned", "reverted", "current_ready"):
                connection.execute(
                    """UPDATE transfer_cases
                       SET upcoming_school_enrolment_id=NULL,
                           relationship_status=?
                       WHERE case_id=?""",
                    (status, case_id),
                )
            connection.execute(
                """UPDATE transfer_cases
                   SET current_school_enrolment_id=NULL,
                       relationship_status='needs_repair'
                   WHERE case_id=?""",
                (case_id,),
            )

            invalid_updates = (
                (
                    """UPDATE transfer_cases
                       SET relationship_status='unknown'
                       WHERE case_id=?""",
                    (case_id,),
                ),
                (
                    """UPDATE transfer_cases
                       SET current_school_enrolment_id=7001,
                           upcoming_school_enrolment_id=7001,
                           relationship_status='ready'
                       WHERE case_id=?""",
                    (case_id,),
                ),
                (
                    """UPDATE transfer_cases
                       SET current_school_enrolment_id=7003,
                           upcoming_school_enrolment_id=NULL,
                           relationship_status='current_ready'
                       WHERE case_id=?""",
                    (case_id,),
                ),
                (
                    """UPDATE transfer_cases
                       SET current_school_enrolment_id=NULL,
                           upcoming_school_enrolment_id=NULL,
                           relationship_status='ready'
                       WHERE case_id=?""",
                    (case_id,),
                ),
            )
            for sql, parameters in invalid_updates:
                with self.subTest(sql=sql):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(sql, parameters)

    def test_transfer_completion_confirmation_contract_is_enforced(self):
        self.apply()
        with closing(self.connect(self.database_path)) as connection:
            case_id = self.insert_case(connection)
            self.insert_transfer(connection, case_id)
            other_case_id = self.insert_case(
                connection,
                student_id="student-2",
                sequence_no=2,
            )
            self.insert_transfer(connection, other_case_id)
            complete_id = self.insert_completion_confirmation(
                connection,
                case_id=case_id,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_completion_confirmation(
                    connection,
                    case_id=case_id,
                    idempotency_key="completion-key",
                )
            undo_id = self.insert_completion_confirmation(
                connection,
                case_id=case_id,
                action="undo_completion",
                reverses_confirmation_id=complete_id,
                idempotency_key="undo-key",
                reason="Correction required",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_completion_confirmation(
                    connection,
                    case_id=case_id,
                    action="undo_completion",
                    reverses_confirmation_id=complete_id,
                    idempotency_key="second-undo-key",
                    reason="Duplicate undo",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_completion_confirmation(
                    connection,
                    case_id=case_id,
                    action="undo_completion",
                    reverses_confirmation_id=undo_id,
                    idempotency_key="undo-of-undo-key",
                    reason="Invalid reverse",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_completion_confirmation(
                    connection,
                    case_id=other_case_id,
                    action="undo_completion",
                    reverses_confirmation_id=complete_id,
                    idempotency_key="cross-case-key",
                    reason="Invalid Case",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_completion_confirmation(
                    connection,
                    case_id=other_case_id,
                    idempotency_key="missing-date-key",
                    effective_date=None,
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_completion_confirmation(
                    connection,
                    case_id=other_case_id,
                    idempotency_key="invalid-date-key",
                    effective_date="2026-02-30",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_completion_confirmation(
                    connection,
                    case_id=other_case_id,
                    action="undo_completion",
                    idempotency_key="missing-reverse-key",
                    reason="Missing reverse",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE case_completion_confirmations
                       SET reason='changed' WHERE id=?""",
                    (complete_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM case_completion_confirmations WHERE id=?",
                    (complete_id,),
                )
            connection.commit()

        verified = migration_runner.verify_migrations(
            self.database_path,
            self.migration_dir_through_008(),
        )
        self.assertEqual(verified["verified"][-1], "20260801_008")

    def test_transfer_completion_rebuild_aborts_on_legacy_confirmation(self):
        through_006 = self.migration_dir_through_006()
        self.apply(migrations_dir=through_006)
        with closing(self.connect(self.database_path)) as connection:
            case_id = self.insert_case(connection)
            self.insert_transfer(connection, case_id)
            connection.execute(
                """INSERT INTO case_completion_confirmations(
                       case_id,confirmation_type,reason,actor_user_id,
                       confirmed_at,previous_status,new_status,
                       metadata_json,created_at
                   ) VALUES(?,'complete',NULL,1,?,'in_progress','completed',
                            '{}',?)""",
                (
                    case_id,
                    "2026-08-01T09:00:00+10:00",
                    "2026-08-01T09:00:00+10:00",
                ),
            )
            connection.commit()

        with self.assertRaises(migration_runner.MigrationExecutionError):
            self.apply()

        with closing(self.connect(self.database_path)) as connection:
            self.assertNotIn(
                "current_school_enrolment_id",
                {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info('transfer_cases')"
                    )
                },
            )
            self.assertEqual(
                [
                    tuple(row)
                    for row in connection.execute(
                        """SELECT confirmation_type
                           FROM case_completion_confirmations"""
                    )
                ],
                [("complete",)],
            )
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM schema_migrations
                       WHERE version='20260730_007'"""
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_keys").fetchone()[0],
                1,
            )

    def test_confirmation_rebuild_requires_exact_declaration_and_backup(self):
        migration_runner.apply_migrations(
            self.database_path,
            self.migration_dir_through_005(),
            applied_by="migration-006-gate-test",
            source_commit="migration-006-gate-test",
            formal_database_path=None,
        )
        with self.assertRaises(
            migration_runner.MigrationRebuildBackupRequiredError
        ):
            migration_runner.apply_migrations(
                self.database_path,
                MIGRATIONS_DIR,
                applied_by="migration-006-gate-test",
                source_commit="migration-006-gate-test",
                formal_database_path=None,
            )
        with self.assertRaises(
            migration_runner.MigrationRebuildBackupRequiredError
        ):
            migration_runner.apply_migrations(
                self.database_path,
                MIGRATIONS_DIR,
                applied_by="migration-006-gate-test",
                source_commit="migration-006-gate-test",
                formal_database_path=None,
                required_backup_metadata={},
            )

        bad_declaration_dir = self.root / "migration-006-bad-declaration"
        bad_checks = bad_declaration_dir / "checks"
        bad_checks.mkdir(parents=True)
        shutil.copy2(
            CONFIRMATION_REBUILD_MIGRATION_PATH,
            bad_declaration_dir / CONFIRMATION_REBUILD_MIGRATION_PATH.name,
        )
        shutil.copy2(
            CONFIRMATION_REBUILD_FORWARD_CHECK_PATH,
            bad_checks / CONFIRMATION_REBUILD_FORWARD_CHECK_PATH.name,
        )
        upgrade_path = (
            bad_declaration_dir / CONFIRMATION_REBUILD_MIGRATION_PATH.name
        )
        sql = upgrade_path.read_text(encoding="utf-8")
        upgrade_path.write_text(
            sql.replace(
                "-- migration_mode: sqlite_fk_off_rebuild",
                "-- migration_mode: wrong_mode",
                1,
            ),
            encoding="utf-8",
        )
        with self.assertRaises(
            migration_runner.MigrationSpecialModeMismatchError
        ):
            migration_runner.discover_migrations(bad_declaration_dir)

    def test_confirmation_rebuild_full_chain_reapply_verify_and_forward_check(
        self,
    ):
        empty = self.root / "migration-006-empty.db"
        self._create_legacy_fixture(empty)

        first = self.apply(path=empty)
        second = self.apply(path=empty)
        verified = migration_runner.verify_migrations(
            empty, self.migration_dir_through_008()
        )

        expected = [
            "20260728_001",
            "20260729_002",
            "20260729_003",
            "20260729_004",
            "20260729_005",
            "20260730_006",
            "20260730_007",
            "20260801_008",
        ]
        self.assertEqual(first["applied"], expected)
        self.assertEqual(second["applied"], [])
        self.assertEqual(second["already_applied"], expected)
        self.assertEqual(verified["verified"], expected)
        with closing(self.connect(empty)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )
            self.assertEqual(
                connection.execute("PRAGMA quick_check").fetchone()[0],
                "ok",
            )

    def test_confirmation_rebuild_preserves_historical_rows_and_evidence(self):
        migration_runner.apply_migrations(
            self.database_path,
            self.migration_dir_through_005(),
            applied_by="migration-006-history-test",
            source_commit="migration-006-history-test",
            formal_database_path=None,
        )
        with closing(self.connect(self.database_path)) as connection:
            confirmation_ids = []
            evidence_ids = []

            def new_case(label):
                return self.insert_migration_case(connection, label)

            case_id, _student_id = new_case("visa-pending")
            confirmation_ids.append(
                self.insert_arrangement_confirmation(
                    connection,
                    case_id=case_id,
                    arrangement_type="visa",
                    arrangement_status="pending",
                    confirmation_id=101,
                )
            )

            case_id, student_id = new_case("visa-carried")
            connection.execute(
                "INSERT INTO visas(id,student_id) VALUES(3101,?)",
                (student_id,),
            )
            visa_carried_link = self.insert_case_record_link(
                connection,
                case_id,
                "visa",
                3101,
                validity_status="carried_forward",
            )
            visa_carried_confirmation = self.insert_arrangement_confirmation(
                connection,
                case_id=case_id,
                arrangement_type="visa",
                arrangement_status="carried_forward",
                linked_record_id=visa_carried_link,
                confirmation_id=102,
            )
            confirmation_ids.append(visa_carried_confirmation)
            evidence_ids.append(
                connection.execute(
                    """INSERT INTO case_arrangement_evidence_links(
                           id,case_id,arrangement_type,
                           arrangement_confirmation_id,case_record_link_id,
                           evidence_role,validity_status,effective_from,
                           created_by,created_at
                       ) VALUES(
                           201,?,'visa',?,?,'primary','current',?,1,?
                       )""",
                    (
                        case_id,
                        visa_carried_confirmation,
                        visa_carried_link,
                        "2026-07-30",
                        "2026-07-30T07:12:00+10:00",
                    ),
                ).lastrowid
            )

            case_id, student_id = new_case("visa-confirmed")
            connection.execute(
                "INSERT INTO visas(id,student_id) VALUES(3102,?)",
                (student_id,),
            )
            visa_confirmed_link = self.insert_case_record_link(
                connection, case_id, "visa", 3102
            )
            confirmation_ids.append(
                self.insert_arrangement_confirmation(
                    connection,
                    case_id=case_id,
                    arrangement_type="visa",
                    arrangement_status="confirmed",
                    linked_record_id=visa_confirmed_link,
                    confirmed_by=1,
                    confirmed_at="2026-07-30T07:09:00+10:00",
                    confirmation_id=103,
                )
            )

            case_id, _student_id = new_case("visa-not-applicable")
            confirmation_ids.append(
                self.insert_arrangement_confirmation(
                    connection,
                    case_id=case_id,
                    arrangement_type="visa",
                    arrangement_status="not_applicable",
                    requirement_reason="No visa action required",
                    confirmation_id=104,
                )
            )

            case_id, _student_id = new_case("oshc-pending")
            confirmation_ids.append(
                self.insert_arrangement_confirmation(
                    connection,
                    case_id=case_id,
                    arrangement_type="oshc",
                    arrangement_status="pending",
                    confirmation_id=105,
                )
            )

            case_id, student_id = new_case("oshc-updated")
            connection.execute(
                """INSERT INTO coe_oshc(id,student_id,document_kind)
                   VALUES(3201,?,'OSHC')""",
                (student_id,),
            )
            oshc_updated_link = self.insert_case_record_link(
                connection, case_id, "oshc", 3201
            )
            oshc_updated_confirmation = self.insert_arrangement_confirmation(
                connection,
                case_id=case_id,
                arrangement_type="oshc",
                arrangement_status="updated",
                linked_record_id=oshc_updated_link,
                confirmation_id=106,
            )
            confirmation_ids.append(oshc_updated_confirmation)
            evidence_ids.append(
                connection.execute(
                    """INSERT INTO case_arrangement_evidence_links(
                           id,case_id,arrangement_type,
                           arrangement_confirmation_id,case_record_link_id,
                           evidence_role,validity_status,effective_from,
                           created_by,created_at
                       ) VALUES(
                           202,?,'oshc',?,?,'primary','current',?,1,?
                       )""",
                    (
                        case_id,
                        oshc_updated_confirmation,
                        oshc_updated_link,
                        "2026-07-30",
                        "2026-07-30T07:13:00+10:00",
                    ),
                ).lastrowid
            )

            case_id, student_id = new_case("oshc-confirmed")
            connection.execute(
                """INSERT INTO coe_oshc(id,student_id,document_kind)
                   VALUES(3202,?,'OSHC')""",
                (student_id,),
            )
            oshc_confirmed_link = self.insert_case_record_link(
                connection, case_id, "oshc", 3202
            )
            confirmation_ids.append(
                self.insert_arrangement_confirmation(
                    connection,
                    case_id=case_id,
                    arrangement_type="oshc",
                    arrangement_status="confirmed",
                    linked_record_id=oshc_confirmed_link,
                    confirmed_by=1,
                    confirmed_at="2026-07-30T07:09:00+10:00",
                    confirmation_id=107,
                )
            )

            case_id, student_id = new_case("guardianship-pending")
            guardian_confirmation = self.insert_arrangement_confirmation(
                connection,
                case_id=case_id,
                arrangement_type="guardianship",
                arrangement_status="pending",
                confirmation_id=108,
            )
            confirmation_ids.append(guardian_confirmation)
            guardian_id = self.insert_guardianship_arrangement(
                connection, case_id
            )
            connection.execute(
                "INSERT INTO student_documents(id,student_id) VALUES(3301,?)",
                (student_id,),
            )
            evidence_ids.append(
                connection.execute(
                    """INSERT INTO case_arrangement_evidence_links(
                           id,case_id,arrangement_type,
                           guardianship_arrangement_id,document_id,
                           evidence_role,validity_status,effective_from,
                           created_by,created_at
                       ) VALUES(
                           203,?,'guardianship',?,3301,'authority','current',
                           ?,1,?
                       )""",
                    (
                        case_id,
                        guardian_id,
                        "2026-07-30",
                        "2026-07-30T07:14:00+10:00",
                    ),
                ).lastrowid
            )

            case_id, student_id = new_case("accommodation-pending")
            confirmation_ids.append(
                self.insert_arrangement_confirmation(
                    connection,
                    case_id=case_id,
                    arrangement_type="accommodation",
                    arrangement_status="pending",
                    confirmation_id=109,
                )
            )
            accommodation_id = self.insert_accommodation_arrangement(
                connection, case_id
            )
            connection.execute(
                "INSERT INTO student_documents(id,student_id) VALUES(3302,?)",
                (student_id,),
            )
            evidence_ids.append(
                connection.execute(
                    """INSERT INTO case_arrangement_evidence_links(
                           id,case_id,arrangement_type,
                           accommodation_arrangement_id,document_id,
                           evidence_role,validity_status,effective_from,
                           created_by,created_at
                       ) VALUES(
                           204,?,'accommodation',?,3302,'address','current',
                           ?,1,?
                       )""",
                    (
                        case_id,
                        accommodation_id,
                        "2026-07-30",
                        "2026-07-30T07:15:00+10:00",
                    ),
                ).lastrowid
            )

            case_id, student_id = new_case("transcript")
            connection.execute(
                "INSERT INTO student_documents(id,student_id) VALUES(3303,?)",
                (student_id,),
            )
            confirmation_ids.append(
                self.insert_arrangement_confirmation(
                    connection,
                    case_id=case_id,
                    arrangement_type="transcript_or_school_report",
                    arrangement_status="updated",
                    document_id=3303,
                    confirmation_id=110,
                )
            )
            connection.commit()
            confirmations_before = [
                tuple(row)
                for row in connection.execute(
                    """SELECT * FROM case_arrangement_confirmations
                       ORDER BY id"""
                )
            ]
            evidence_before = [
                tuple(row)
                for row in connection.execute(
                    """SELECT * FROM case_arrangement_evidence_links
                       ORDER BY id"""
                )
            ]

        result = self.apply(
            migrations_dir=self.migration_dir_through_006()
        )

        self.assertEqual(result["applied"], ["20260730_006"])
        with closing(self.connect(self.database_path)) as connection:
            confirmations_after = [
                tuple(row)
                for row in connection.execute(
                    """SELECT * FROM case_arrangement_confirmations
                       ORDER BY id"""
                )
            ]
            evidence_after = [
                tuple(row)
                for row in connection.execute(
                    """SELECT * FROM case_arrangement_evidence_links
                       ORDER BY id"""
                )
            ]
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )
            self.assertEqual(
                connection.execute("PRAGMA quick_check").fetchone()[0],
                "ok",
            )
        self.assertEqual(confirmations_after, confirmations_before)
        self.assertEqual(evidence_after, evidence_before)
        self.assertEqual(
            [row[0] for row in confirmations_after],
            confirmation_ids,
        )
        self.assertEqual([row[0] for row in evidence_after], evidence_ids)

    def test_confirmation_rebuild_accepts_all_supported_status_combinations(
        self,
    ):
        self.apply()
        inserted = []
        with closing(self.connect(self.database_path)) as connection:
            for arrangement_type in ("visa", "oshc"):
                for status in (
                    "pending",
                    "carried_forward",
                    "updated",
                    "confirmed",
                    "not_applicable",
                ):
                    case_id, student_id = self.insert_migration_case(
                        connection, f"{arrangement_type}-{status}"
                    )
                    link_id = None
                    if status in ("carried_forward", "updated", "confirmed"):
                        record_id = 4000 + self._migration_case_counter
                        if arrangement_type == "visa":
                            connection.execute(
                                "INSERT INTO visas(id,student_id) VALUES(?,?)",
                                (record_id, student_id),
                            )
                        else:
                            connection.execute(
                                """INSERT INTO coe_oshc(
                                       id,student_id,document_kind
                                   ) VALUES(?,?,'OSHC')""",
                                (record_id, student_id),
                            )
                        link_id = self.insert_case_record_link(
                            connection,
                            case_id,
                            arrangement_type,
                            record_id,
                            validity_status=(
                                "carried_forward"
                                if status == "carried_forward"
                                else "current"
                            ),
                        )
                    inserted.append(
                        self.insert_arrangement_confirmation(
                            connection,
                            case_id=case_id,
                            arrangement_type=arrangement_type,
                            arrangement_status=status,
                            linked_record_id=link_id,
                            requirement_reason=(
                                "Not required"
                                if status == "not_applicable"
                                else None
                            ),
                            confirmed_by=1 if status == "confirmed" else None,
                            confirmed_at=(
                                "2026-07-30T07:20:00+10:00"
                                if status == "confirmed"
                                else None
                            ),
                        )
                    )

            for arrangement_type in ("guardianship", "accommodation"):
                for status in (
                    "pending",
                    "carried_forward",
                    "updated",
                    "confirmed",
                    "not_applicable",
                ):
                    case_id, _student_id = self.insert_migration_case(
                        connection, f"{arrangement_type}-{status}"
                    )
                    guardianship_id = None
                    accommodation_id = None
                    if status in ("carried_forward", "updated", "confirmed"):
                        if arrangement_type == "guardianship":
                            guardianship_id = (
                                self.insert_guardianship_arrangement(
                                    connection,
                                    case_id,
                                    arrangement_mode=(
                                        "parent_guardian"
                                        if status == "confirmed"
                                        else "pending_confirmation"
                                    ),
                                    guardian_person_name=(
                                        "Parent"
                                        if status == "confirmed"
                                        else None
                                    ),
                                    source_type=(
                                        "carried_forward"
                                        if status == "carried_forward"
                                        else "updated_arrangement"
                                    ),
                                    arrangement_status=status,
                                )
                            )
                        else:
                            accommodation_id = (
                                self.insert_accommodation_arrangement(
                                    connection,
                                    case_id,
                                    accommodation_type=(
                                        "homestay"
                                        if status == "confirmed"
                                        else "pending_confirmation"
                                    ),
                                    provider_name=(
                                        "Provider"
                                        if status == "confirmed"
                                        else None
                                    ),
                                    source_type=(
                                        "carried_forward"
                                        if status == "carried_forward"
                                        else "updated_arrangement"
                                    ),
                                    arrangement_status=status,
                                )
                            )
                    inserted.append(
                        self.insert_arrangement_confirmation(
                            connection,
                            case_id=case_id,
                            arrangement_type=arrangement_type,
                            arrangement_status=status,
                            guardianship_arrangement_id=guardianship_id,
                            accommodation_arrangement_id=accommodation_id,
                            requirement_reason=(
                                "Not required"
                                if status == "not_applicable"
                                else None
                            ),
                            confirmed_by=1 if status == "confirmed" else None,
                            confirmed_at=(
                                "2026-07-30T07:20:00+10:00"
                                if status == "confirmed"
                                else None
                            ),
                        )
                    )

            case_id, student_id = self.insert_migration_case(
                connection, "transcript"
            )
            connection.execute(
                "INSERT INTO student_documents(id,student_id) VALUES(4999,?)",
                (student_id,),
            )
            inserted.append(
                self.insert_arrangement_confirmation(
                    connection,
                    case_id=case_id,
                    arrangement_type="transcript_or_school_report",
                    arrangement_status="updated",
                    document_id=4999,
                )
            )
            connection.commit()

        self.assertEqual(len(inserted), 21)

    def test_confirmation_rebuild_rejects_invalid_targets_and_state_shapes(self):
        self.apply()
        with closing(self.connect(self.database_path)) as connection:
            case_id, student_id = self.insert_migration_case(
                connection, "invalid-primary"
            )
            other_case_id, other_student_id = self.insert_migration_case(
                connection, "invalid-other"
            )
            connection.execute(
                "INSERT INTO visas(id,student_id) VALUES(5101,?)",
                (student_id,),
            )
            connection.execute(
                "INSERT INTO visas(id,student_id) VALUES(5102,?)",
                (other_student_id,),
            )
            connection.execute(
                """INSERT INTO coe_oshc(id,student_id,document_kind)
                   VALUES(5201,?,'OSHC')""",
                (student_id,),
            )
            visa_link = self.insert_case_record_link(
                connection, case_id, "visa", 5101
            )
            other_visa_link = self.insert_case_record_link(
                connection, other_case_id, "visa", 5102
            )
            oshc_link = self.insert_case_record_link(
                connection, case_id, "oshc", 5201
            )
            guardian_id = self.insert_guardianship_arrangement(
                connection,
                case_id,
                source_type="updated_arrangement",
                arrangement_status="updated",
            )
            other_guardian_id = self.insert_guardianship_arrangement(
                connection,
                other_case_id,
                source_type="updated_arrangement",
                arrangement_status="updated",
            )
            accommodation_id = self.insert_accommodation_arrangement(
                connection,
                case_id,
                source_type="updated_arrangement",
                arrangement_status="updated",
            )
            other_accommodation_id = self.insert_accommodation_arrangement(
                connection,
                other_case_id,
                source_type="updated_arrangement",
                arrangement_status="updated",
            )
            connection.execute(
                "INSERT INTO student_documents(id,student_id) VALUES(5301,?)",
                (student_id,),
            )
            connection.execute(
                "INSERT INTO student_documents(id,student_id) VALUES(5302,?)",
                (other_student_id,),
            )
            connection.commit()

            invalid = (
                (
                    "visa carried forward without link",
                    {
                        "arrangement_type": "visa",
                        "arrangement_status": "carried_forward",
                    },
                ),
                (
                    "visa uses oshc link",
                    {
                        "arrangement_type": "visa",
                        "arrangement_status": "updated",
                        "linked_record_id": oshc_link,
                    },
                ),
                (
                    "visa uses guardianship id",
                    {
                        "arrangement_type": "visa",
                        "arrangement_status": "updated",
                        "linked_record_id": visa_link,
                        "guardianship_arrangement_id": guardian_id,
                    },
                ),
                (
                    "oshc uses visa link",
                    {
                        "arrangement_type": "oshc",
                        "arrangement_status": "updated",
                        "linked_record_id": visa_link,
                    },
                ),
                (
                    "guardianship carried forward without target",
                    {
                        "arrangement_type": "guardianship",
                        "arrangement_status": "carried_forward",
                    },
                ),
                (
                    "guardianship document does not replace target",
                    {
                        "arrangement_type": "guardianship",
                        "arrangement_status": "updated",
                        "document_id": 5301,
                    },
                ),
                (
                    "guardianship uses accommodation target",
                    {
                        "arrangement_type": "guardianship",
                        "arrangement_status": "updated",
                        "accommodation_arrangement_id": accommodation_id,
                    },
                ),
                (
                    "guardianship target is cross case",
                    {
                        "arrangement_type": "guardianship",
                        "arrangement_status": "updated",
                        "guardianship_arrangement_id": other_guardian_id,
                    },
                ),
                (
                    "accommodation updated without target",
                    {
                        "arrangement_type": "accommodation",
                        "arrangement_status": "updated",
                    },
                ),
                (
                    "accommodation uses guardianship target",
                    {
                        "arrangement_type": "accommodation",
                        "arrangement_status": "updated",
                        "guardianship_arrangement_id": guardian_id,
                    },
                ),
                (
                    "accommodation target is cross case",
                    {
                        "arrangement_type": "accommodation",
                        "arrangement_status": "updated",
                        "accommodation_arrangement_id": other_accommodation_id,
                    },
                ),
                (
                    "confirmed missing confirmed by",
                    {
                        "arrangement_type": "visa",
                        "arrangement_status": "confirmed",
                        "linked_record_id": visa_link,
                        "confirmed_at": "2026-07-30T07:30:00+10:00",
                    },
                ),
                (
                    "confirmed missing confirmed at",
                    {
                        "arrangement_type": "visa",
                        "arrangement_status": "confirmed",
                        "linked_record_id": visa_link,
                        "confirmed_by": 1,
                    },
                ),
                (
                    "not applicable missing reason",
                    {
                        "arrangement_type": "visa",
                        "arrangement_status": "not_applicable",
                    },
                ),
                (
                    "both structured targets",
                    {
                        "arrangement_type": "guardianship",
                        "arrangement_status": "updated",
                        "guardianship_arrangement_id": guardian_id,
                        "accommodation_arrangement_id": accommodation_id,
                    },
                ),
                (
                    "document belongs to another student",
                    {
                        "arrangement_type": "transcript_or_school_report",
                        "arrangement_status": "updated",
                        "document_id": 5302,
                    },
                ),
                (
                    "record link belongs to another case",
                    {
                        "arrangement_type": "visa",
                        "arrangement_status": "updated",
                        "linked_record_id": other_visa_link,
                    },
                ),
            )
            for label, values in invalid:
                with self.subTest(label=label):
                    connection.execute("SAVEPOINT invalid_confirmation")
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.insert_arrangement_confirmation(
                            connection,
                            case_id=case_id,
                            **values,
                        )
                    connection.execute(
                        "ROLLBACK TO SAVEPOINT invalid_confirmation"
                    )
                    connection.execute(
                        "RELEASE SAVEPOINT invalid_confirmation"
                    )

            identity_id = self.insert_arrangement_confirmation(
                connection,
                case_id=case_id,
                arrangement_type="visa",
                arrangement_status="pending",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE case_arrangement_confirmations
                       SET case_id=? WHERE id=?""",
                    (other_case_id, identity_id),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE case_arrangement_confirmations
                       SET arrangement_type='oshc' WHERE id=?""",
                    (identity_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM case_arrangement_confirmations WHERE id=?",
                    (identity_id,),
                )

    def test_empty_database_applies_all_migrations_and_creates_only_18_case_tables(
        self,
    ):
        empty = self.root / "empty.db"
        self._create_legacy_fixture(empty)
        result = self.apply(path=empty)
        self.assertEqual(
            result["applied"],
            [
                "20260728_001",
                "20260729_002",
                "20260729_003",
                "20260729_004",
                "20260729_005",
                "20260730_006",
                "20260730_007",
                "20260801_008",
            ],
        )
        with closing(self.connect(empty)) as connection:
            created = self.table_names(connection) - {"schema_migrations"}
            self.assertTrue(
                (PHASE1_TABLES | PHASE2_TABLES).issubset(created)
            )
            for table in PHASE1_TABLES | PHASE2_TABLES:
                self.assertEqual(
                    connection.execute(
                        f'SELECT count(*) FROM "{table}"'
                    ).fetchone()[0],
                    0,
                )

    def test_phase1_database_applies_only_phase2_and_repeat_is_verified_noop(self):
        self.apply_phase1_only()
        first = self.apply()
        second = self.apply()
        self.assertEqual(
            first["applied"],
            [
                "20260729_002",
                "20260729_003",
                "20260729_004",
                "20260729_005",
                "20260730_006",
                "20260730_007",
                "20260801_008",
            ],
        )
        self.assertEqual(second["applied"], [])
        self.assertEqual(
            second["already_applied"],
            [
                "20260728_001",
                "20260729_002",
                "20260729_003",
                "20260729_004",
                "20260729_005",
                "20260730_006",
                "20260730_007",
                "20260801_008",
            ],
        )
        verified = migration_runner.verify_migrations(
            self.database_path, self.migration_dir_through_008()
        )
        self.assertEqual(
            verified["verified"],
            [
                "20260728_001",
                "20260729_002",
                "20260729_003",
                "20260729_004",
                "20260729_005",
                "20260730_006",
                "20260730_007",
                "20260801_008",
            ],
        )

    def test_verify_uses_latest_cumulative_forward_contract(self):
        migrations = self.root / "cumulative_migrations"
        checks = migrations / "checks"
        checks.mkdir(parents=True)
        (migrations / "20260729_001_first.sql").write_text(
            "CREATE TABLE first_probe(id INTEGER PRIMARY KEY);\n",
            encoding="utf-8",
        )
        (
            checks / "20260729_001_first_forward_check.sql"
        ).write_text(
            """SELECT count(*) = 1
               FROM sqlite_master
               WHERE type='table' AND name='first_probe';
               SELECT count(*) = 0
               FROM sqlite_master
               WHERE type='table' AND name='second_probe';
            """,
            encoding="utf-8",
        )
        (migrations / "20260729_002_second.sql").write_text(
            "CREATE TABLE second_probe(id INTEGER PRIMARY KEY);\n",
            encoding="utf-8",
        )
        (
            checks / "20260729_002_second_forward_check.sql"
        ).write_text(
            """SELECT count(*) = 2
               FROM sqlite_master
               WHERE type='table'
                 AND name IN ('first_probe','second_probe');
            """,
            encoding="utf-8",
        )
        database = self.root / "cumulative.db"
        sqlite3.connect(database).close()
        migration_runner.apply_migrations(
            database,
            migrations,
            applied_by="cumulative-test",
            source_commit="t1-test",
            formal_database_path=None,
        )

        verified = migration_runner.verify_migrations(database, migrations)

        self.assertEqual(
            verified["verified"],
            ["20260729_001", "20260729_002"],
        )

    def test_required_tables_columns_indexes_and_triggers_exist(self):
        self.apply()
        required_columns = {
            "transfer_cases": {
                "case_id",
                "title",
                "workflow_state",
                "current_school_enrolment_id",
                "upcoming_school_enrolment_id",
                "relationship_status",
                "current_school_name_snapshot",
                "current_school_id",
                "current_city_key",
                "current_school_sector",
                "target_city_key",
                "target_school_sector",
                "rule_status",
                "same_city_result",
                "sector_transition",
                "required_completion_document",
                "tuition_requirement",
                "final_application_id",
                "rule_calculated_at",
                "rule_calculated_by",
                "rule_version",
                "created_at",
                "updated_at",
            },
            "transfer_school_applications": {
                "id",
                "case_id",
                "school_id",
                "school_name_snapshot",
                "institution_group_id",
                "city_key",
                "campus_key",
                "school_sector",
                "expected_start_date",
                "preference_order",
                "application_status",
                "notes",
                "is_final",
                "created_by",
                "created_at",
                "updated_at",
            },
            "case_record_links": {
                "id",
                "case_id",
                "transfer_application_id",
                "record_type",
                "record_id",
                "document_id",
                "link_role",
                "validity_status",
                "effective_from",
                "superseded_by",
                "created_by",
                "created_at",
            },
            "case_material_items": {
                "id",
                "case_id",
                "material_key",
                "material_label_snapshot",
                "material_status",
                "document_id",
                "source_type",
                "is_required",
                "confirmed_by",
                "confirmed_at",
                "created_by",
                "created_at",
                "updated_at",
            },
            "case_payment_requirements": {
                "id",
                "case_id",
                "transfer_application_id",
                "requirement_type",
                "source_offer_id",
                "extracted_amount",
                "confirmed_amount",
                "currency",
                "status",
                "modification_reason",
                "confirmed_by",
                "confirmed_at",
                "created_at",
                "updated_at",
            },
            "case_payment_requirement_proofs": {
                "id",
                "case_id",
                "transfer_application_id",
                "payment_requirement_id",
                "case_record_link_id",
                "validity_status",
                "effective_from",
                "superseded_by",
                "created_by",
                "created_at",
            },
            "case_arrangement_confirmations": {
                "id",
                "case_id",
                "arrangement_type",
                "arrangement_status",
                "linked_record_id",
                "document_id",
                "notes",
                "confirmed_by",
                "confirmed_at",
                "created_at",
                "updated_at",
            },
            "case_completion_confirmations": {
                "id",
                "case_id",
                "action",
                "reverses_confirmation_id",
                "reason",
                "actor_user_id",
                "confirmed_at",
                "effective_date",
                "idempotency_key",
                "workflow_state_before",
                "workflow_state_after",
                "case_status_before",
                "case_status_after",
                "active_slot_before",
                "active_slot_after",
                "terminal_result_before",
                "terminal_result_after",
                "current_enrolment_before_json",
                "current_enrolment_after_json",
                "upcoming_enrolment_before_json",
                "upcoming_enrolment_after_json",
                "readiness_snapshot_json",
                "school_state_before_json",
                "school_state_after_json",
                "metadata_json",
                "created_at",
            },
            "student_source_tags": {
                "id",
                "student_id",
                "source_tag",
                "effective_from",
                "effective_to",
                "source_case_id",
                "created_by",
                "created_at",
            },
            "external_transfer_case_requests": {
                "id",
                "student_id",
                "contract_id",
                "idempotency_key",
                "request_status",
                "case_id",
                "attempt_count",
                "last_error_code",
                "last_error_message",
                "next_retry_at",
                "created_at",
                "updated_at",
                "completed_at",
            },
        }
        required_indexes = {
            "uq_transfer_school_applications_one_final",
            "uq_transfer_school_applications_active_preference",
            "uq_case_record_links_same_record_context",
            "uq_case_record_links_current_application_record",
            "uq_case_record_links_active_case_record",
            "uq_student_source_tags_current",
            "uq_external_transfer_requests_idempotency",
            "uq_external_transfer_requests_contract_student",
            "uq_payment_requirement_proofs_requirement_link",
            "uq_payment_requirement_proofs_link",
            "uq_payment_requirement_proofs_current_requirement",
            "uq_payment_requirement_proofs_current_link",
        }
        required_triggers = {
            "transfer_cases_final_application_insert_guard",
            "transfer_cases_final_application_update_guard",
            "transfer_school_applications_sync_final_insert",
            "transfer_school_applications_sync_final_update",
            "case_record_links_validate_insert",
            "case_record_links_validate_update",
            "case_completion_confirmations_no_update",
            "case_completion_confirmations_no_delete",
            "case_payment_requirement_proofs_validate_insert",
            "case_payment_requirement_proofs_validate_update",
            "case_payment_requirement_proofs_no_delete",
            "case_record_links_payment_proof_relation_guard",
        }
        with closing(self.connect(self.database_path)) as connection:
            self.assertTrue(PHASE2_TABLES.issubset(self.table_names(connection)))
            for table, expected in required_columns.items():
                actual = {
                    row["name"]
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                }
                self.assertTrue(expected.issubset(actual), table)
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
        self.assertTrue(required_indexes.issubset(indexes))
        self.assertTrue(required_triggers.issubset(triggers))

    def test_title_column_is_nullable_text_with_enforced_content_limits(self):
        self.apply()
        with closing(self.connect(self.database_path)) as connection:
            title_column = next(
                dict(row)
                for row in connection.execute(
                    "PRAGMA table_info('transfer_cases')"
                )
                if row["name"] == "title"
            )
            self.assertEqual(title_column["type"].upper(), "TEXT")
            self.assertEqual(title_column["notnull"], 0)
            case_id = self.insert_case(connection)
            self.insert_transfer(connection, case_id)
            connection.execute(
                "UPDATE transfer_cases SET title=NULL WHERE case_id=?",
                (case_id,),
            )
            connection.execute(
                "UPDATE transfer_cases SET title=? WHERE case_id=?",
                ("Internal transfer planning", case_id),
            )
            for invalid_title in (
                "",
                "   ",
                "x" * 201,
                " " + ("x" * 200),
            ):
                with self.subTest(title=repr(invalid_title[:20])):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            """UPDATE transfer_cases SET title=?
                               WHERE case_id=?""",
                            (invalid_title, case_id),
                        )
            self.assertEqual(
                connection.execute(
                    "SELECT title FROM transfer_cases WHERE case_id=?",
                    (case_id,),
                ).fetchone()[0],
                "Internal transfer planning",
            )

    def test_title_migration_preserves_existing_phase2_rows_and_is_noop_on_repeat(
        self,
    ):
        phase2_only = self.root / "phase2_only"
        checks = phase2_only / "checks"
        checks.mkdir(parents=True)
        for source in (PHASE1_MIGRATION_PATH, MIGRATION_PATH):
            shutil.copy2(source, phase2_only / source.name)
        for source in (PHASE1_FORWARD_CHECK_PATH, FORWARD_CHECK_PATH):
            shutil.copy2(source, checks / source.name)
        migration_runner.apply_migrations(
            self.database_path,
            phase2_only,
            applied_by="phase2-title-test",
            source_commit="phase2-title-test",
            formal_database_path=None,
        )
        with closing(self.connect(self.database_path)) as connection:
            case_id = self.insert_case(connection)
            self.insert_transfer(connection, case_id)
            before = dict(
                connection.execute(
                    "SELECT * FROM transfer_cases WHERE case_id=?",
                    (case_id,),
                ).fetchone()
            )
            connection.commit()

        first = self.apply()
        second = self.apply()

        self.assertEqual(
            first["applied"],
            [
                "20260729_003",
                "20260729_004",
                "20260729_005",
                "20260730_006",
                "20260730_007",
                "20260801_008",
            ],
        )
        self.assertEqual(second["applied"], [])
        with closing(self.connect(self.database_path)) as connection:
            after = dict(
                connection.execute(
                    """SELECT case_id,transfer_origin,source_contract_id,
                              workflow_state,current_school_name_snapshot,
                              current_school_id,current_city_key,
                              current_school_sector,target_city_key,
                              target_school_sector,rule_status,
                              same_city_result,sector_transition,
                              required_completion_document,
                              tuition_requirement,final_application_id,
                              rule_calculated_at,rule_calculated_by,
                              rule_version,created_at,updated_at
                       FROM transfer_cases WHERE case_id=?""",
                    (case_id,),
                ).fetchone()
            )
            title = connection.execute(
                "SELECT title FROM transfer_cases WHERE case_id=?",
                (case_id,),
            ).fetchone()[0]
            versions = [
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
        self.assertEqual(after, before)
        self.assertIsNone(title)
        self.assertEqual(
            versions,
            [
                "20260728_001",
                "20260729_002",
                "20260729_003",
                "20260729_004",
                "20260729_005",
                "20260730_006",
                "20260730_007",
                "20260801_008",
            ],
        )

    def test_existing_003_database_applies_only_004_and_preserves_phase2_rows(self):
        migration_runner.apply_migrations(
            self.database_path,
            self.migration_dir_through_003(),
            applied_by="proof-upgrade-test",
            source_commit="proof-upgrade-test",
            formal_database_path=None,
        )
        with closing(self.connect(self.database_path)) as connection:
            case_id = self.insert_case(connection)
            self.insert_transfer(connection, case_id)
            application_id = self.insert_application(connection, case_id)
            requirement_id = self.insert_payment_requirement(
                connection,
                case_id=case_id,
                application_id=application_id,
            )
            before = {
                table: [
                    tuple(row)
                    for row in connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    )
                ]
                for table in PHASE1_TABLES
                | (
                    PHASE2_TABLES
                    - {
                        "case_payment_requirement_proofs",
                        "transfer_guardianship_arrangements",
                        "transfer_accommodation_arrangements",
                        "case_arrangement_evidence_links",
                    }
                )
            }
            connection.commit()

        first = self.apply()
        second = self.apply()

        self.assertEqual(
            first["applied"],
            [
                "20260729_004",
                "20260729_005",
                "20260730_006",
                "20260730_007",
                "20260801_008",
            ],
        )
        self.assertEqual(second["applied"], [])
        with closing(self.connect(self.database_path)) as connection:
            after = {
                table: [
                    tuple(row)
                    for row in connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    )
                ]
                for table in before
            }
            proof_count = connection.execute(
                "SELECT count(*) FROM case_payment_requirement_proofs"
            ).fetchone()[0]
            versions = [
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
        self.assertEqual(
            {
                table: rows
                for table, rows in after.items()
                if table != "transfer_cases"
            },
            {
                table: rows
                for table, rows in before.items()
                if table != "transfer_cases"
            },
        )
        self.assertEqual(
            [
                row[: len(before["transfer_cases"][0])]
                for row in after["transfer_cases"]
            ],
            before["transfer_cases"],
        )
        self.assertEqual(proof_count, 0)
        self.assertGreater(requirement_id, 0)
        self.assertEqual(
            versions,
            [
                "20260728_001",
                "20260729_002",
                "20260729_003",
                "20260729_004",
                "20260729_005",
                "20260730_006",
                "20260730_007",
                "20260801_008",
            ],
        )

    def test_004_failure_rolls_back_new_table_and_registry_row(self):
        migration_runner.apply_migrations(
            self.database_path,
            self.migration_dir_through_003(),
            applied_by="proof-rollback-test",
            source_commit="proof-rollback-test",
            formal_database_path=None,
        )
        broken_dir = self.root / "broken_004"
        checks = broken_dir / "checks"
        checks.mkdir(parents=True)
        for source in (
            PHASE1_MIGRATION_PATH,
            MIGRATION_PATH,
            TITLE_MIGRATION_PATH,
            PROOF_MIGRATION_PATH,
        ):
            shutil.copy2(source, broken_dir / source.name)
        for source in (
            PHASE1_FORWARD_CHECK_PATH,
            FORWARD_CHECK_PATH,
            TITLE_FORWARD_CHECK_PATH,
            PROOF_FORWARD_CHECK_PATH,
        ):
            shutil.copy2(source, checks / source.name)
        with (broken_dir / PROOF_MIGRATION_PATH.name).open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(
                "\nINSERT INTO table_that_does_not_exist(id) VALUES(1);\n"
            )

        with self.assertRaises(migration_runner.MigrationExecutionError):
            self.apply(migrations_dir=broken_dir)

        with closing(self.connect(self.database_path)) as connection:
            self.assertNotIn(
                "case_payment_requirement_proofs",
                self.table_names(connection),
            )
            versions = [
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
        self.assertEqual(
            versions,
            ["20260728_001", "20260729_002", "20260729_003"],
        )

    @unittest.skipUnless(
        T0_BACKUP_PATH.is_file(),
        "historical T0 backup artifact is not present in this worktree",
    )
    def test_t0_backup_recopy_applies_pending_migrations_without_new_fk_errors(self):
        self.assertEqual(sha256(T0_BACKUP_PATH), T0_BACKUP_SHA256)
        copied_once = self.root / "t0-copy.db"
        copied_twice = self.root / "t0-copy-for-004.db"
        shutil.copy2(T0_BACKUP_PATH, copied_once)
        shutil.copy2(copied_once, copied_twice)
        before_foreign_keys = migration_runner.foreign_key_violations(
            copied_twice
        )

        result = self.apply(path=copied_twice)

        after_foreign_keys = migration_runner.foreign_key_violations(
            copied_twice
        )
        with closing(self.connect(copied_twice)) as connection:
            proof_count = connection.execute(
                "SELECT count(*) FROM case_payment_requirement_proofs"
            ).fetchone()[0]
            check = connection.execute("PRAGMA quick_check").fetchone()[0]
        self.assertEqual(
            result["applied"],
            [
                "20260729_002",
                "20260729_003",
                "20260729_004",
                "20260729_005",
                "20260730_006",
                "20260730_007",
                "20260801_008",
            ],
        )
        self.assertEqual(after_foreign_keys, before_foreign_keys)
        self.assertEqual(proof_count, 0)
        self.assertEqual(check, "ok")
        self.assertEqual(sha256(T0_BACKUP_PATH), T0_BACKUP_SHA256)

    def test_illegal_states_sector_amount_currency_and_confirmation_are_rejected(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_transfer(
                    connection,
                    self.insert_case(
                        connection, student_id="student-2", sequence_no=1
                    ),
                    workflow_state="made_up",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_application(
                    connection,
                    case_id,
                    preference_order=2,
                    school_sector="government",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO case_payment_requirements(
                           case_id,transfer_application_id,requirement_type,
                           extracted_amount,currency,status,created_at,updated_at
                       ) VALUES(?,?, 'deposit', -1, 'AUD', 'pending', ?, ?)""",
                    (case_id, application_id, "now", "now"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO case_payment_requirements(
                           case_id,transfer_application_id,requirement_type,
                           extracted_amount,currency,status,created_at,updated_at
                       ) VALUES(?,?, 'deposit', 1, 'aud', 'pending', ?, ?)""",
                    (case_id, application_id, "now", "now"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO case_material_items(
                           case_id,material_key,material_label_snapshot,
                           material_status,source_type,is_required,
                           created_by,created_at,updated_at
                       ) VALUES(?,?,?,'confirmed','staff_added',1,1,?,?)""",
                    (case_id, "passport", "Passport", "now", "now"),
                )

    def test_final_application_is_unique_same_case_and_pointer_is_synchronised(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            connection.execute(
                "UPDATE transfer_school_applications SET is_final=1 WHERE id=?",
                (application_id,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT final_application_id FROM transfer_cases WHERE case_id=?",
                    (case_id,),
                ).fetchone()[0],
                application_id,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE transfer_cases SET final_application_id=NULL
                       WHERE case_id=?""",
                    (case_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_application(
                    connection,
                    case_id,
                    preference_order=2,
                    is_final=1,
                )
            second_case = self.insert_case(
                connection, student_id="student-2", sequence_no=1
            )
            self.insert_transfer(connection, second_case)
            second_application = self.insert_application(
                connection, second_case, preference_order=1
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE transfer_cases SET final_application_id=?
                       WHERE case_id=?""",
                    (second_application, case_id),
                )
            connection.execute(
                "UPDATE transfer_school_applications SET is_final=0 WHERE id=?",
                (application_id,),
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT final_application_id FROM transfer_cases WHERE case_id=?",
                    (case_id,),
                ).fetchone()[0]
            )

    def test_active_preference_order_is_unique_but_inactive_history_can_reuse_it(self):
        connection, case_id, _ = self.migrated_case_graph()
        with closing(connection):
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_application(connection, case_id, preference_order=1)
            connection.execute(
                """UPDATE transfer_school_applications
                   SET application_status='withdrawn' WHERE case_id=?""",
                (case_id,),
            )
            self.insert_application(connection, case_id, preference_order=1)

    def insert_link(
        self,
        connection,
        *,
        case_id,
        application_id=None,
        record_type,
        record_id,
        validity_status="current",
        document_id=None,
        link_role="current",
    ):
        return connection.execute(
            """INSERT INTO case_record_links(
                   case_id,transfer_application_id,record_type,record_id,
                   document_id,link_role,validity_status,effective_from,
                   created_by,created_at
               ) VALUES(?,?,?,?,?,?,?,?,1,?)""",
            (
                case_id,
                application_id,
                record_type,
                record_id,
                document_id,
                link_role,
                validity_status,
                "2026-07-29T12:00:00+10:00",
                "2026-07-29T12:00:00+10:00",
            ),
        ).lastrowid

    def test_polymorphic_links_require_real_typed_same_student_records(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            valid = [
                ("offer", 1000, application_id, None),
                ("payment", 1500, application_id, None),
                ("coe", 1100, application_id, None),
                ("confirmation_letter", 1200, application_id, None),
                ("visa", 1400, None, None),
                ("oshc", 1300, None, None),
                ("student_document", 1600, application_id, 1600),
            ]
            for index, (record_type, record_id, app_id, document_id) in enumerate(
                valid
            ):
                with self.subTest(record_type=record_type):
                    self.insert_link(
                        connection,
                        case_id=case_id,
                        application_id=app_id,
                        record_type=record_type,
                        record_id=record_id,
                        document_id=document_id,
                        validity_status=(
                            "historical" if index else "current"
                        ),
                        link_role="evidence",
                    )
            invalid = [
                ("offer", 999999, application_id, None),
                ("offer", 2000, application_id, None),
                ("coe", 1200, application_id, None),
                ("visa", 2400, None, None),
                ("student_document", 1600, application_id, None),
            ]
            for record_type, record_id, app_id, document_id in invalid:
                with self.subTest(
                    invalid_type=record_type, invalid_id=record_id
                ):
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.insert_link(
                            connection,
                            case_id=case_id,
                            application_id=app_id,
                            record_type=record_type,
                            record_id=record_id,
                            document_id=document_id,
                            validity_status="historical",
                        )

    def test_application_scoped_record_types_and_cross_case_links_are_rejected(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            for record_type, record_id in (
                ("offer", 1000),
                ("payment", 1500),
                ("coe", 1100),
                ("confirmation_letter", 1200),
            ):
                with self.subTest(record_type=record_type):
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.insert_link(
                            connection,
                            case_id=case_id,
                            record_type=record_type,
                            record_id=record_id,
                            validity_status="historical",
                        )
            second_case = self.insert_case(
                connection, student_id="student-2", sequence_no=1
            )
            self.insert_transfer(connection, second_case)
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_link(
                    connection,
                    case_id=second_case,
                    application_id=application_id,
                    record_type="offer",
                    record_id=2000,
                )

    def test_current_record_uniqueness_is_enforced_per_required_context(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            application_records = {
                "offer": (1000, 1001),
                "coe": (1100, 1101),
                "confirmation_letter": (1200, 1201),
            }
            for record_type, (first, second) in application_records.items():
                with self.subTest(record_type=record_type):
                    self.insert_link(
                        connection,
                        case_id=case_id,
                        application_id=application_id,
                        record_type=record_type,
                        record_id=first,
                    )
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.insert_link(
                            connection,
                            case_id=case_id,
                            application_id=application_id,
                            record_type=record_type,
                            record_id=second,
                        )
            for record_type, first, second in (
                ("visa", 1400, 1401),
                ("oshc", 1300, 1301),
            ):
                with self.subTest(record_type=record_type):
                    self.insert_link(
                        connection,
                        case_id=case_id,
                        record_type=record_type,
                        record_id=first,
                        validity_status="carried_forward",
                    )
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.insert_link(
                            connection,
                            case_id=case_id,
                            record_type=record_type,
                            record_id=second,
                            validity_status="current",
                        )

    def test_same_business_record_cannot_be_linked_twice_in_one_context(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="offer",
                record_id=1000,
                validity_status="historical",
                link_role="evidence",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_link(
                    connection,
                    case_id=case_id,
                    application_id=application_id,
                    record_type="offer",
                    record_id=1000,
                    validity_status="superseded",
                    link_role="historical",
                )

    def test_payment_requirement_proofs_accept_typed_payment_and_document_links(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            deposit_id = self.insert_payment_requirement(
                connection,
                case_id=case_id,
                application_id=application_id,
            )
            first_payment_id = self.insert_payment_requirement(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_type="first_payment",
            )
            payment_link_id = self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="payment",
                record_id=1500,
                link_role="payment_proof",
            )
            document_link_id = self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="student_document",
                record_id=1600,
                document_id=1600,
                link_role="payment_proof",
            )

            payment_proof_id = self.insert_payment_proof(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_id=deposit_id,
                link_id=payment_link_id,
            )
            document_proof_id = self.insert_payment_proof(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_id=first_payment_id,
                link_id=document_link_id,
            )
            connection.commit()

            rows = connection.execute(
                """SELECT id,payment_requirement_id,case_record_link_id,
                          validity_status
                   FROM case_payment_requirement_proofs ORDER BY id"""
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    (payment_proof_id, deposit_id, payment_link_id, "current"),
                    (
                        document_proof_id,
                        first_payment_id,
                        document_link_id,
                        "current",
                    ),
                ],
            )

    def test_payment_requirement_proofs_reject_wrong_type_role_and_link_status(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            requirement_id = self.insert_payment_requirement(
                connection,
                case_id=case_id,
                application_id=application_id,
            )
            invalid_links = [
                self.insert_link(
                    connection,
                    case_id=case_id,
                    application_id=application_id,
                    record_type="offer",
                    record_id=1000,
                    link_role="payment_proof",
                ),
                self.insert_link(
                    connection,
                    case_id=case_id,
                    application_id=application_id,
                    record_type="payment",
                    record_id=1500,
                    link_role="evidence",
                    validity_status="historical",
                ),
                self.insert_link(
                    connection,
                    case_id=case_id,
                    application_id=application_id,
                    record_type="payment",
                    record_id=1501,
                    link_role="payment_proof",
                    validity_status="pending_confirmation",
                ),
            ]
            for link_id in invalid_links:
                with self.subTest(link_id=link_id):
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.insert_payment_proof(
                            connection,
                            case_id=case_id,
                            application_id=application_id,
                            requirement_id=requirement_id,
                            link_id=link_id,
                        )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_payment_proof(
                    connection,
                    case_id=case_id,
                    application_id=application_id,
                    requirement_id=requirement_id,
                    link_id=invalid_links[1],
                    validity_status="rejected",
                )

    def test_payment_requirement_proofs_reject_cross_case_application_and_student(self):
        connection, first_case, first_application = self.migrated_case_graph()
        with closing(connection):
            first_requirement = self.insert_payment_requirement(
                connection,
                case_id=first_case,
                application_id=first_application,
            )
            second_application = self.insert_application(
                connection,
                first_case,
                preference_order=2,
            )
            second_app_link = self.insert_link(
                connection,
                case_id=first_case,
                application_id=second_application,
                record_type="payment",
                record_id=1500,
                link_role="payment_proof",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_payment_proof(
                    connection,
                    case_id=first_case,
                    application_id=first_application,
                    requirement_id=first_requirement,
                    link_id=second_app_link,
                )

            second_case = self.insert_case(
                connection, student_id="student-2", sequence_no=1
            )
            self.insert_transfer(connection, second_case)
            second_case_application = self.insert_application(
                connection, second_case
            )
            second_case_link = self.insert_link(
                connection,
                case_id=second_case,
                application_id=second_case_application,
                record_type="payment",
                record_id=2500,
                link_role="payment_proof",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_payment_proof(
                    connection,
                    case_id=first_case,
                    application_id=first_application,
                    requirement_id=first_requirement,
                    link_id=second_case_link,
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_payment_proof(
                    connection,
                    case_id=second_case,
                    application_id=second_case_application,
                    requirement_id=first_requirement,
                    link_id=second_case_link,
                )

    def test_payment_requirement_and_proof_current_uniqueness_is_database_enforced(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            deposit_id = self.insert_payment_requirement(
                connection,
                case_id=case_id,
                application_id=application_id,
            )
            first_payment_id = self.insert_payment_requirement(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_type="first_payment",
            )
            first_link = self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="payment",
                record_id=1500,
                link_role="payment_proof",
            )
            second_link = self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="payment",
                record_id=1501,
                link_role="payment_proof",
            )
            self.insert_payment_proof(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_id=deposit_id,
                link_id=first_link,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_payment_proof(
                    connection,
                    case_id=case_id,
                    application_id=application_id,
                    requirement_id=deposit_id,
                    link_id=second_link,
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_payment_proof(
                    connection,
                    case_id=case_id,
                    application_id=application_id,
                    requirement_id=first_payment_id,
                    link_id=first_link,
                    validity_status="historical",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_payment_proof(
                    connection,
                    case_id=case_id,
                    application_id=application_id,
                    requirement_id=deposit_id,
                    link_id=first_link,
                    validity_status="historical",
                )

    def test_payment_proof_replacement_preserves_a_deferred_supersede_chain(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            requirement_id = self.insert_payment_requirement(
                connection,
                case_id=case_id,
                application_id=application_id,
            )
            old_link = self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="payment",
                record_id=1500,
                link_role="payment_proof",
            )
            new_link = self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="payment",
                record_id=1501,
                link_role="payment_proof",
            )
            old_proof = self.insert_payment_proof(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_id=requirement_id,
                link_id=old_link,
            )
            new_proof = old_proof + 1

            connection.execute(
                """UPDATE case_payment_requirement_proofs
                   SET validity_status='superseded',superseded_by=?
                   WHERE id=?""",
                (new_proof, old_proof),
            )
            self.insert_payment_proof(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_id=requirement_id,
                link_id=new_link,
                proof_id=new_proof,
            )
            connection.execute(
                """UPDATE case_record_links
                   SET validity_status='superseded',superseded_by=?
                   WHERE id=?""",
                (new_link, old_link),
            )
            connection.commit()

            self.assertEqual(
                [
                    tuple(row)
                    for row in connection.execute(
                        """SELECT id,validity_status,superseded_by
                           FROM case_payment_requirement_proofs ORDER BY id"""
                    )
                ],
                [
                    (old_proof, "superseded", new_proof),
                    (new_proof, "current", None),
                ],
            )

    def test_payment_proof_supersede_cannot_cross_requirement_or_point_to_self(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            first_requirement = self.insert_payment_requirement(
                connection,
                case_id=case_id,
                application_id=application_id,
            )
            second_requirement = self.insert_payment_requirement(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_type="first_payment",
            )
            first_link = self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="payment",
                record_id=1500,
                link_role="payment_proof",
            )
            second_link = self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="payment",
                record_id=1501,
                link_role="payment_proof",
                validity_status="historical",
            )
            first_proof = self.insert_payment_proof(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_id=first_requirement,
                link_id=first_link,
            )
            second_proof = self.insert_payment_proof(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_id=second_requirement,
                link_id=second_link,
                validity_status="historical",
            )
            connection.commit()

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE case_payment_requirement_proofs
                       SET validity_status='superseded',superseded_by=id
                       WHERE id=?""",
                    (first_proof,),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE case_payment_requirement_proofs
                       SET validity_status='superseded',superseded_by=?
                       WHERE id=?""",
                    (second_proof, first_proof),
                )
            connection.rollback()

    def test_payment_proof_supersede_target_must_be_current_when_chain_is_created(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            requirement_id = self.insert_payment_requirement(
                connection,
                case_id=case_id,
                application_id=application_id,
            )
            current_link = self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="payment",
                record_id=1500,
                link_role="payment_proof",
            )
            historical_link = self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="payment",
                record_id=1501,
                link_role="payment_proof",
                validity_status="historical",
            )
            future_historical_link = self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="student_document",
                record_id=1600,
                document_id=1600,
                link_role="payment_proof",
                validity_status="historical",
            )
            current_proof = self.insert_payment_proof(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_id=requirement_id,
                link_id=current_link,
            )
            historical_proof = self.insert_payment_proof(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_id=requirement_id,
                link_id=historical_link,
                validity_status="historical",
            )
            connection.commit()

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE case_payment_requirement_proofs
                       SET validity_status='superseded',superseded_by=?
                       WHERE id=?""",
                    (historical_proof, current_proof),
                )
            connection.rollback()

            future_proof = historical_proof + 1
            connection.execute(
                """UPDATE case_payment_requirement_proofs
                   SET validity_status='superseded',superseded_by=?
                   WHERE id=?""",
                (future_proof, current_proof),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_payment_proof(
                    connection,
                    case_id=case_id,
                    application_id=application_id,
                    requirement_id=requirement_id,
                    link_id=future_historical_link,
                    validity_status="historical",
                    proof_id=future_proof,
                )
            connection.rollback()

    def test_payment_proof_identity_history_and_delete_are_database_protected(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            requirement_id = self.insert_payment_requirement(
                connection,
                case_id=case_id,
                application_id=application_id,
            )
            link_id = self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="payment",
                record_id=1500,
                link_role="payment_proof",
                validity_status="historical",
            )
            proof_id = self.insert_payment_proof(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_id=requirement_id,
                link_id=link_id,
                validity_status="historical",
            )
            connection.commit()

            identity_changes = {
                "case_id": case_id + 1000,
                "transfer_application_id": application_id + 1000,
                "payment_requirement_id": requirement_id + 1000,
                "case_record_link_id": link_id + 1000,
                "created_by": 2,
                "created_at": "changed",
            }
            for column, value in identity_changes.items():
                with self.subTest(column=column):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            f"""UPDATE case_payment_requirement_proofs
                                SET {column}=? WHERE id=?""",
                            (value, proof_id),
                        )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE case_payment_requirement_proofs
                       SET validity_status='current' WHERE id=?""",
                    (proof_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM case_payment_requirement_proofs WHERE id=?",
                    (proof_id,),
                )
            self.assertEqual(
                connection.execute(
                    """SELECT validity_status
                       FROM case_payment_requirement_proofs WHERE id=?""",
                    (proof_id,),
                ).fetchone()[0],
                "historical",
            )

    def test_current_payment_proof_relation_guards_the_underlying_record_link(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            requirement_id = self.insert_payment_requirement(
                connection,
                case_id=case_id,
                application_id=application_id,
            )
            link_id = self.insert_link(
                connection,
                case_id=case_id,
                application_id=application_id,
                record_type="payment",
                record_id=1500,
                link_role="payment_proof",
            )
            self.insert_payment_proof(
                connection,
                case_id=case_id,
                application_id=application_id,
                requirement_id=requirement_id,
                link_id=link_id,
            )
            for assignment in (
                "link_role='evidence'",
                "validity_status='historical'",
            ):
                with self.subTest(assignment=assignment):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            f"UPDATE case_record_links SET {assignment} WHERE id=?",
                            (link_id,),
                        )

    def test_payment_and_arrangement_cross_case_references_are_rejected(self):
        connection, first_case, first_application = self.migrated_case_graph()
        with closing(connection):
            second_case = self.insert_case(
                connection, student_id="student-2", sequence_no=1
            )
            self.insert_transfer(connection, second_case)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO case_payment_requirements(
                           case_id,transfer_application_id,requirement_type,
                           status,created_at,updated_at
                       ) VALUES(?,?,'deposit','pending',?,?)""",
                    (second_case, first_application, "now", "now"),
                )
            link_id = self.insert_link(
                connection,
                case_id=first_case,
                record_type="visa",
                record_id=1400,
                validity_status="historical",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO case_arrangement_confirmations(
                           case_id,arrangement_type,arrangement_status,
                           linked_record_id,created_at,updated_at
                       ) VALUES(?,'visa','updated',?,?,?)""",
                    (second_case, link_id, "now", "now"),
                )

    def test_source_tag_current_uniqueness_history_and_request_idempotency(self):
        connection, case_id, _ = self.migrated_case_graph()
        with closing(connection):
            connection.execute(
                """INSERT INTO student_source_tags(
                       student_id,source_tag,effective_from,source_case_id,
                       created_by,created_at
                   ) VALUES('student-1','external_transfer','2026-07-29',?,1,'now')""",
                (case_id,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO student_source_tags(
                           student_id,source_tag,effective_from,created_by,created_at
                       ) VALUES('student-1','external_transfer','2026-07-30',1,'now')"""
                )
            connection.execute(
                """UPDATE student_source_tags SET effective_to='2026-07-30'
                   WHERE student_id='student-1'"""
            )
            connection.execute(
                """INSERT INTO student_source_tags(
                       student_id,source_tag,effective_from,created_by,created_at
                   ) VALUES('student-1','external_transfer','2026-07-31',1,'now')"""
            )
            connection.execute(
                """INSERT INTO external_transfer_case_requests(
                       student_id,contract_id,idempotency_key,request_status,
                       attempt_count,created_at,updated_at
                   ) VALUES('student-1',100,'request-1','pending',0,'now','now')"""
            )
            for contract_id, key in ((None, "request-1"), (100, "request-2")):
                with self.subTest(contract_id=contract_id, key=key):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            """INSERT INTO external_transfer_case_requests(
                                   student_id,contract_id,idempotency_key,
                                   request_status,attempt_count,created_at,updated_at
                               ) VALUES('student-1',?,?,'pending',0,'now','now')""",
                            (contract_id, key),
                        )

    def test_completion_is_append_only_and_all_phase2_tables_reject_delete(self):
        connection, case_id, application_id = self.migrated_case_graph()
        with closing(connection):
            completion_id = self.insert_completion_confirmation(
                connection,
                case_id=case_id,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE case_completion_confirmations SET reason='changed'
                       WHERE id=?""",
                    (completion_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM case_completion_confirmations WHERE id=?",
                    (completion_id,),
                )
            populated = {
                "transfer_school_applications": application_id,
                "transfer_cases": case_id,
            }
            for table, row_id in populated.items():
                key = "case_id" if table == "transfer_cases" else "id"
                with self.subTest(table=table):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            f'DELETE FROM "{table}" WHERE "{key}"=?', (row_id,)
                        )

    def test_every_phase2_table_has_delete_protection_trigger(self):
        self.apply()
        with closing(self.connect(self.database_path)) as connection:
            protected = {
                row["tbl_name"]
                for row in connection.execute(
                    """SELECT tbl_name FROM sqlite_master
                       WHERE type='trigger' AND name LIKE '%no_delete'"""
                )
            }
        self.assertTrue(PHASE2_TABLES.issubset(protected))

    def test_old_schema_rows_flags_and_phase1_data_are_unchanged(self):
        old_tables = {
            "users",
            "students",
            "schools",
            "contracts",
            "offers",
            "coe_oshc",
            "visas",
            "payments",
            "student_documents",
            "roles",
            "system_settings",
            "legacy_probe",
        }
        with closing(self.connect(self.database_path)) as connection:
            before_schema = {
                table: connection.execute(
                    """SELECT sql FROM sqlite_master
                       WHERE type='table' AND name=?""",
                    (table,),
                ).fetchone()[0]
                for table in old_tables
            }
            before_rows = {
                table: [
                    tuple(row)
                    for row in connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    )
                ]
                for table in old_tables
            }
        self.apply()
        with closing(self.connect(self.database_path)) as connection:
            after_schema = {
                table: connection.execute(
                    """SELECT sql FROM sqlite_master
                       WHERE type='table' AND name=?""",
                    (table,),
                ).fetchone()[0]
                for table in old_tables
            }
            after_rows = {
                table: [
                    tuple(row)
                    for row in connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    )
                ]
                for table in old_tables
            }
            phase1_counts = {
                table: connection.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
                for table in PHASE1_TABLES
            }
            phase2_counts = {
                table: connection.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
                for table in PHASE2_TABLES
            }
        self.assertEqual(before_schema, after_schema)
        self.assertEqual(before_rows, after_rows)
        self.assertEqual(set(phase1_counts.values()), {0})
        self.assertEqual(set(phase2_counts.values()), {0})

    def test_phase2_failure_rolls_back_every_phase2_object(self):
        self.apply_phase1_only()
        broken_dir = self.root / "broken_migrations"
        checks = broken_dir / "checks"
        checks.mkdir(parents=True)
        for source in (PHASE1_MIGRATION_PATH, MIGRATION_PATH):
            shutil.copy2(source, broken_dir / source.name)
        for source in (PHASE1_FORWARD_CHECK_PATH, FORWARD_CHECK_PATH):
            shutil.copy2(source, checks / source.name)
        with (broken_dir / MIGRATION_PATH.name).open("a", encoding="utf-8") as handle:
            handle.write(
                "\nINSERT INTO table_that_does_not_exist(id) VALUES(1);\n"
            )
        with self.assertRaises(migration_runner.MigrationExecutionError):
            self.apply(migrations_dir=broken_dir)
        with closing(self.connect(self.database_path)) as connection:
            self.assertTrue(
                PHASE2_TABLES.isdisjoint(self.table_names(connection))
            )
            versions = [
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
        self.assertEqual(versions, ["20260728_001"])

    def test_forward_check_detects_missing_critical_trigger_and_index(self):
        self.apply()
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                "DROP TRIGGER case_completion_confirmations_no_update"
            )
        with self.assertRaises(migration_runner.MigrationForwardCheckError):
            migration_runner.verify_migrations(
                self.database_path, self.migration_dir_through_008()
            )

    def test_arrangement_schema_has_structured_tables_and_confirmation_fields(self):
        self.apply()
        expected_columns = {
            "transfer_guardianship_arrangements": {
                "id",
                "case_id",
                "version_no",
                "arrangement_mode",
                "guardian_person_name",
                "guardian_organisation_name",
                "relationship_to_student",
                "provider_name",
                "start_date",
                "end_date",
                "source_type",
                "arrangement_status",
                "is_current",
                "superseded_by",
                "notes",
                "created_by",
                "created_at",
                "updated_by",
                "updated_at",
            },
            "transfer_accommodation_arrangements": {
                "id",
                "case_id",
                "version_no",
                "accommodation_type",
                "provider_name",
                "host_name",
                "address_line1",
                "address_line2",
                "suburb",
                "state_code",
                "postcode",
                "country_code",
                "start_date",
                "end_date",
                "source_type",
                "arrangement_status",
                "is_current",
                "superseded_by",
                "notes",
                "created_by",
                "created_at",
                "updated_by",
                "updated_at",
            },
            "case_arrangement_evidence_links": {
                "id",
                "case_id",
                "arrangement_type",
                "arrangement_confirmation_id",
                "guardianship_arrangement_id",
                "accommodation_arrangement_id",
                "case_record_link_id",
                "document_id",
                "evidence_role",
                "validity_status",
                "effective_from",
                "superseded_by",
                "created_by",
                "created_at",
            },
        }
        with closing(self.connect(self.database_path)) as connection:
            for table, expected in expected_columns.items():
                actual = {
                    row["name"]
                    for row in connection.execute(
                        f'PRAGMA table_info("{table}")'
                    )
                }
                self.assertEqual(actual, expected, table)
            confirmation_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info('case_arrangement_confirmations')"
                )
            }
            self.assertTrue(
                {
                    "guardianship_arrangement_id",
                    "accommodation_arrangement_id",
                    "rule_version",
                    "requirement_reason",
                    "updated_by",
                }.issubset(confirmation_columns)
            )
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
        self.assertTrue(
            {
                "uq_transfer_guardianship_current",
                "uq_transfer_guardianship_version",
                "uq_transfer_accommodation_current",
                "uq_transfer_accommodation_version",
                "uq_arrangement_evidence_current_source",
            }.issubset(indexes)
        )
        self.assertTrue(
            {
                "transfer_guardianship_arrangements_validate_insert",
                "transfer_guardianship_arrangements_validate_update",
                "transfer_guardianship_arrangements_no_delete",
                "transfer_accommodation_arrangements_validate_insert",
                "transfer_accommodation_arrangements_validate_update",
                "transfer_accommodation_arrangements_no_delete",
                "case_arrangement_evidence_links_validate_insert",
                "case_arrangement_evidence_links_validate_update",
                "case_arrangement_evidence_links_no_delete",
                "case_arrangement_confirmations_t7_validate_insert",
                "case_arrangement_confirmations_t7_validate_update",
            }.issubset(triggers)
        )

    def test_guardianship_structure_current_version_and_history_are_enforced(self):
        connection, case_id, _application_id = self.migrated_case_graph()
        with closing(connection):
            pending_id = self.insert_guardianship_arrangement(
                connection, case_id
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_guardianship_arrangement(
                    connection,
                    case_id,
                    version_no=2,
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_guardianship_arrangement(
                    connection,
                    case_id,
                    version_no=3,
                    is_current=0,
                    superseded_by=pending_id,
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE transfer_guardianship_arrangements
                       SET arrangement_mode='other' WHERE id=?""",
                    (pending_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM transfer_guardianship_arrangements WHERE id=?",
                    (pending_id,),
                )
            connection.execute(
                """UPDATE transfer_guardianship_arrangements
                   SET is_current=0,superseded_by=99,updated_by=1,updated_at=?
                   WHERE id=?""",
                ("2026-07-30T12:00:00+10:00", pending_id),
            )
            replacement_id = self.insert_guardianship_arrangement(
                connection,
                case_id,
                id=99,
                version_no=2,
                arrangement_mode="parent_guardian",
                guardian_person_name="Parent",
                source_type="updated_arrangement",
                arrangement_status="confirmed",
            )
            self.assertEqual(replacement_id, 99)
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE transfer_guardianship_arrangements
                       SET is_current=1,superseded_by=NULL WHERE id=?""",
                    (pending_id,),
                )

    def test_guardianship_minimum_details_dates_and_not_applicable_reason(self):
        connection, case_id, _application_id = self.migrated_case_graph()
        with closing(connection):
            legal = (
                {
                    "arrangement_mode": "parent_guardian",
                    "guardian_person_name": "Parent",
                },
                {
                    "arrangement_mode": "nominated_guardian",
                    "guardian_person_name": "Nominee",
                },
                {
                    "arrangement_mode": "school_arranged",
                    "guardian_organisation_name": "School",
                },
                {
                    "arrangement_mode": "welfare_provider",
                    "provider_name": "Provider",
                },
                {
                    "arrangement_mode": "caaw",
                    "start_date": "2026-08-01",
                    "end_date": "2027-08-01",
                },
            )
            for details in legal:
                with self.subTest(details=details):
                    connection.execute("SAVEPOINT legal_guardianship")
                    self.insert_guardianship_arrangement(
                        connection,
                        case_id,
                        arrangement_status="confirmed",
                        **details,
                    )
                    connection.execute(
                        "ROLLBACK TO SAVEPOINT legal_guardianship"
                    )
                    connection.execute("RELEASE SAVEPOINT legal_guardianship")
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_guardianship_arrangement(
                    connection,
                    case_id,
                    arrangement_mode="parent_guardian",
                    arrangement_status="confirmed",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_guardianship_arrangement(
                    connection,
                    case_id,
                    start_date="2027-01-01",
                    end_date="2026-01-01",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_guardianship_arrangement(
                    connection,
                    case_id,
                    arrangement_status="not_applicable",
                )
            self.insert_guardianship_arrangement(
                connection,
                case_id,
                arrangement_status="not_applicable",
                notes="Adult at target course start",
            )

    def test_accommodation_structure_formats_and_minimum_details_are_enforced(self):
        connection, case_id, _application_id = self.migrated_case_graph()
        with closing(connection):
            self.insert_accommodation_arrangement(
                connection,
                case_id,
                accommodation_type="homestay",
                provider_name="Provider",
                state_code="VIC",
                postcode="3000",
                country_code="AU",
                start_date="2026-08-01",
                end_date="2027-08-01",
                arrangement_status="confirmed",
            )
            for overrides in (
                {
                    "version_no": 2,
                    "accommodation_type": "homestay",
                    "arrangement_status": "confirmed",
                },
                {"version_no": 3, "state_code": "vic"},
                {"version_no": 4, "postcode": "3!00"},
                {"version_no": 5, "country_code": "AUS"},
                {
                    "version_no": 6,
                    "start_date": "2027-01-01",
                    "end_date": "2026-01-01",
                },
                {
                    "version_no": 7,
                    "arrangement_status": "not_applicable",
                },
            ):
                with self.subTest(overrides=overrides):
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.insert_accommodation_arrangement(
                            connection, case_id, **overrides
                        )

    def test_confirmation_type_case_status_and_identity_guards_are_enforced(self):
        connection, case_id, _application_id = self.migrated_case_graph()
        with closing(connection):
            visa_link = self.insert_case_record_link(
                connection, case_id, "visa", 1400
            )
            oshc_link = self.insert_case_record_link(
                connection, case_id, "oshc", 1300
            )
            visa_confirmation = connection.execute(
                """INSERT INTO case_arrangement_confirmations(
                       case_id,arrangement_type,arrangement_status,
                       linked_record_id,confirmed_by,confirmed_at,
                       created_at,updated_at
                   ) VALUES(?,'visa','confirmed',?,1,?,?,?)""",
                (
                    case_id,
                    visa_link,
                    "2026-07-29T12:00:00+10:00",
                    "2026-07-29T12:00:00+10:00",
                    "2026-07-29T12:00:00+10:00",
                ),
            ).lastrowid
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO case_arrangement_confirmations(
                           case_id,arrangement_type,arrangement_status,
                           linked_record_id,created_at,updated_at
                       ) VALUES(?,'oshc','updated',?,?,?)""",
                    (
                        case_id,
                        visa_link,
                        "2026-07-29T12:00:00+10:00",
                        "2026-07-29T12:00:00+10:00",
                    ),
                )
            connection.execute(
                """INSERT INTO case_arrangement_confirmations(
                       case_id,arrangement_type,arrangement_status,
                       linked_record_id,created_at,updated_at
                   ) VALUES(?,'oshc','updated',?,?,?)""",
                (
                    case_id,
                    oshc_link,
                    "2026-07-29T12:00:00+10:00",
                    "2026-07-29T12:00:00+10:00",
                ),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO case_arrangement_confirmations(
                           case_id,arrangement_type,arrangement_status,
                           created_at,updated_at
                       ) VALUES(?,'accommodation','not_applicable',?,?)""",
                    (
                        case_id,
                        "2026-07-29T12:00:00+10:00",
                        "2026-07-29T12:00:00+10:00",
                    ),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE case_arrangement_confirmations
                       SET arrangement_type='guardianship' WHERE id=?""",
                    (visa_confirmation,),
                )

    def test_dedicated_confirmation_and_evidence_cross_case_student_guards(self):
        connection, case_id, _application_id = self.migrated_case_graph()
        with closing(connection):
            guardian_id = self.insert_guardianship_arrangement(
                connection,
                case_id,
                arrangement_mode="parent_guardian",
                guardian_person_name="Parent",
                arrangement_status="confirmed",
            )
            guardian_confirmation = connection.execute(
                """INSERT INTO case_arrangement_confirmations(
                       case_id,arrangement_type,arrangement_status,
                       guardianship_arrangement_id,confirmed_by,confirmed_at,
                       created_at,updated_at
                   ) VALUES(?,'guardianship','confirmed',?,1,?,?,?)""",
                (
                    case_id,
                    guardian_id,
                    "2026-07-29T12:00:00+10:00",
                    "2026-07-29T12:00:00+10:00",
                    "2026-07-29T12:00:00+10:00",
                ),
            ).lastrowid
            evidence_id = connection.execute(
                """INSERT INTO case_arrangement_evidence_links(
                       case_id,arrangement_type,guardianship_arrangement_id,
                       document_id,evidence_role,validity_status,effective_from,
                       created_by,created_at
                   ) VALUES(?,'guardianship',?,1600,'authority','current',?,1,?)""",
                (
                    case_id,
                    guardian_id,
                    "2026-07-29",
                    "2026-07-29T12:00:00+10:00",
                ),
            ).lastrowid
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO case_arrangement_evidence_links(
                           case_id,arrangement_type,guardianship_arrangement_id,
                           document_id,evidence_role,validity_status,
                           effective_from,created_by,created_at
                       ) VALUES(?,'guardianship',?,1600,'authority','current',?,1,?)""",
                    (
                        case_id,
                        guardian_id,
                        "2026-07-29",
                        "2026-07-29T12:00:00+10:00",
                    ),
                )
            for statement, parameters in (
                (
                    """INSERT INTO case_arrangement_evidence_links(
                           case_id,arrangement_type,
                           guardianship_arrangement_id,evidence_role,
                           validity_status,effective_from,created_by,created_at
                       ) VALUES(?,'guardianship',?,'authority','current',?,1,?)""",
                    (
                        case_id,
                        guardian_id,
                        "2026-07-29",
                        "2026-07-29T12:00:00+10:00",
                    ),
                ),
                (
                    """INSERT INTO case_arrangement_evidence_links(
                           case_id,arrangement_type,
                           arrangement_confirmation_id,
                           guardianship_arrangement_id,document_id,
                           evidence_role,validity_status,effective_from,
                           created_by,created_at
                       ) VALUES(?,'guardianship',?,?,1601,'supporting',
                                'current',?,1,?)""",
                    (
                        case_id,
                        guardian_confirmation,
                        guardian_id,
                        "2026-07-29",
                        "2026-07-29T12:00:00+10:00",
                    ),
                ),
                (
                    """INSERT INTO case_arrangement_evidence_links(
                           case_id,arrangement_type,
                           guardianship_arrangement_id,document_id,
                           evidence_role,validity_status,effective_from,
                           created_by,created_at
                       ) VALUES(?,'accommodation',?,1601,'supporting',
                                'current',?,1,?)""",
                    (
                        case_id,
                        guardian_id,
                        "2026-07-29",
                        "2026-07-29T12:00:00+10:00",
                    ),
                ),
                (
                    """INSERT INTO case_arrangement_evidence_links(
                           case_id,arrangement_type,
                           guardianship_arrangement_id,document_id,
                           evidence_role,validity_status,effective_from,
                           created_by,created_at
                       ) VALUES(?,'guardianship',?,2600,'supporting',
                                'current',?,1,?)""",
                    (
                        case_id,
                        guardian_id,
                        "2026-07-29",
                        "2026-07-29T12:00:00+10:00",
                    ),
                ),
            ):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(statement, parameters)
            replacement_id = connection.execute(
                """INSERT INTO case_arrangement_evidence_links(
                       case_id,arrangement_type,guardianship_arrangement_id,
                       document_id,evidence_role,validity_status,effective_from,
                       created_by,created_at
                   ) VALUES(?,'guardianship',?,1601,'supporting','current',?,1,?)""",
                (
                    case_id,
                    guardian_id,
                    "2026-07-30",
                    "2026-07-30T12:00:00+10:00",
                ),
            ).lastrowid
            connection.execute(
                """UPDATE case_arrangement_evidence_links
                   SET validity_status='superseded',superseded_by=?
                   WHERE id=?""",
                (replacement_id, evidence_id),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE case_arrangement_evidence_links
                       SET evidence_role='address' WHERE id=?""",
                    (evidence_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM case_arrangement_evidence_links WHERE id=?",
                    (evidence_id,),
                )

    def test_existing_004_database_applies_only_005_without_backfill(self):
        migration_runner.apply_migrations(
            self.database_path,
            self.migration_dir_through_004(),
            applied_by="arrangements-upgrade-test",
            source_commit="arrangements-upgrade-test",
            formal_database_path=None,
        )
        with closing(self.connect(self.database_path)) as connection:
            case_id = self.insert_case(connection)
            self.insert_transfer(connection, case_id)
            old_tables = PHASE1_TABLES | (
                PHASE2_TABLES
                - {
                    "transfer_guardianship_arrangements",
                    "transfer_accommodation_arrangements",
                    "case_arrangement_evidence_links",
                }
            )
            before_rows = {
                table: connection.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
                for table in old_tables
            }
            connection.commit()
        first = self.apply()
        second = self.apply()
        self.assertEqual(
            first["applied"],
            ["20260729_005", "20260730_006", "20260730_007", "20260801_008"],
        )
        self.assertEqual(second["applied"], [])
        with closing(self.connect(self.database_path)) as connection:
            after_rows = {
                table: connection.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
                for table in old_tables
            }
            new_counts = {
                table: connection.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
                for table in (
                    "transfer_guardianship_arrangements",
                    "transfer_accommodation_arrangements",
                    "case_arrangement_evidence_links",
                )
            }
        self.assertEqual(after_rows, before_rows)
        self.assertEqual(set(new_counts.values()), {0})

    def test_005_failure_rolls_back_schema_and_registry(self):
        migration_runner.apply_migrations(
            self.database_path,
            self.migration_dir_through_004(),
            applied_by="arrangements-rollback-test",
            source_commit="arrangements-rollback-test",
            formal_database_path=None,
        )
        broken_dir = self.root / "broken_005"
        checks = broken_dir / "checks"
        checks.mkdir(parents=True)
        for source in (
            PHASE1_MIGRATION_PATH,
            MIGRATION_PATH,
            TITLE_MIGRATION_PATH,
            PROOF_MIGRATION_PATH,
            ARRANGEMENTS_MIGRATION_PATH,
        ):
            shutil.copy2(source, broken_dir / source.name)
        for source in (
            PHASE1_FORWARD_CHECK_PATH,
            FORWARD_CHECK_PATH,
            TITLE_FORWARD_CHECK_PATH,
            PROOF_FORWARD_CHECK_PATH,
            ARRANGEMENTS_FORWARD_CHECK_PATH,
        ):
            shutil.copy2(source, checks / source.name)
        with (broken_dir / ARRANGEMENTS_MIGRATION_PATH.name).open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(
                "\nINSERT INTO table_that_does_not_exist(id) VALUES(1);\n"
            )
        with self.assertRaises(migration_runner.MigrationExecutionError):
            self.apply(migrations_dir=broken_dir)
        with closing(self.connect(self.database_path)) as connection:
            self.assertNotIn(
                "transfer_guardianship_arrangements",
                self.table_names(connection),
            )
            confirmation_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info('case_arrangement_confirmations')"
                )
            }
            versions = [
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
        self.assertNotIn("guardianship_arrangement_id", confirmation_columns)
        self.assertEqual(
            versions,
            [
                "20260728_001",
                "20260729_002",
                "20260729_003",
                "20260729_004",
            ],
        )

    @unittest.skipUnless(
        T0_BACKUP_PATH.is_file(),
        "historical T0 backup artifact is not present in this worktree",
    )
    def test_t0_backup_hash_is_unchanged(self):
        self.assertTrue(T0_BACKUP_PATH.is_file())
        self.assertEqual(sha256(T0_BACKUP_PATH), T0_BACKUP_SHA256)

    def test_t2_domain_modules_exist(self):
        self.assertIsNotNone(transfer_case_errors)
        self.assertIsNotNone(transfer_case_repository)
        self.assertIsNotNone(transfer_case_service)

    def test_final_special_rebuilds_use_distinct_fresh_verified_backups(self):
        label = "distinct-final-rebuild-backups"
        apply_all_test_migrations_with_rebuild_backup(
            self.database_path,
            self.root,
            label=label,
        )
        receipts = []
        for stage in ("035-transfer", "035-visa"):
            backup = self.root / f"{label}-before-{stage}.db"
            manifest = backup.with_suffix(".manifest.json")
            self.assertTrue(backup.is_file())
            self.assertTrue(manifest.is_file())
            receipt = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(sha256(backup), receipt["backup_sha256"])
            self.assertEqual("ok", receipt["quick_check"])
            self.assertEqual("ok", receipt["integrity_check"])
            receipts.append(receipt)
        self.assertNotEqual(
            receipts[0]["source_database_sha256"],
            receipts[1]["source_database_sha256"],
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                "20260910_040",
                connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0],
            )

    def test_t3_transfer_views_module_exists(self):
        self.assertIsNotNone(transfer_case_views)


@unittest.skipIf(
    transfer_case_service is None,
    "Transfer repository/service modules are not implemented yet",
)
class TransferCaseRepositoryServiceTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "fixture.db"
        TransferPhase2MigrationTests._create_legacy_fixture(
            self, self.database_path
        )
        apply_all_test_migrations_with_rebuild_backup(
            self.database_path,
            self.root,
            label="repository-service",
            before_transfer_cutover=getattr(
                self, "before_transfer_cutover", None
            ),
        )
        self.service = transfer_case_service.TransferCaseService(
            self.database_path,
            timeout=2.0,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_record_candidates_use_business_date_before_created_at_and_id(self):
        picker = importlib.import_module("record_picker_and_uploader")
        with closing(self.connect(self.database_path)) as connection:
            for table, definitions in (
                ("offers", (
                    "offer_date TEXT", "uploaded_at TEXT", "created_at TEXT",
                    "offer_status TEXT", "school_name TEXT",
                )),
                ("coe_oshc", (
                    "coe_start_date TEXT", "oshc_expiry_date TEXT",
                    "created_at TEXT", "school_name TEXT",
                )),
                ("visas", (
                    "visa_expiry_date TEXT", "created_at TEXT", "visa_status TEXT",
                )),
                ("payments", (
                    "paid_date TEXT", "created_at TEXT", "receipt_file_path TEXT",
                )),
            ):
                existing = {
                    row[1] for row in connection.execute(
                        f"PRAGMA table_info('{table}')"
                    )
                }
                for definition in definitions:
                    if definition.split()[0] not in existing:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
            connection.execute(
                "UPDATE offers SET offer_date='2026-01-01',created_at='2026-08-01',offer_status='received',school_name='Older' WHERE id=1001"
            )
            connection.execute(
                "UPDATE offers SET offer_date='2026-07-01',created_at='2026-01-01',offer_status='received',school_name='Latest' WHERE id=1000"
            )
            connection.execute(
                "UPDATE coe_oshc SET coe_start_date='2026-01-01',oshc_expiry_date='2026-02-01',created_at='2026-08-01' WHERE id IN (1101,1301)"
            )
            connection.execute(
                "UPDATE coe_oshc SET coe_start_date='2026-07-01',oshc_expiry_date='2027-02-01',created_at='2026-01-01' WHERE id IN (1100,1300)"
            )
            connection.execute(
                "UPDATE visas SET visa_expiry_date='2026-02-01',created_at='2026-08-01',visa_status='active' WHERE id=1401"
            )
            connection.execute(
                "UPDATE visas SET visa_expiry_date='2027-02-01',created_at='2026-01-01',visa_status='active' WHERE id=1400"
            )
            connection.execute(
                "UPDATE payments SET paid_date='2026-01-01',created_at='2026-08-01',receipt_file_path='newer-id.pdf' WHERE id=1501"
            )
            connection.execute(
                "UPDATE payments SET paid_date='2026-07-01',created_at='2026-01-01',receipt_file_path='latest-business.pdf' WHERE id=1500"
            )
            connection.commit()
        expected = {
            "offer": 1000,
            "coe": 1100,
            "oshc": 1300,
            "visa": 1400,
            "payment": 1500,
        }
        for record_type, record_id in expected.items():
            with self.subTest(record_type=record_type):
                candidates = picker.list_record_candidates(
                    self.database_path, "student-1", record_type
                )
                self.assertEqual(candidates[0].record_id, record_id)

    @staticmethod
    def connect(path):
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def create_case(self, **overrides):
        activate = overrides.pop("_activate", True)
        values = {
            "student_id": "student-1",
            "actor_user_id": 1,
            "owner_user_id": 2,
            "current_school_name_snapshot": "Current School",
            "current_school_id": None,
            "current_city_key": "melbourne",
            "current_school_sector": "public",
        }
        values.update(overrides)
        draft = self.service.create_transfer_case(**values)
        if not activate:
            return draft
        return lifecycle_support.activate_transfer(
            self.service,
            draft,
            actor_user_id=values["actor_user_id"],
        )

    def counts(self, *tables):
        with closing(self.connect(self.database_path)) as connection:
            return {
                table: connection.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
                for table in tables
            }

    def end_case(self, case_id, status="cancelled"):
        if status == "cancelled":
            return self.service.cancel_transfer_case(
                case_id,
                actor_user_id=1,
                reason="test fixture cancellation",
                idempotency_key=f"test-cancel-transfer-{case_id}",
            )
        lifecycle = lifecycle_support.lifecycle_service(self.database_path)
        if status == "failed":
            result = self.service.fail_transfer_case(
                case_id,
                actor_user_id=1,
                reason="test fixture failure",
                idempotency_key=f"test-fail-transfer-{case_id}",
            )
            lifecycle.close_lifecycle(
                case_id=case_id,
                outcome="cancelled",
                actor_user_id=1,
                idempotency_key=f"test-close-failed-transfer-{case_id}",
            )
            return result
        if status == "completed":
            result = lifecycle.complete_operational_case(
                case_id=case_id,
                actor_user_id=1,
                idempotency_key=f"test-complete-transfer-{case_id}",
            )
            lifecycle.close_lifecycle(
                case_id=case_id,
                outcome="completed",
                actor_user_id=1,
                idempotency_key=f"test-close-transfer-{case_id}",
            )
            return result
        raise AssertionError(f"unsupported fixture terminal status: {status}")

    def test_modules_expose_safe_errors_and_connection_scoped_repository(self):
        expected_codes = {
            "transfer_case_not_found",
            "transfer_case_schema_not_ready",
            "transfer_case_invalid_workflow_state",
            "transfer_case_invalid_transition",
            "transfer_case_rule_input_invalid",
            "transfer_case_student_not_found",
            "transfer_case_user_not_found",
            "transfer_case_owner_not_found",
            "transfer_case_active_conflict",
            "transfer_case_database_busy",
            "transfer_case_integrity_error",
            "transfer_case_student_case_conflict",
            "transfer_case_access_denied",
            "transfer_case_title_invalid",
            "transfer_case_field_not_editable",
            "transfer_school_application_not_found",
            "transfer_school_application_invalid_input",
            "transfer_school_application_invalid_status",
            "transfer_school_application_invalid_transition",
            "transfer_school_application_order_conflict",
            "transfer_school_application_final_conflict",
            "transfer_school_application_final_required",
            "transfer_school_application_final_locked",
            "transfer_school_application_case_paused",
            "transfer_school_application_case_terminal",
            "transfer_school_application_cross_case",
            "transfer_school_application_school_not_found",
            "transfer_material_not_found",
            "transfer_material_invalid_input",
            "transfer_material_duplicate_key",
            "transfer_material_invalid_status",
            "transfer_material_invalid_transition",
            "transfer_material_document_not_found",
            "transfer_material_document_student_mismatch",
            "transfer_material_confirmation_requires_document",
            "transfer_material_already_initialized",
            "transfer_material_case_paused",
            "transfer_material_case_terminal",
            "transfer_material_cross_case",
            "transfer_admission_record_not_found",
            "transfer_admission_record_student_mismatch",
            "transfer_admission_record_school_mismatch",
            "transfer_admission_record_invalid_type",
            "transfer_payment_requirement_not_found",
            "transfer_payment_requirement_invalid",
            "transfer_payment_requirement_reason_required",
            "transfer_payment_proof_not_found",
            "transfer_payment_proof_invalid",
            "transfer_payment_proof_already_used",
            "transfer_payment_proof_required",
            "transfer_payment_proof_detach_paid",
            "transfer_completion_document_rule_error",
            "transfer_workflow_prerequisite",
            "relationship_needs_repair",
            "missing_current_enrolment",
            "multiple_current_enrolments",
            "missing_upcoming_enrolment",
            "multiple_upcoming_enrolments",
            "enrolment_student_mismatch",
            "enrolment_role_invalid",
            "final_school_mismatch",
            "final_application_terminal",
            "readiness_blocked",
            "completion_history_invalid",
            "completion_not_ready",
            "completion_already_completed",
            "completion_idempotency_conflict",
            "completion_relationship_changed",
            "completion_school_transition_failed",
            "undo_not_allowed",
            "undo_already_applied",
            "undo_relationship_changed",
            "undo_active_case_conflict",
            "pause_not_allowed",
            "resume_not_allowed",
            "cancel_not_allowed",
            "fail_not_allowed",
            "archive_not_allowed",
            "unarchive_not_allowed",
            "status_idempotency_conflict",
        }
        self.assertEqual(
            set(transfer_case_errors.ERROR_TYPES_BY_CODE),
            expected_codes,
        )
        source = Path(transfer_case_repository.__file__).read_text(
            encoding="utf-8"
        )
        self.assertNotIn("sqlite3.connect", source)
        self.assertNotIn(".commit(", source)
        self.assertNotIn("aaie_crm.db", source)
        with closing(self.connect(self.database_path)) as connection:
            self.assertTrue(
                transfer_case_repository.schema_ready(connection)
            )
        empty_path = self.root / "empty.db"
        sqlite3.connect(empty_path).close()
        with self.assertRaises(
            transfer_case_errors.TransferCaseSchemaNotReadyError
        ):
            transfer_case_service.TransferCaseService(
                empty_path
            ).get_transfer_case(1)

    def test_create_is_one_transaction_and_returns_active_lifecycle_dto(self):
        dto = self.create_case(_activate=False)
        self.assertTrue(hasattr(dto, "__dataclass_fields__"))
        self.assertEqual(dto.student_id, "student-1")
        self.assertEqual(dto.case_type, "transfer")
        self.assertEqual(dto.sequence_no, 1)
        self.assertEqual(dto.status, "preparing")
        self.assertEqual(dto.workflow_state, "preparing")
        self.assertEqual(dto.active_slot, 1)
        self.assertEqual(dto.owner_user_id, 2)
        self.assertEqual(dto.current_school_name_snapshot, "Current School")
        with closing(self.connect(self.database_path)) as connection:
            case_row = connection.execute(
                "SELECT * FROM student_cases WHERE id=?", (dto.case_id,)
            ).fetchone()
            transfer_row = connection.execute(
                "SELECT * FROM transfer_cases WHERE case_id=?", (dto.case_id,)
            ).fetchone()
            owner = connection.execute(
                """SELECT * FROM student_case_assignments
                   WHERE case_id=? AND role_type='owner'
                     AND effective_to IS NULL""",
                (dto.case_id,),
            ).fetchone()
            events = [
                dict(row)
                for row in connection.execute(
                    """SELECT event_type,actor_user_id,metadata_json
                       FROM case_events
                       WHERE case_id=? ORDER BY id""",
                    (dto.case_id,),
                )
            ]
        self.assertEqual(case_row["case_type"], "transfer")
        self.assertEqual(transfer_row["rule_status"], "pending_confirmation")
        self.assertEqual(owner["user_id"], 2)
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "case_created",
                "assignment_added",
                "transfer_case_created",
                "lifecycle_started",
            ],
        )
        self.assertEqual({event["actor_user_id"] for event in events}, {1})
        for event in events[:3]:
            metadata = json.loads(event["metadata_json"])
            self.assertEqual(metadata["sequence_no"], 1)
            self.assertEqual(metadata["workflow_state"], "preparing")
            self.assertEqual(metadata["rule_version"], "transfer-phase2-v1")
        lifecycle_metadata = json.loads(events[-1]["metadata_json"])
        self.assertEqual(lifecycle_metadata["lifecycle_type"], "transfer")
        child_counts = self.counts(
            "transfer_school_applications",
            "case_record_links",
            "case_material_items",
            "case_payment_requirements",
            "case_arrangement_confirmations",
            "case_completion_confirmations",
            "student_school_enrolments",
        )
        self.assertEqual(set(child_counts.values()), {0})

    def test_owner_defaults_to_actor_and_type_sequences_are_independent(self):
        first = self.create_case(owner_user_id=None)
        self.assertEqual(first.owner_user_id, 1)
        self.end_case(first.case_id)
        second = self.create_case()
        self.assertEqual(second.sequence_no, 2)
        with closing(self.connect(self.database_path)) as connection:
            import student_case_repository

            next_progression = student_case_repository.allocate_sequence(
                connection,
                student_id="student-1",
                case_type="progression",
                updated_at="2026-07-29T12:00:00+10:00",
            )
            connection.rollback()
        self.assertEqual(next_progression, 1)

    def test_create_validates_identity_and_active_case_conflicts(self):
        cases = (
            (
                {"student_id": "missing"},
                transfer_case_errors.TransferCaseStudentNotFoundError,
            ),
            (
                {"actor_user_id": 99},
                transfer_case_errors.TransferCaseUserNotFoundError,
            ),
            (
                {"owner_user_id": 99},
                transfer_case_errors.TransferCaseOwnerNotFoundError,
            ),
            (
                {"current_school_name_snapshot": "   "},
                transfer_case_errors.TransferCaseRuleInputInvalidError,
            ),
            (
                {"current_city_key": "Melbourne CBD"},
                transfer_case_errors.TransferCaseRuleInputInvalidError,
            ),
            (
                {"current_school_sector": "government"},
                transfer_case_errors.TransferCaseRuleInputInvalidError,
            ),
        )
        for overrides, error_type in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(error_type):
                    self.create_case(**overrides)
        active = self.create_case()
        with self.assertRaises(
            transfer_case_errors.TransferCaseActiveConflictError
        ):
            self.create_case()
        self.service.pause_transfer_case(active.case_id, actor_user_id=1)
        with self.assertRaises(
            transfer_case_errors.TransferCaseActiveConflictError
        ):
            self.create_case()

    def test_progression_active_case_conflicts_and_terminal_releases_slot(self):
        import progression_case_service

        progression = progression_case_service.ProgressionCaseService(
            self.database_path
        )
        progression_case = progression.create_case(
            student_id="student-1", actor_user_id=1,
            actor_permissions={"progression_cases.create"},
            flags=StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            progression_type="bachelor", priority="normal",
            current_school_id=10,
            current_school_name_snapshot="Current School",
            current_course_name="VCE", current_year_level="secondary",
            expected_graduation_date="2026-12-01",
            target_education_stage="Higher Education",
            target_course_type="bachelor", target_institution_id=20,
            target_institution_name_snapshot="Target University",
            target_start_date="2027-02-01", owner_user_id=2, notes=None,
        )
        with self.assertRaises(
            transfer_case_errors.TransferCaseActiveConflictError
        ):
            self.create_case()
        lifecycle_support.lifecycle_service(
            self.database_path
        ).cancel_lifecycle(
            case_id=progression_case.case_id,
            actor_user_id=1,
            reason="test terminal release",
            idempotency_key="test-cancel-progression-active",
        )
        self.assertEqual(self.create_case().sequence_no, 1)

    def test_create_rolls_back_sequence_parent_child_owner_and_events(self):
        failure_points = (
            (
                "student_case_sequences",
                "BEFORE INSERT",
                "SELECT RAISE(ABORT,'sequence failure');",
            ),
            (
                "transfer_cases",
                "BEFORE INSERT",
                "SELECT RAISE(ABORT,'transfer failure');",
            ),
            (
                "case_events",
                "BEFORE INSERT",
                "SELECT RAISE(ABORT,'event failure');",
            ),
        )
        for index, (table, timing, body) in enumerate(failure_points):
            with self.subTest(table=table):
                database_path = self.root / f"rollback-{index}.db"
                TransferPhase2MigrationTests._create_legacy_fixture(
                    self, database_path
                )
                apply_all_test_migrations_with_rebuild_backup(
                    database_path,
                    self.root,
                    label=f"rollback-{index}",
                )
                with closing(self.connect(database_path)) as connection:
                    connection.execute(
                        f"""CREATE TRIGGER injected_failure
                            {timing} ON {table} BEGIN {body} END"""
                    )
                    connection.commit()
                service = transfer_case_service.TransferCaseService(
                    database_path
                )
                with self.assertRaises(
                    transfer_case_errors.TransferCaseIntegrityError
                ):
                    service.create_transfer_case(
                        student_id="student-1",
                        actor_user_id=1,
                        current_school_name_snapshot="School",
                    )
                with closing(self.connect(database_path)) as connection:
                    counts = [
                        connection.execute(
                            f'SELECT count(*) FROM "{name}"'
                        ).fetchone()[0]
                        for name in (
                            "student_case_sequences",
                            "student_cases",
                            "transfer_cases",
                            "student_case_assignments",
                            "case_events",
                        )
                    ]
                self.assertEqual(counts, [0, 0, 0, 0, 0])

    def test_workflow_happy_path_pause_resume_noop_and_illegal_edges(self):
        dto = self.create_case()
        path = [
            "school_search",
            "target_confirmed",
            "application_submitted",
            "waiting_offer",
            "offer_received",
            "waiting_payment",
            "payment_satisfied",
            "waiting_completion_document",
            "ready_to_complete",
        ]
        edit_arguments = {
            "actor_user_id": 1,
            "actor_permissions": {"student_cases.edit"},
            "flags": StudentCaseFeatureFlags(
                read_enabled=True,
                transfer_write_enabled=True,
            ),
        }
        application = None
        requirement = None
        for state in path:
            if state == "target_confirmed":
                application = self.service.add_transfer_school_application(
                    dto.case_id,
                    school_id=None,
                    school_name_snapshot="Target School",
                    institution_group_id=None,
                    city_key="melbourne",
                    campus_key=None,
                    school_sector="private",
                    expected_start_date="2027-02-01",
                    preference_order=None,
                    application_status="preparing",
                    notes=None,
                    actor_user_id=1,
                    actor_permissions={"student_cases.edit"},
                    flags=StudentCaseFeatureFlags(
                        read_enabled=True,
                        transfer_write_enabled=True,
                    ),
                )
                self.service.set_final_transfer_school_application(
                    dto.case_id,
                    application.application_id,
                    actor_user_id=1,
                    actor_permissions={"student_cases.edit"},
                    flags=StudentCaseFeatureFlags(
                        read_enabled=True,
                        transfer_write_enabled=True,
                    ),
                )
            if state == "offer_received":
                with closing(self.connect(self.database_path)) as connection:
                    offer_columns = {
                        str(row[1])
                        for row in connection.execute("PRAGMA table_info(offers)")
                    }
                    if "school_name" not in offer_columns:
                        connection.execute(
                            "ALTER TABLE offers ADD COLUMN school_name TEXT"
                        )
                    connection.execute(
                        "UPDATE offers SET school_name='Target School' WHERE id=1000"
                    )
                    connection.commit()
                self.service.attach_transfer_offer(
                    dto.case_id,
                    application.application_id,
                    1000,
                    manual_school_confirmation=True,
                    confirmation_reason="Legacy fixture has no school name",
                    **edit_arguments,
                )
            if state == "waiting_payment":
                requirement = (
                    self.service.create_transfer_payment_requirement(
                        dto.case_id,
                        application.application_id,
                        "deposit",
                        source_offer_id=1000,
                        extracted_amount=1000,
                        currency="AUD",
                        **edit_arguments,
                    )
                )
            if state == "payment_satisfied":
                self.service.attach_transfer_payment_proof(
                    dto.case_id,
                    application.application_id,
                    requirement.requirement_id,
                    record_type="payment",
                    record_id=1500,
                    **edit_arguments,
                )
                self.service.change_transfer_payment_status(
                    dto.case_id,
                    requirement.requirement_id,
                    "paid",
                    reason=None,
                    **edit_arguments,
                )
            if state == "ready_to_complete":
                with closing(self.connect(self.database_path)) as connection:
                    coe_columns = {
                        str(row[1])
                        for row in connection.execute("PRAGMA table_info(coe_oshc)")
                    }
                    if "school_name" not in coe_columns:
                        connection.execute(
                            "ALTER TABLE coe_oshc ADD COLUMN school_name TEXT"
                        )
                    connection.execute(
                        "UPDATE coe_oshc SET school_name='Target School' WHERE id=1100"
                    )
                    connection.commit()
                self.service.attach_transfer_coe(
                    dto.case_id,
                    application.application_id,
                    1100,
                    manual_school_confirmation=True,
                    confirmation_reason="Legacy fixture has no school name",
                    **edit_arguments,
                )
                with closing(self.connect(self.database_path)) as connection:
                    student_columns = {
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info('students')"
                        )
                    }
                    if "date_of_birth" not in student_columns:
                        connection.execute(
                            "ALTER TABLE students ADD COLUMN date_of_birth TEXT"
                        )
                    connection.execute(
                        """UPDATE students SET date_of_birth='2010-02-01'
                           WHERE student_id='student-1'"""
                    )
                    connection.commit()
                arrangements = (
                    transfer_arrangement_service.TransferArrangementService(
                        self.database_path
                    )
                )
                arrangements.create_transfer_guardianship_arrangement(
                    dto.case_id,
                    arrangement_mode="parent_guardian",
                    guardian_person_name="Parent",
                    guardian_organisation_name=None,
                    relationship_to_student="parent",
                    provider_name=None,
                    start_date=None,
                    end_date=None,
                    source_type="staff_entered",
                    arrangement_status="confirmed",
                    notes=None,
                    **edit_arguments,
                )
                for arrangement_type in (
                    "visa",
                    "oshc",
                    "accommodation",
                ):
                    getattr(
                        arrangements,
                        f"mark_transfer_{arrangement_type}_not_applicable",
                    )(
                        dto.case_id,
                        requirement_reason=f"{arrangement_type} reviewed",
                        **edit_arguments,
                    )
            dto = self.service.transition_workflow(
                dto.case_id, state, actor_user_id=1
            )
            self.assertEqual(dto.workflow_state, state)
        before = self.counts("case_events")["case_events"]
        unchanged = self.service.transition_workflow(
            dto.case_id, "ready_to_complete", actor_user_id=1
        )
        self.assertEqual(unchanged, dto)
        self.assertEqual(self.counts("case_events")["case_events"], before)
        paused = self.service.pause_transfer_case(
            dto.case_id, actor_user_id=1
        )
        self.assertEqual((paused.workflow_state, paused.status), ("paused", "paused"))
        resumed = self.service.resume_transfer_case(
            dto.case_id,
            "ready_to_complete",
            actor_user_id=1,
        )
        self.assertEqual(
            (resumed.workflow_state, resumed.status),
            ("ready_to_complete", "in_progress"),
        )
        with closing(self.connect(self.database_path)) as connection:
            workflow_events = [
                dict(row)
                for row in connection.execute(
                    """SELECT event_type,old_value_json,new_value_json,
                              metadata_json
                       FROM case_events
                       WHERE case_id=?
                         AND event_type='transfer_workflow_state_changed'
                       ORDER BY id""",
                    (dto.case_id,),
                )
            ]
        self.assertEqual(len(workflow_events), len(path) + 2)
        self.assertEqual(
            json.loads(workflow_events[0]["old_value_json"])[
                "workflow_state"
            ],
            "preparing",
        )
        self.assertEqual(
            json.loads(workflow_events[-1]["new_value_json"])[
                "workflow_state"
            ],
            "ready_to_complete",
        )
        with self.assertRaises(
            transfer_case_errors.TransferCaseInvalidTransitionError
        ):
            self.service.transition_workflow(
                dto.case_id, "draft", actor_user_id=1
            )
        with self.assertRaises(
            transfer_case_errors.TransferCaseInvalidWorkflowStateError
        ):
            self.service.transition_workflow(
                dto.case_id, "completed", actor_user_id=1
            )

    def test_workflow_event_failure_rolls_back_both_states(self):
        dto = self.create_case()
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER fail_workflow_event
                   BEFORE INSERT ON case_events
                   WHEN NEW.event_type='transfer_workflow_state_changed'
                   BEGIN SELECT RAISE(ABORT,'event failure'); END"""
            )
            connection.commit()
        with self.assertRaises(
            transfer_case_errors.TransferCaseIntegrityError
        ):
            self.service.transition_workflow(
                dto.case_id, "school_search", actor_user_id=1
            )
        current = self.service.get_transfer_case(dto.case_id)
        self.assertEqual(
            (current.workflow_state, current.status),
            ("preparing", "preparing"),
        )

    def test_terminal_or_archived_parent_rejects_workflow_updates(self):
        for index, status in enumerate(("completed", "cancelled", "failed")):
            with self.subTest(status=status):
                dto = self.create_case(student_id="student-1")
                self.end_case(dto.case_id, status=status)
                if status == "completed":
                    with closing(self.connect(self.database_path)) as connection:
                        connection.execute(
                            """UPDATE student_cases
                               SET status='archived', archived_at=
                                   '2026-07-29T13:00:00+10:00'
                               WHERE id=?""",
                            (dto.case_id,),
                        )
                        connection.execute(
                            """UPDATE transfer_cases
                               SET workflow_state='archived' WHERE case_id=?""",
                            (dto.case_id,),
                        )
                        connection.commit()
                with self.assertRaises(
                    transfer_case_errors.TransferCaseInvalidTransitionError
                ):
                    self.service.transition_workflow(
                        dto.case_id, "preparing", actor_user_id=1
                    )

    def test_rule_matrix_pending_validation_noop_and_event(self):
        expected = {
            ("public", "public"): (
                "confirmation_letter",
                "not_applicable",
                "public_to_public",
            ),
            ("public", "private"): (
                "coe",
                "deposit_or_first_payment_required",
                "public_to_private",
            ),
            ("private", "public"): (
                "coe",
                "deposit_or_first_payment_required",
                "private_to_public",
            ),
            ("private", "private"): (
                "coe",
                "deposit_or_first_payment_required",
                "private_to_private",
            ),
        }
        for index, ((current_sector, target_sector), values) in enumerate(
            expected.items()
        ):
            student_id = "student-1" if index % 2 == 0 else "student-2"
            dto = self.create_case(student_id=student_id)
            result = self.service.set_transfer_rule_snapshot(
                dto.case_id,
                current_city_key="melbourne",
                target_city_key=(
                    "melbourne" if index < 2 else "sydney"
                ),
                current_school_sector=current_sector,
                target_school_sector=target_sector,
                actor_user_id=1,
                rule_version="transfer-phase2-v1",
            )
            self.assertEqual(result.rule_status, "determined")
            self.assertEqual(
                (
                    result.required_completion_document,
                    result.tuition_requirement,
                    result.sector_transition,
                ),
                values,
            )
            with closing(self.connect(self.database_path)) as connection:
                rule_event = connection.execute(
                    """SELECT event_type,old_value_json,new_value_json,
                              metadata_json
                       FROM case_events
                       WHERE case_id=? AND event_type='transfer_rule_calculated'
                       ORDER BY id DESC LIMIT 1""",
                    (dto.case_id,),
                ).fetchone()
            self.assertIsNotNone(rule_event)
            self.assertEqual(
                json.loads(rule_event["metadata_json"])["rule_version"],
                "transfer-phase2-v1",
            )
            self.assertEqual(
                json.loads(rule_event["new_value_json"])["rule_status"],
                "determined",
            )
            before = self.counts("case_events")["case_events"]
            repeated = self.service.set_transfer_rule_snapshot(
                dto.case_id,
                current_city_key="melbourne",
                target_city_key=(
                    "melbourne" if index < 2 else "sydney"
                ),
                current_school_sector=current_sector,
                target_school_sector=target_sector,
                actor_user_id=1,
                rule_version="transfer-phase2-v1",
            )
            self.assertEqual(repeated, result)
            self.assertEqual(self.counts("case_events")["case_events"], before)
            self.end_case(dto.case_id)
            self.end_case(dto.case_id)
        dto = self.create_case(student_id="student-1")
        for partial in (
            {"target_city_key": None, "target_school_sector": "private"},
            {"target_city_key": "sydney", "target_school_sector": None},
            {"target_city_key": "sydney", "target_school_sector": "unknown"},
        ):
            with self.subTest(partial=partial):
                result = self.service.set_transfer_rule_snapshot(
                    dto.case_id,
                    current_city_key="melbourne",
                    current_school_sector="public",
                    actor_user_id=1,
                    rule_version="transfer-phase2-v1",
                    **partial,
                )
                self.assertEqual(result.rule_status, "pending_confirmation")
                self.assertEqual(
                    result.required_completion_document,
                    "pending_confirmation",
                )
        for overrides in (
            {"target_city_key": "Sydney CBD"},
            {"target_school_sector": "government"},
            {"rule_version": " "},
        ):
            values = {
                "current_city_key": "melbourne",
                "target_city_key": "sydney",
                "current_school_sector": "public",
                "target_school_sector": "private",
                "actor_user_id": 1,
                "rule_version": "transfer-phase2-v1",
            }
            values.update(overrides)
            with self.assertRaises(
                transfer_case_errors.TransferCaseRuleInputInvalidError
            ):
                self.service.set_transfer_rule_snapshot(dto.case_id, **values)

    def test_rule_event_failure_rolls_back_snapshot(self):
        dto = self.create_case()
        before = self.service.get_rule_snapshot(dto.case_id)
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER fail_rule_event
                   BEFORE INSERT ON case_events
                   WHEN NEW.event_type='transfer_rule_calculated'
                   BEGIN SELECT RAISE(ABORT,'event failure'); END"""
            )
            connection.commit()
        with self.assertRaises(
            transfer_case_errors.TransferCaseIntegrityError
        ):
            self.service.set_transfer_rule_snapshot(
                dto.case_id,
                current_city_key="melbourne",
                target_city_key="sydney",
                current_school_sector="public",
                target_school_sector="private",
                actor_user_id=1,
                rule_version="transfer-phase2-v1",
            )
        self.assertEqual(self.service.get_rule_snapshot(dto.case_id), before)

    def test_get_list_active_counts_and_not_found(self):
        first = self.create_case()
        self.end_case(first.case_id, "completed")
        second = self.create_case()
        self.assertEqual(
            self.service.get_transfer_case(second.case_id), second
        )
        self.assertEqual(
            [item.case_id for item in self.service.list_transfer_cases(
                "student-1"
            )],
            [second.case_id, first.case_id],
        )
        self.assertEqual(
            self.service.get_active_transfer_case("student-1").case_id,
            second.case_id,
        )
        counts = self.service.get_transfer_counts("student-1")
        self.assertEqual((counts.total, counts.successful), (2, 1))
        self.end_case(second.case_id, "cancelled")
        failed = self.create_case()
        self.end_case(failed.case_id, "failed")
        archived_completed = self.create_case()
        self.end_case(archived_completed.case_id, "completed")
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE student_cases
                   SET status='archived',
                       archived_at='2026-07-29T13:00:00+10:00'
                   WHERE id=?""",
                (archived_completed.case_id,),
            )
            connection.execute(
                """UPDATE transfer_cases SET workflow_state='archived'
                   WHERE case_id=?""",
                (archived_completed.case_id,),
            )
            connection.commit()
        counts = self.service.get_transfer_counts("student-1")
        self.assertEqual(
            (counts.total, counts.successful, counts.active),
            (4, 2, 0),
        )
        with self.assertRaises(
            transfer_case_errors.TransferCaseNotFoundError
        ):
            self.service.get_transfer_case(9999)
        self.assertIsNone(
            self.service.get_active_transfer_case("student-2")
        )

    def test_busy_is_safe_and_concurrent_create_allows_one_active_lifecycle(self):
        blocker = self.connect(self.database_path)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            busy_service = transfer_case_service.TransferCaseService(
                self.database_path, timeout=0.01
            )
            with self.assertRaises(
                transfer_case_errors.TransferCaseDatabaseBusyError
            ) as caught:
                busy_service.create_transfer_case(
                    student_id="student-1",
                    actor_user_id=1,
                    current_school_name_snapshot="School",
                )
            self.assertNotIn(str(self.database_path), str(caught.exception))
        finally:
            blocker.rollback()
            blocker.close()

        barrier = threading.Barrier(2)
        outcomes = []

        def worker():
            service = transfer_case_service.TransferCaseService(
                self.database_path, timeout=5.0
            )
            barrier.wait()
            try:
                outcomes.append(
                    service.create_transfer_case(
                        student_id="student-1",
                        actor_user_id=1,
                        current_school_name_snapshot="School",
                    )
                )
            except Exception as error:  # test captures the public error type
                outcomes.append(error)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(
            sum(
                isinstance(item, transfer_case_service.TransferCaseDTO)
                for item in outcomes
            ),
            1,
        )
        self.assertEqual(
            sum(
                isinstance(
                    item,
                    transfer_case_errors.TransferCaseActiveConflictError,
                )
                for item in outcomes
            ),
            1,
        )


@unittest.skipIf(
    transfer_case_service is None,
    "Transfer repository/service modules are not implemented yet",
)
class TransferCaseT3ServiceTests(TransferCaseRepositoryServiceTests):
    def enabled_flags(self):
        return StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
        )

    def create_internal(self, **overrides):
        activate = overrides.pop("_activate", True)
        values = {
            "student_id": "student-1",
            "actor_user_id": 1,
            "actor_permissions": {"student_cases.create"},
            "flags": self.enabled_flags(),
            "owner_user_id": 2,
            "title": None,
            "current_school_name_snapshot": "Current School",
            "current_school_id": None,
            "current_city_key": "melbourne",
            "current_school_sector": "public",
            "initial_note": None,
        }
        values.update(overrides)
        draft = self.service.create_internal_transfer(**values)
        if not activate:
            return draft
        return lifecycle_support.activate_transfer(
            self.service, draft, actor_user_id=values["actor_user_id"]
        )

    def test_create_accepts_nullable_or_valid_title_and_records_initial_note_only_in_event(
        self,
    ):
        first = self.create_internal()
        self.assertIsNone(first.title)
        self.end_case(first.case_id)

        second = self.create_internal(
            title="  Year 10 transfer  ",
            initial_note="  Family prefers Term 3.  ",
        )
        self.assertEqual(second.title, "Year 10 transfer")
        with closing(self.connect(self.database_path)) as connection:
            projection = dict(
                connection.execute(
                    "SELECT * FROM transfer_cases WHERE case_id=?",
                    (second.case_id,),
                ).fetchone()
            )
            event = connection.execute(
                """SELECT new_value_json,metadata_json
                   FROM case_events
                   WHERE case_id=? AND event_type='transfer_case_created'""",
                (second.case_id,),
            ).fetchone()
        self.assertEqual(projection["title"], "Year 10 transfer")
        self.assertNotIn("initial_note", projection)
        self.assertEqual(
            json.loads(event["metadata_json"])["initial_note"],
            "Family prefers Term 3.",
        )

    def test_create_rejects_invalid_title_and_rolls_back_title_case_and_sequence(
        self,
    ):
        for invalid in ("", "   ", "x" * 201):
            with self.subTest(title=repr(invalid[:10])):
                with self.assertRaises(
                    transfer_case_errors.TransferCaseTitleInvalidError
                ):
                    self.create_internal(title=invalid)
                self.assertEqual(
                    self.counts(
                        "student_case_sequences",
                        "student_cases",
                        "transfer_cases",
                        "case_events",
                    ),
                    {
                        "student_case_sequences": 0,
                        "student_cases": 0,
                        "transfer_cases": 0,
                        "case_events": 0,
                    },
                )

    def test_create_event_failure_rolls_back_title_and_entire_case(self):
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER fail_transfer_created_event
                   BEFORE INSERT ON case_events
                   WHEN NEW.event_type='transfer_case_created'
                   BEGIN SELECT RAISE(ABORT,'event failure'); END"""
            )
            connection.commit()
        with self.assertRaises(
            transfer_case_errors.TransferCaseIntegrityError
        ):
            self.create_internal(
                title="Rollback me",
                initial_note="Must not survive",
            )
        self.assertEqual(
            self.counts(
                "student_case_sequences",
                "student_cases",
                "transfer_cases",
                "student_case_assignments",
                "case_events",
            ),
            {
                "student_case_sequences": 0,
                "student_cases": 0,
                "transfer_cases": 0,
                "student_case_assignments": 0,
                "case_events": 0,
            },
        )

    def test_basic_edit_whitelist_title_noop_event_and_rule_recalculation(self):
        created = self.create_internal(title="Original")
        before_events = self.counts("case_events")["case_events"]
        unchanged = self.service.update_transfer_case(
            created.case_id,
            changes={"title": "Original"},
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.enabled_flags(),
        )
        self.assertEqual(unchanged, created)
        self.assertEqual(
            self.counts("case_events")["case_events"], before_events
        )

        determined = self.service.set_transfer_rule_snapshot(
            created.case_id,
            current_city_key="melbourne",
            target_city_key="melbourne",
            current_school_sector="public",
            target_school_sector="private",
            actor_user_id=1,
            rule_version="transfer-phase2-v1",
        )
        self.assertEqual(
            (
                determined.rule_status,
                determined.same_city_result,
                determined.sector_transition,
            ),
            ("determined", "same_city", "public_to_private"),
        )
        updated = self.service.update_transfer_case(
            created.case_id,
            changes={
                "title": "Updated",
                "current_school_name_snapshot": "Updated School",
                "current_school_id": 10,
                "current_city_key": "sydney",
                "current_school_sector": "private",
            },
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.enabled_flags(),
        )
        self.assertEqual(updated.title, "Updated")
        self.assertEqual(
            (
                updated.current_school_name_snapshot,
                updated.current_school_id,
                updated.current_city_key,
                updated.current_school_sector,
            ),
            ("Updated School", 10, "sydney", "private"),
        )
        self.assertEqual(
            (
                updated.rule_status,
                updated.same_city_result,
                updated.sector_transition,
                updated.required_completion_document,
                updated.tuition_requirement,
            ),
            (
                "determined",
                "cross_city",
                "private_to_private",
                "coe",
                "deposit_or_first_payment_required",
            ),
        )
        with closing(self.connect(self.database_path)) as connection:
            event = connection.execute(
                """SELECT old_value_json,new_value_json
                   FROM case_events
                   WHERE case_id=? AND event_type='transfer_case_updated'
                   ORDER BY id DESC LIMIT 1""",
                (created.case_id,),
            ).fetchone()
            enrolments = connection.execute(
                "SELECT count(*) FROM student_school_enrolments"
            ).fetchone()[0]
            student = connection.execute(
                "SELECT * FROM students WHERE student_id='student-1'"
            ).fetchone()
        old_value = json.loads(event["old_value_json"])
        new_value = json.loads(event["new_value_json"])
        self.assertEqual(old_value["title"], "Original")
        self.assertEqual(new_value["title"], "Updated")
        self.assertEqual(new_value["current_city_key"], "sydney")
        self.assertEqual(new_value["same_city_result"], "cross_city")
        self.assertTrue(
            json.loads(event["new_value_json"])["rule_calculated_at"]
        )
        # Formal Case creation now binds the already-confirmed current
        # enrolment. Editing the Case must not create a second enrolment.
        self.assertEqual(enrolments, 1)
        self.assertEqual(student["student_id"], "student-1")
        self.assertTrue(
            all(
                value is None
                for key, value in dict(student).items()
                if key != "student_id"
            )
        )

    def test_initial_note_and_unknown_fields_cannot_be_edited(self):
        created = self.create_internal(initial_note="Original note")
        for changes in (
            {"initial_note": "replacement"},
            {"student_id": "student-2"},
            {"completed_at": "2026-07-29"},
            {"final_application_id": 1},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(
                    transfer_case_errors.TransferCaseFieldNotEditableError
                ):
                    self.service.update_transfer_case(
                        created.case_id,
                        changes=changes,
                        actor_user_id=1,
                        actor_permissions={"student_cases.edit"},
                        flags=self.enabled_flags(),
                    )

    def test_edit_event_failure_rolls_back_projection_rule_and_parent_timestamp(self):
        created = self.create_internal(title="Before")
        with closing(self.connect(self.database_path)) as connection:
            parent_before = connection.execute(
                "SELECT updated_at FROM student_cases WHERE id=?",
                (created.case_id,),
            ).fetchone()[0]
            connection.execute(
                """CREATE TRIGGER fail_transfer_updated_event
                   BEFORE INSERT ON case_events
                   WHEN NEW.event_type='transfer_case_updated'
                   BEGIN SELECT RAISE(ABORT,'event failure'); END"""
            )
            connection.commit()
        with self.assertRaises(
            transfer_case_errors.TransferCaseIntegrityError
        ):
            self.service.update_transfer_case(
                created.case_id,
                changes={
                    "title": "After",
                    "current_city_key": "sydney",
                },
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.enabled_flags(),
            )
        current = self.service.get_transfer_case(created.case_id)
        self.assertEqual((current.title, current.current_city_key), ("Before", "melbourne"))
        with closing(self.connect(self.database_path)) as connection:
            parent_after = connection.execute(
                "SELECT updated_at FROM student_cases WHERE id=?",
                (created.case_id,),
            ).fetchone()[0]
        self.assertEqual(parent_after, parent_before)

    def test_owner_collaborator_and_watcher_history_is_transactional(self):
        created = self.create_internal()
        owner = self.service.set_owner(
            created.case_id,
            user_id=1,
            actor_user_id=1,
            actor_permissions={"student_cases.assign"},
            flags=self.enabled_flags(),
        )
        collaborator = self.service.add_collaborator(
            created.case_id,
            user_id=2,
            actor_user_id=1,
            actor_permissions={"student_cases.assign"},
            flags=self.enabled_flags(),
        )
        watcher = self.service.add_watcher(
            created.case_id,
            user_id=2,
            actor_user_id=1,
            actor_permissions={"student_cases.assign"},
            flags=self.enabled_flags(),
        )
        self.service.remove_collaborator(
            created.case_id,
            user_id=2,
            actor_user_id=1,
            actor_permissions={"student_cases.assign"},
            flags=self.enabled_flags(),
        )
        self.service.remove_watcher(
            created.case_id,
            user_id=2,
            actor_user_id=1,
            actor_permissions={"student_cases.assign"},
            flags=self.enabled_flags(),
        )
        self.assertEqual((owner.role_type, owner.user_id), ("owner", 1))
        self.assertEqual(collaborator.role_type, "collaborator")
        self.assertEqual(watcher.role_type, "watcher")
        with closing(self.connect(self.database_path)) as connection:
            history = [
                dict(row)
                for row in connection.execute(
                    """SELECT user_id,role_type,effective_to
                       FROM student_case_assignments
                       WHERE case_id=? ORDER BY id""",
                    (created.case_id,),
                )
            ]
            events = [
                row[0]
                for row in connection.execute(
                    """SELECT event_type FROM case_events
                       WHERE case_id=? ORDER BY id""",
                    (created.case_id,),
                )
            ]
        self.assertEqual(len(history), 4)
        self.assertIsNotNone(history[0]["effective_to"])
        self.assertIsNone(history[1]["effective_to"])
        self.assertIsNotNone(history[2]["effective_to"])
        self.assertIsNotNone(history[3]["effective_to"])
        self.assertIn("assignment_changed", events)
        self.assertEqual(events.count("assignment_added"), 3)
        self.assertEqual(events.count("assignment_ended"), 2)

    def test_create_edit_assign_and_workflow_require_exact_permissions_and_flags(self):
        denied_flags = StudentCaseFeatureFlags(read_enabled=True)
        attempts = (
            lambda: self.create_internal(
                flags=denied_flags,
            ),
            lambda: self.create_internal(
                actor_permissions={"*"},
            ),
        )
        for attempt in attempts:
            with self.assertRaises(
                transfer_case_errors.TransferCaseAccessDeniedError
            ):
                attempt()

        created = self.create_internal()
        for action in (
            lambda: self.service.update_transfer_case(
                created.case_id,
                changes={"title": "Denied"},
                actor_user_id=1,
                actor_permissions={"student_cases.assign"},
                flags=self.enabled_flags(),
            ),
            lambda: self.service.set_owner(
                created.case_id,
                user_id=1,
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.enabled_flags(),
            ),
            lambda: self.service.transition_transfer(
                created.case_id,
                "preparing",
                actor_user_id=1,
                actor_permissions={"student_cases.assign"},
                flags=self.enabled_flags(),
            ),
        ):
            with self.assertRaises(
                transfer_case_errors.TransferCaseAccessDeniedError
            ):
                action()

        transitioned = self.service.transition_transfer(
            created.case_id,
            "preparing",
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.enabled_flags(),
        )
        self.assertEqual(
            (transitioned.workflow_state, transitioned.status),
            ("preparing", "preparing"),
        )

    def test_assignment_roles_do_not_grant_edit_assign_or_view(self):
        flags = self.enabled_flags()
        wildcard = {"permissions": ["*"]}
        owner = {"permissions": []}
        collaborator = {"permissions": []}
        watcher = {"permissions": []}
        for user in (wildcard, owner, collaborator, watcher):
            self.assertFalse(
                student_case_permissions.can_view_student_cases(user, flags)
            )
            self.assertFalse(
                student_case_permissions.can_edit_student_case(
                    user, flags, "transfer"
                )
            )
            self.assertFalse(
                student_case_permissions.can_assign_student_case(
                    user, flags, "transfer"
                )
            )

    def test_transfer_service_composes_read_write_and_exact_capability(self):
        write_without_read = StudentCaseFeatureFlags(
            transfer_write_enabled=True
        )
        self.assertTrue(
            student_case_permissions.can_edit_student_case(
                {"permissions": ["student_cases.edit"]},
                write_without_read,
                "transfer",
            )
        )
        self.assertTrue(
            student_case_permissions.can_assign_student_case(
                {"permissions": ["student_cases.assign"]},
                write_without_read,
                "transfer",
            )
        )
        created = self.create_internal()
        for action in (
            lambda: self.service.update_transfer_case(
                created.case_id,
                changes={"title": "Denied without READ"},
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=write_without_read,
            ),
            lambda: self.service.set_owner(
                created.case_id,
                user_id=1,
                actor_user_id=1,
                actor_permissions={"student_cases.assign"},
                flags=write_without_read,
            ),
        ):
            with self.assertRaises(
                transfer_case_errors.TransferCaseAccessDeniedError
            ):
                action()
        flags = self.enabled_flags()
        self.assertTrue(
            student_case_permissions.can_edit_student_case(
                {"permissions": ["student_cases.edit"]}, flags, "transfer"
            )
        )
        self.assertTrue(
            student_case_permissions.can_assign_student_case(
                {"permissions": ["student_cases.assign"]}, flags, "transfer"
            )
        )

@unittest.skipIf(
    transfer_case_service is None,
    "Transfer repository/service modules are not implemented yet",
)
class TransferCaseT4SchoolApplicationTests(
    TransferCaseRepositoryServiceTests
):
    def flags(self, write=True):
        return StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=write,
        )

    def create_internal(self, student_id="student-1"):
        draft = self.service.create_internal_transfer(
            student_id=student_id,
            actor_user_id=1,
            actor_permissions={"student_cases.create"},
            flags=self.flags(),
            owner_user_id=2,
            title=None,
            current_school_name_snapshot="Current School",
            current_school_id=None,
            current_city_key="melbourne",
            current_school_sector="public",
            initial_note=None,
        )
        return lifecycle_support.activate_transfer(self.service, draft)

    def add_application(self, case_id, **overrides):
        values = {
            "school_id": None,
            "school_name_snapshot": "Target School",
            "institution_group_id": None,
            "city_key": "melbourne",
            "campus_key": None,
            "school_sector": "private",
            "expected_start_date": "2027-02-01",
            "preference_order": None,
            "application_status": "preparing",
            "notes": "Initial note",
        }
        values.update(overrides)
        request_id = values.pop("request_id", None)
        correlation_id = values.pop("correlation_id", None)
        return self.service.add_transfer_school_application(
            case_id,
            **values,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
            request_id=request_id,
            correlation_id=correlation_id,
        )

    def test_add_list_get_and_middle_insert_keep_stable_dto_and_order(self):
        case = self.create_internal()
        first = self.add_application(
            case.case_id, school_name_snapshot="First"
        )
        third = self.add_application(
            case.case_id, school_name_snapshot="Third"
        )
        second = self.add_application(
            case.case_id,
            school_name_snapshot="Second",
            preference_order=2,
        )
        rows = self.service.list_transfer_school_applications(case.case_id)
        self.assertEqual(
            [(row.school_name_snapshot, row.preference_order) for row in rows],
            [("First", 1), ("Second", 2), ("Third", 3)],
        )
        self.assertEqual(
            self.service.get_transfer_school_application(
                case.case_id, second.application_id
            ),
            second,
        )
        self.assertFalse(isinstance(first, sqlite3.Row))

    def test_add_validates_fields_without_guessing_school_or_city(self):
        case = self.create_internal()
        for changes in (
            {"school_name_snapshot": " "},
            {"school_name_snapshot": "x" * 201},
            {"school_sector": "government"},
            {"expected_start_date": "2027-02-30"},
            {"school_id": 99999},
            {"city_key": "Melbourne"},
        ):
            with self.assertRaises(
                transfer_case_errors.TransferSchoolApplicationInvalidInputError
            ):
                self.add_application(case.case_id, **changes)
        row = self.add_application(
            case.case_id,
            school_name_snapshot="Name resembles School 10",
            school_id=None,
            city_key=None,
        )
        self.assertIsNone(row.school_id)
        self.assertIsNone(row.city_key)

    def test_add_request_id_is_idempotent_and_initial_status_is_bounded(self):
        case = self.create_internal()
        first = self.add_application(
            case.case_id,
            school_name_snapshot="Idempotent School",
            request_id="same-request",
        )
        second = self.add_application(
            case.case_id,
            school_name_snapshot="Changed retry body",
            request_id="same-request",
        )
        self.assertEqual(first, second)
        self.assertEqual(
            self.counts("transfer_school_applications")[
                "transfer_school_applications"
            ],
            1,
        )
        with self.assertRaises(
            transfer_case_errors.TransferSchoolApplicationInvalidStatusError
        ):
            self.add_application(
                case.case_id,
                application_status="accepted",
            )

    def test_reorder_uses_exact_active_set_and_writes_one_event(self):
        case = self.create_internal()
        first = self.add_application(case.case_id, school_name_snapshot="A")
        second = self.add_application(case.case_id, school_name_snapshot="B")
        self.service.reorder_transfer_school_applications(
            case.case_id,
            [second.application_id, first.application_id],
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        rows = self.service.list_transfer_school_applications(case.case_id)
        self.assertEqual(
            [row.application_id for row in rows],
            [second.application_id, first.application_id],
        )
        with closing(self.connect(self.database_path)) as connection:
            count = connection.execute(
                """SELECT count(*) FROM case_events
                   WHERE case_id=?
                     AND event_type='transfer_school_applications_reordered'""",
                (case.case_id,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_status_matrix_noop_terminal_and_event(self):
        case = self.create_internal()
        application = self.add_application(
            case.case_id, application_status="draft"
        )
        changed = self.service.change_transfer_school_application_status(
            case.case_id,
            application.application_id,
            "preparing",
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.assertEqual(changed.application_status, "preparing")
        noop = self.service.change_transfer_school_application_status(
            case.case_id,
            application.application_id,
            "preparing",
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.assertEqual(noop, changed)
        with self.assertRaises(
            transfer_case_errors.TransferSchoolApplicationInvalidTransitionError
        ):
            self.service.change_transfer_school_application_status(
                case.case_id,
                application.application_id,
                "accepted",
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            )

    def test_final_replaces_atomically_syncs_parent_and_recalculates_rule(self):
        case = self.create_internal()
        first = self.add_application(
            case.case_id,
            school_name_snapshot="Public same city",
            school_sector="public",
        )
        second = self.add_application(
            case.case_id,
            school_name_snapshot="Private Sydney",
            city_key="sydney",
            school_sector="private",
        )
        self.service.set_final_transfer_school_application(
            case.case_id,
            first.application_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.service.set_final_transfer_school_application(
            case.case_id,
            second.application_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        current = self.service.get_transfer_case(case.case_id)
        rows = self.service.list_transfer_school_applications(case.case_id)
        self.assertEqual(current.final_application_id, second.application_id)
        self.assertEqual(sum(row.is_final for row in rows), 1)
        self.assertEqual(current.same_city_result, "cross_city")
        self.assertEqual(current.sector_transition, "public_to_private")

    def test_clear_final_and_workflow_final_guards(self):
        case = self.create_internal()
        application = self.add_application(case.case_id)
        for state in ("preparing", "school_search"):
            self.service.transition_transfer(
                case.case_id,
                state,
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            )
        with self.assertRaises(
            transfer_case_errors.TransferSchoolApplicationFinalRequiredError
        ):
            self.service.transition_transfer(
                case.case_id,
                "target_confirmed",
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            )
        self.service.set_final_transfer_school_application(
            case.case_id,
            application.application_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.service.transition_transfer(
            case.case_id,
            "target_confirmed",
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        with self.assertRaises(
            transfer_case_errors.TransferSchoolApplicationFinalLockedError
        ):
            self.service.clear_final_transfer_school_application(
                case.case_id,
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            )

    def test_final_edit_recalculates_nonfinal_edit_does_not(self):
        case = self.create_internal()
        final = self.add_application(case.case_id, school_name_snapshot="Final")
        other = self.add_application(case.case_id, school_name_snapshot="Other")
        self.service.set_final_transfer_school_application(
            case.case_id,
            final.application_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        with closing(self.connect(self.database_path)) as connection:
            before = connection.execute(
                """SELECT count(*) FROM case_events WHERE case_id=?
                   AND event_type='transfer_rule_calculated'""",
                (case.case_id,),
            ).fetchone()[0]
        self.service.edit_transfer_school_application(
            case.case_id,
            other.application_id,
            changes={"city_key": "sydney"},
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.service.edit_transfer_school_application(
            case.case_id,
            final.application_id,
            changes={"city_key": "sydney"},
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        with closing(self.connect(self.database_path)) as connection:
            after = connection.execute(
                """SELECT count(*) FROM case_events WHERE case_id=?
                   AND event_type='transfer_rule_calculated'""",
                (case.case_id,),
            ).fetchone()[0]
        self.assertEqual(after - before, 1)

    def test_paused_terminal_flag_permissions_and_cross_case_are_rejected(self):
        case = self.create_internal()
        application = self.add_application(case.case_id)
        for permissions, flags in (
            ({"student_cases.view"}, self.flags()),
            ({"*"}, self.flags()),
            ({"student_cases.edit"}, self.flags(False)),
        ):
            with self.assertRaises(
                transfer_case_errors.TransferCaseAccessDeniedError
            ):
                self.service.edit_transfer_school_application(
                    case.case_id,
                    application.application_id,
                    changes={"notes": "Denied"},
                    actor_user_id=1,
                    actor_permissions=permissions,
                    flags=flags,
                )
        self.service.pause_transfer_case(case.case_id, actor_user_id=1)
        with self.assertRaises(
            transfer_case_errors.TransferSchoolApplicationCasePausedError
        ):
            self.add_application(case.case_id)

    def test_application_event_failure_rolls_back_business_change(self):
        case = self.create_internal()
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER fail_t4_event BEFORE INSERT ON case_events
                   WHEN NEW.event_type='transfer_school_application_added'
                   BEGIN SELECT RAISE(ABORT, 'forced event failure'); END"""
            )
            connection.commit()
        with self.assertRaises(
            transfer_case_errors.TransferCaseIntegrityError
        ):
            self.add_application(case.case_id)
        self.assertEqual(
            self.counts("transfer_school_applications")[
                "transfer_school_applications"
            ],
            0,
        )

    def test_final_rule_event_failure_rolls_back_final_and_parent_pointer(self):
        case = self.create_internal()
        application = self.add_application(case.case_id)
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER fail_t4_rule_event
                   BEFORE INSERT ON case_events
                   WHEN NEW.event_type='transfer_rule_calculated'
                   BEGIN SELECT RAISE(ABORT, 'forced rule event failure'); END"""
            )
            connection.commit()
        with self.assertRaises(
            transfer_case_errors.TransferCaseIntegrityError
        ):
            self.service.set_final_transfer_school_application(
                case.case_id,
                application.application_id,
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            )
        current = self.service.get_transfer_case(case.case_id)
        application = self.service.get_transfer_school_application(
            case.case_id, application.application_id
        )
        self.assertIsNone(current.final_application_id)
        self.assertFalse(application.is_final)

    def test_concurrent_final_and_preference_writes_remain_consistent(self):
        case = self.create_internal()
        first = self.add_application(case.case_id, school_name_snapshot="A")
        second = self.add_application(case.case_id, school_name_snapshot="B")
        barrier = threading.Barrier(2)
        outcomes = []

        def set_final(application_id):
            try:
                barrier.wait(5)
                outcomes.append(
                    self.service.set_final_transfer_school_application(
                        case.case_id,
                        application_id,
                        actor_user_id=1,
                        actor_permissions={"student_cases.edit"},
                        flags=self.flags(),
                    )
                )
            except Exception as error:
                outcomes.append(error)

        threads = [
            threading.Thread(target=set_final, args=(application_id,))
            for application_id in (
                first.application_id,
                second.application_id,
            )
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(
            sum(
                isinstance(
                    item,
                    transfer_case_service.TransferSchoolApplicationDTO,
                )
                for item in outcomes
            ),
            2,
        )
        rows = self.service.list_transfer_school_applications(case.case_id)
        final_rows = [row for row in rows if row.is_final]
        self.assertEqual(len(final_rows), 1)
        self.assertEqual(
            self.service.get_transfer_case(case.case_id).final_application_id,
            final_rows[0].application_id,
        )

        self.end_case(case.case_id)
        next_case = self.create_internal()
        insert_barrier = threading.Barrier(2)
        inserted = []

        def insert_first(name):
            try:
                insert_barrier.wait(5)
                inserted.append(
                    self.add_application(
                        next_case.case_id,
                        school_name_snapshot=name,
                        preference_order=1,
                    )
                )
            except Exception as error:
                inserted.append(error)

        insert_threads = [
            threading.Thread(target=insert_first, args=(name,))
            for name in ("Concurrent A", "Concurrent B")
        ]
        for thread in insert_threads:
            thread.start()
        for thread in insert_threads:
            thread.join(10)
        self.assertEqual(
            sum(
                isinstance(
                    item,
                    transfer_case_service.TransferSchoolApplicationDTO,
                )
                for item in inserted
            ),
            2,
        )
        self.assertEqual(
            [
                row.preference_order
                for row in self.service.list_transfer_school_applications(
                    next_case.case_id
                )
            ],
            [1, 2],
        )


class TransferCaseT5MaterialTests(TransferCaseRepositoryServiceTests):
    def flags(self, write=True):
        return StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=write,
        )

    def create_internal(self, student_id="student-1"):
        draft = self.service.create_transfer_case(
            student_id=student_id,
            actor_user_id=1,
            owner_user_id=2,
            current_school_name_snapshot="Current School",
            current_city_key="melbourne",
            current_school_sector="public",
        )
        return lifecycle_support.activate_transfer(self.service, draft)

    def initialize(self, case_id):
        return self.service.ensure_transfer_material_template(
            case_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
            request_id="t5-template",
        )

    def test_template_is_complete_idempotent_and_audited(self):
        case = self.create_internal()
        first = self.initialize(case.case_id)
        second = self.initialize(case.case_id)
        self.assertEqual(len(first), 6)
        self.assertEqual(first, second)
        self.assertEqual(
            {item.material_key for item in first},
            {
                "current_school_report",
                "passport",
                "current_visa",
                "current_coe",
                "current_oshc",
                "guardianship_document",
            },
        )
        self.assertEqual({item.source_type for item in first}, {"system_generated"})
        self.assertEqual({item.material_status for item in first}, {"missing"})
        self.assertEqual({item.document_id for item in first}, {None})
        with closing(self.connect(self.database_path)) as connection:
            events = connection.execute(
                """SELECT event_type,metadata_json FROM case_events
                   WHERE case_id=? AND event_type=
                     'transfer_material_template_initialized'""",
                (case.case_id,),
            ).fetchall()
            record_links = connection.execute(
                "SELECT count(*) FROM case_record_links WHERE case_id=?",
                (case.case_id,),
            ).fetchone()[0]
        self.assertEqual(len(events), 1)
        metadata = json.loads(events[0]["metadata_json"])
        self.assertEqual(metadata["template_version"], "transfer-materials-v1")
        self.assertEqual(len(metadata["material_keys"]), 6)
        self.assertEqual(record_links, 0)

    def test_removed_template_material_on_existing_case_is_not_deleted(self):
        case = self.create_internal()
        historical = self.service.add_transfer_material_item(
            case.case_id,
            material_key="address_evidence",
            material_label_snapshot="Address evidence",
            is_required=True,
            initial_status="missing",
            document_id=None,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.initialize(case.case_id)
        material_keys = {
            item.material_key
            for item in self.service.list_transfer_material_items(case.case_id)
        }
        self.assertIn(historical.material_key, material_keys)

    def test_custom_material_validates_key_label_status_and_uniqueness(self):
        case = self.create_internal()
        added = self.service.add_transfer_material_item(
            case.case_id,
            material_key="custom_parent_letter",
            material_label_snapshot="Parent explanation letter",
            is_required=False,
            initial_status="missing",
            document_id=None,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.assertEqual(added.source_type, "staff_added")
        self.assertFalse(added.is_required)
        self.assertTrue(hasattr(added, "__dataclass_fields__"))
        invalid = (
            ({"material_key": "Bad Key"}, "transfer_material_invalid_input"),
            ({"material_label_snapshot": "   "}, "transfer_material_invalid_input"),
            ({"material_label_snapshot": "x" * 201}, "transfer_material_invalid_input"),
            ({"initial_status": "pending"}, "transfer_material_invalid_status"),
        )
        for changes, code in invalid:
            values = {
                "material_key": "another_custom",
                "material_label_snapshot": "Another custom item",
                "is_required": True,
                "initial_status": "missing",
                "document_id": None,
            }
            values.update(changes)
            with self.subTest(changes=changes):
                with self.assertRaises(transfer_case_errors.TransferCaseError) as caught:
                    self.service.add_transfer_material_item(
                        case.case_id,
                        **values,
                        actor_user_id=1,
                        actor_permissions={"student_cases.edit"},
                        flags=self.flags(),
                    )
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(
            transfer_case_errors.TransferMaterialDuplicateKeyError
        ):
            self.service.add_transfer_material_item(
                case.case_id,
                material_key="custom_parent_letter",
                material_label_snapshot="Duplicate",
                is_required=True,
                initial_status="missing",
                document_id=None,
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            )

    def test_edit_and_status_changes_preserve_identity_and_confirmation_rules(self):
        case = self.create_internal()
        material = self.initialize(case.case_id)[0]
        edited = self.service.edit_transfer_material_item(
            case.case_id,
            material.material_id,
            material_label_snapshot="Updated report label",
            is_required=False,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.assertEqual(edited.material_key, material.material_key)
        self.assertEqual(edited.source_type, "system_generated")
        self.assertEqual(edited.material_label_snapshot, "Updated report label")
        self.assertFalse(edited.is_required)
        available = self.service.change_transfer_material_status(
            case.case_id,
            material.material_id,
            "available",
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.assertEqual(available.material_status, "available")
        with self.assertRaises(
            transfer_case_errors.TransferMaterialConfirmationRequiresDocumentError
        ):
            self.service.change_transfer_material_status(
                case.case_id,
                material.material_id,
                "confirmed",
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            )
        not_applicable = self.service.change_transfer_material_status(
            case.case_id,
            material.material_id,
            "not_applicable",
            reason="Not needed for this case",
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.assertIsNone(not_applicable.confirmed_by)
        self.assertIsNone(not_applicable.confirmed_at)

    def test_document_linking_is_same_student_only_and_never_mutates_document(self):
        case = self.create_internal()
        material = self.initialize(case.case_id)[0]
        with closing(self.connect(self.database_path)) as connection:
            before = dict(
                connection.execute(
                    "SELECT * FROM student_documents WHERE id=1600"
                ).fetchone()
            )
        attached = self.service.attach_transfer_material_document(
            case.case_id,
            material.material_id,
            1600,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.assertEqual(attached.document_id, 1600)
        self.assertEqual(attached.material_status, "available")
        with self.assertRaises(
            transfer_case_errors.TransferMaterialDocumentStudentMismatchError
        ):
            self.service.attach_transfer_material_document(
                case.case_id,
                material.material_id,
                2600,
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            )
        with self.assertRaises(
            transfer_case_errors.TransferMaterialDocumentNotFoundError
        ):
            self.service.attach_transfer_material_document(
                case.case_id,
                material.material_id,
                9999,
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            )
        detached = self.service.detach_transfer_material_document(
            case.case_id,
            material.material_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.assertIsNone(detached.document_id)
        self.assertEqual(detached.material_status, "missing")
        with closing(self.connect(self.database_path)) as connection:
            after = dict(
                connection.execute(
                    "SELECT * FROM student_documents WHERE id=1600"
                ).fetchone()
            )
        self.assertEqual(before, after)

    def test_confirmation_requires_document_and_relink_clears_confirmation(self):
        case = self.create_internal()
        material = self.initialize(case.case_id)[0]
        with self.assertRaises(
            transfer_case_errors.TransferMaterialConfirmationRequiresDocumentError
        ):
            self.service.confirm_transfer_material_item(
                case.case_id,
                material.material_id,
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            )
        self.service.attach_transfer_material_document(
            case.case_id,
            material.material_id,
            1600,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        confirmed = self.service.confirm_transfer_material_item(
            case.case_id,
            material.material_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.assertEqual(confirmed.material_status, "confirmed")
        self.assertEqual(confirmed.confirmed_by, 1)
        self.assertIsNotNone(confirmed.confirmed_at)
        self.assertEqual(
            self.service.confirm_transfer_material_item(
                case.case_id,
                material.material_id,
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            ),
            confirmed,
        )
        relinked = self.service.attach_transfer_material_document(
            case.case_id,
            material.material_id,
            1601,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.assertEqual(relinked.material_status, "available")
        self.assertIsNone(relinked.confirmed_by)
        self.assertIsNone(relinked.confirmed_at)

    def test_summary_excludes_required_not_applicable_from_denominator(self):
        case = self.create_internal()
        materials = self.initialize(case.case_id)
        self.service.attach_transfer_material_document(
            case.case_id,
            materials[0].material_id,
            1600,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.service.confirm_transfer_material_item(
            case.case_id,
            materials[0].material_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.service.change_transfer_material_status(
            case.case_id,
            materials[1].material_id,
            "not_applicable",
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.service.change_transfer_material_status(
            case.case_id,
            materials[2].material_id,
            "needs_update",
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        summary = self.service.get_transfer_material_summary(case.case_id)
        self.assertEqual(summary.total_count, 6)
        self.assertEqual(summary.required_count, 5)
        self.assertEqual(summary.confirmed_required_count, 1)
        self.assertEqual(summary.missing_required_count, 3)
        self.assertEqual(summary.needs_update_required_count, 1)
        self.assertEqual(summary.completion_percentage, 20)

    def test_permissions_flags_paused_terminal_and_cross_case_are_protected(self):
        case = self.create_internal()
        rejected = (
            ({"student_cases.view"}, self.flags()),
            ({"*"}, self.flags()),
            ({"student_cases.edit"}, self.flags(write=False)),
        )
        for permissions, flags in rejected:
            with self.subTest(permissions=permissions, flags=flags):
                with self.assertRaises(transfer_case_errors.TransferCaseAccessDeniedError):
                    self.service.ensure_transfer_material_template(
                        case.case_id,
                        actor_user_id=1,
                        actor_permissions=permissions,
                        flags=flags,
                    )
        self.service.pause_transfer_case(case.case_id, actor_user_id=1)
        with self.assertRaises(transfer_case_errors.TransferMaterialCasePausedError):
            self.initialize(case.case_id)
        self.service.resume_transfer_case(
            case.case_id, "preparing", actor_user_id=1
        )
        materials = self.initialize(case.case_id)
        self.end_case(case.case_id)
        with self.assertRaises(transfer_case_errors.TransferMaterialCaseTerminalError):
            self.service.edit_transfer_material_item(
                case.case_id,
                materials[0].material_id,
                material_label_snapshot="No",
                is_required=True,
                actor_user_id=1,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            )
        other = self.create_internal(student_id="student-2")
        with self.assertRaises(transfer_case_errors.TransferMaterialCrossCaseError):
            self.service.get_transfer_material_item(
                other.case_id, materials[0].material_id
            )

    def test_event_failure_rolls_back_material_operation(self):
        case = self.create_internal()
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER fail_t5_event BEFORE INSERT ON case_events
                   WHEN NEW.event_type='transfer_material_template_initialized'
                   BEGIN SELECT RAISE(ABORT,'t5 event failure'); END"""
            )
            connection.commit()
        with self.assertRaises(transfer_case_errors.TransferCaseIntegrityError):
            self.initialize(case.case_id)
        self.assertEqual(self.counts("case_material_items")["case_material_items"], 0)

    def test_repository_returns_stable_material_and_document_dtos(self):
        case = self.create_internal()
        material = self.initialize(case.case_id)[0]
        self.assertIsInstance(
            self.service.get_transfer_material_item(
                case.case_id, material.material_id
            ),
            transfer_case_service.TransferMaterialItemDTO,
        )
        documents = self.service.list_student_documents_for_material_linking(
            case.case_id
        )
        self.assertEqual([document.document_id for document in documents], [1601, 1600])
        self.assertEqual({document.student_id for document in documents}, {"student-1"})

    def test_concurrent_template_key_and_confirmation_writes_are_controlled(self):
        case = self.create_internal()
        barrier = threading.Barrier(2)
        initialized = []

        def initialize():
            try:
                barrier.wait(5)
                initialized.append(
                    self.service.ensure_transfer_material_template(
                        case.case_id,
                        actor_user_id=1,
                        actor_permissions={"student_cases.edit"},
                        flags=self.flags(),
                    )
                )
            except Exception as error:
                initialized.append(error)

        threads = [threading.Thread(target=initialize) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(
            sum(isinstance(item, tuple) and len(item) == 6 for item in initialized),
            2,
        )
        with closing(self.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM case_events
                       WHERE case_id=? AND event_type=
                         'transfer_material_template_initialized'""",
                    (case.case_id,),
                ).fetchone()[0],
                1,
            )

        key_barrier = threading.Barrier(2)
        key_outcomes = []

        def add_same_key():
            try:
                key_barrier.wait(5)
                key_outcomes.append(
                    self.service.add_transfer_material_item(
                        case.case_id,
                        material_key="concurrent_custom",
                        material_label_snapshot="Concurrent custom",
                        is_required=True,
                        initial_status="missing",
                        document_id=None,
                        actor_user_id=1,
                        actor_permissions={"student_cases.edit"},
                        flags=self.flags(),
                    )
                )
            except Exception as error:
                key_outcomes.append(error)

        threads = [threading.Thread(target=add_same_key) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(
            sum(
                isinstance(item, transfer_case_service.TransferMaterialItemDTO)
                for item in key_outcomes
            ),
            1,
        )
        self.assertEqual(
            sum(
                isinstance(item, transfer_case_errors.TransferMaterialDuplicateKeyError)
                for item in key_outcomes
            ),
            1,
        )

        material = self.service.list_transfer_material_items(case.case_id)[0]
        self.service.attach_transfer_material_document(
            case.case_id,
            material.material_id,
            1600,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        update_barrier = threading.Barrier(2)
        update_outcomes = []

        def confirm_or_relink(action):
            try:
                update_barrier.wait(5)
                if action == "confirm":
                    result = self.service.confirm_transfer_material_item(
                        case.case_id,
                        material.material_id,
                        actor_user_id=1,
                        actor_permissions={"student_cases.edit"},
                        flags=self.flags(),
                    )
                else:
                    result = self.service.attach_transfer_material_document(
                        case.case_id,
                        material.material_id,
                        1601,
                        actor_user_id=1,
                        actor_permissions={"student_cases.edit"},
                        flags=self.flags(),
                    )
                update_outcomes.append(result)
            except Exception as error:
                update_outcomes.append(error)

        threads = [
            threading.Thread(target=confirm_or_relink, args=(action,))
            for action in ("confirm", "relink")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(
            sum(
                isinstance(item, transfer_case_service.TransferMaterialItemDTO)
                for item in update_outcomes
            ),
            2,
        )
        final = self.service.get_transfer_material_item(
            case.case_id, material.material_id
        )
        self.assertEqual(final.document_id, 1601)
        self.assertIn(final.material_status, {"available", "confirmed"})
        self.assertEqual(
            final.confirmed_by is not None,
            final.material_status == "confirmed",
        )


class TransferCaseT6AdmissionRecordTests(
    TransferCaseT4SchoolApplicationTests
):
    def setUp(self):
        super().setUp()
        with closing(self.connect(self.database_path)) as connection:
            # Combined Visa baseline now supplies neighbouring legacy columns.
            # Keep the T6 fixture compatible with both shapes without duplicate DDL.
            for table, definitions in (
                ("offers", ("school_name TEXT", "deposit_amount REAL", "deposit_currency TEXT")),
                ("coe_oshc", ("school_name TEXT",)),
                ("payments", ("amount REAL", "currency TEXT")),
                ("student_documents", ("folder_category TEXT", "original_name TEXT")),
            ):
                existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                for definition in definitions:
                    if definition.split()[0] not in existing:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
            connection.executescript(
                """
                UPDATE offers
                   SET school_name='Target School',
                       deposit_amount=5000,
                       deposit_currency='AUD'
                 WHERE id IN (1000,1001);
                UPDATE offers
                   SET school_name='Other Student School'
                 WHERE id=2000;
                UPDATE coe_oshc
                   SET school_name='Target School'
                 WHERE id IN (1100,1101,1200,1201);
                UPDATE payments
                   SET amount=5000,currency='AUD'
                 WHERE id IN (1500,1501);
                UPDATE student_documents
                   SET folder_category='Payments',
                       document_type='OfferDepositReceipt',
                       original_name='payment-proof.pdf'
                 WHERE id IN (1600,1601);
                UPDATE student_documents
                   SET folder_category='Applications',
                       document_type='Other',
                       original_name='not-a-payment-proof.pdf'
                 WHERE id=2600;
                """
            )
            connection.commit()

    def prepare_final_application(
        self,
        *,
        school_sector="private",
        city_key="melbourne",
    ):
        case = self.create_internal()
        application = self.add_application(
            case.case_id,
            school_name_snapshot="Target School",
            school_sector=school_sector,
            city_key=city_key,
        )
        self.service.set_final_transfer_school_application(
            case.case_id,
            application.application_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        return case, application

    def edit_arguments(self):
        return {
            "actor_user_id": 1,
            "actor_permissions": {"student_cases.edit"},
            "flags": self.flags(),
        }

    def test_offer_link_replace_and_history_are_application_scoped(self):
        case, application = self.prepare_final_application()
        first = self.service.attach_transfer_offer(
            case.case_id,
            application.application_id,
            1000,
            **self.edit_arguments(),
        )
        repeated = self.service.attach_transfer_offer(
            case.case_id,
            application.application_id,
            1000,
            **self.edit_arguments(),
        )
        second = self.service.replace_transfer_offer(
            case.case_id,
            application.application_id,
            1001,
            **self.edit_arguments(),
        )
        self.assertEqual(first, repeated)
        self.assertEqual(second.record_id, 1001)
        self.assertEqual(second.validity_status, "current")
        history = self.service.list_transfer_record_links(
            case.case_id,
            application.application_id,
            "offer",
        )
        self.assertEqual(
            [(row.record_id, row.validity_status) for row in history],
            [(1001, "current"), (1000, "superseded")],
        )
        self.assertEqual(history[1].superseded_by, history[0].link_id)
        with self.assertRaises(
            transfer_case_errors.TransferAdmissionRecordStudentMismatchError
        ):
            self.service.attach_transfer_offer(
                case.case_id,
                application.application_id,
                2000,
                **self.edit_arguments(),
            )

    def test_payment_requirement_amount_status_and_proof_lifecycle(self):
        case, application = self.prepare_final_application()
        offer = self.service.attach_transfer_offer(
            case.case_id,
            application.application_id,
            1000,
            **self.edit_arguments(),
        )
        requirement = self.service.create_transfer_payment_requirement(
            case.case_id,
            application.application_id,
            "deposit",
            source_offer_id=offer.record_id,
            **self.edit_arguments(),
        )
        self.assertEqual(requirement.extracted_amount, 5000)
        self.assertEqual(requirement.confirmed_amount, 5000)
        self.assertEqual(requirement.currency, "AUD")
        with self.assertRaises(
            transfer_case_errors.TransferPaymentRequirementReasonRequiredError
        ):
            self.service.confirm_transfer_payment_amount(
                case.case_id,
                requirement.requirement_id,
                confirmed_amount=4500,
                currency="AUD",
                modification_reason=None,
                **self.edit_arguments(),
            )
        confirmed = self.service.confirm_transfer_payment_amount(
            case.case_id,
            requirement.requirement_id,
            confirmed_amount=4500,
            currency="AUD",
            modification_reason="Scholarship applied",
            **self.edit_arguments(),
        )
        self.assertEqual(confirmed.confirmed_amount, 4500)

        proof = self.service.attach_transfer_payment_proof(
            case.case_id,
            application.application_id,
            requirement.requirement_id,
            record_type="payment",
            record_id=1500,
            **self.edit_arguments(),
        )
        paid = self.service.change_transfer_payment_status(
            case.case_id,
            requirement.requirement_id,
            "paid",
            reason=None,
            **self.edit_arguments(),
        )
        self.assertEqual(paid.status, "paid")
        detached = self.service.detach_transfer_payment_proof(
            case.case_id,
            requirement.requirement_id,
            **self.edit_arguments(),
        )
        self.assertEqual(detached.validity_status, "historical")
        self.assertEqual(
            self.service.list_transfer_payment_requirements(
                case.case_id, application.application_id
            )[0].status,
            "pending",
        )
        self.assertEqual(
            self.service.get_current_transfer_payment_proof(
                case.case_id, requirement.requirement_id
            ),
            None,
        )
        with closing(self.connect(self.database_path)) as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM payments WHERE id=?", (proof.record_id,)
                ).fetchone()
            )

    def test_payment_proof_replace_uses_t6a_relation_and_is_globally_unique(self):
        case, application = self.prepare_final_application()
        first_requirement = self.service.create_transfer_payment_requirement(
            case.case_id,
            application.application_id,
            "deposit",
            source_offer_id=None,
            extracted_amount=1000,
            currency="AUD",
            **self.edit_arguments(),
        )
        second_requirement = self.service.create_transfer_payment_requirement(
            case.case_id,
            application.application_id,
            "first_payment",
            source_offer_id=None,
            extracted_amount=2000,
            currency="AUD",
            **self.edit_arguments(),
        )
        first = self.service.attach_transfer_payment_proof(
            case.case_id,
            application.application_id,
            first_requirement.requirement_id,
            record_type="student_document",
            record_id=1600,
            **self.edit_arguments(),
        )
        repeated = self.service.attach_transfer_payment_proof(
            case.case_id,
            application.application_id,
            first_requirement.requirement_id,
            record_type="student_document",
            record_id=1600,
            **self.edit_arguments(),
        )
        self.assertEqual(first, repeated)
        replacement = self.service.replace_transfer_payment_proof(
            case.case_id,
            application.application_id,
            first_requirement.requirement_id,
            record_type="student_document",
            record_id=1601,
            **self.edit_arguments(),
        )
        self.assertEqual(replacement.validity_status, "current")
        history = self.service.list_transfer_payment_requirement_proofs(
            case.case_id, first_requirement.requirement_id
        )
        self.assertEqual(
            [row.validity_status for row in history],
            ["current", "superseded"],
        )
        self.assertEqual(history[1].superseded_by, history[0].proof_id)
        with self.assertRaises(
            transfer_case_errors.TransferPaymentProofAlreadyUsedError
        ):
            self.service.attach_transfer_payment_proof(
                case.case_id,
                application.application_id,
                second_requirement.requirement_id,
                record_type="student_document",
                record_id=1601,
                **self.edit_arguments(),
            )
        with closing(self.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM case_payment_requirement_proofs
                       WHERE payment_requirement_id=?""",
                    (first_requirement.requirement_id,),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                tuple(connection.execute(
                    """SELECT folder_category,document_type,original_name
                       FROM student_documents WHERE id=1601"""
                ).fetchone()),
                ("Payments", "OfferDepositReceipt", "payment-proof.pdf"),
            )

    def test_completion_documents_follow_rule_and_workflow_prerequisites(self):
        case, application = self.prepare_final_application(
            school_sector="private"
        )
        with self.assertRaises(
            transfer_case_errors.TransferCompletionDocumentRuleError
        ):
            self.service.attach_transfer_confirmation_letter(
                case.case_id,
                application.application_id,
                1200,
                **self.edit_arguments(),
            )
        coe = self.service.attach_transfer_coe(
            case.case_id,
            application.application_id,
            1100,
            **self.edit_arguments(),
        )
        self.assertEqual(coe.record_type, "coe")
        requirement = self.service.create_transfer_payment_requirement(
            case.case_id,
            application.application_id,
            "deposit",
            source_offer_id=None,
            extracted_amount=1000,
            currency="AUD",
            **self.edit_arguments(),
        )
        proof = self.service.attach_transfer_payment_proof(
            case.case_id,
            application.application_id,
            requirement.requirement_id,
            record_type="payment",
            record_id=1500,
            **self.edit_arguments(),
        )
        self.assertIsNotNone(proof)
        self.service.change_transfer_payment_status(
            case.case_id,
            requirement.requirement_id,
            "paid",
            reason=None,
            **self.edit_arguments(),
        )
        summary = self.service.get_transfer_admission_summary(case.case_id)
        self.assertTrue(summary.payment_satisfied)
        self.assertTrue(summary.completion_document_satisfied)
        self.assertTrue(summary.ready_to_complete)

        second_case = self.create_internal(student_id="student-2")
        second_application = self.add_application(
            second_case.case_id,
            school_name_snapshot="Other Student School",
        )
        self.service.set_final_transfer_school_application(
            second_case.case_id,
            second_application.application_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        with self.assertRaises(
            transfer_case_errors.TransferAdmissionRecordStudentMismatchError
        ):
            self.service.attach_transfer_coe(
                second_case.case_id,
                second_application.application_id,
                1100,
                manual_school_confirmation=True,
                confirmation_reason="School name not available",
                **self.edit_arguments(),
            )

    def test_event_failure_rolls_back_payment_proof_and_record_link(self):
        case, application = self.prepare_final_application()
        requirement = self.service.create_transfer_payment_requirement(
            case.case_id,
            application.application_id,
            "deposit",
            source_offer_id=None,
            extracted_amount=1000,
            currency="AUD",
            **self.edit_arguments(),
        )
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER fail_t6_proof_event
                   BEFORE INSERT ON case_events
                   WHEN NEW.event_type='transfer_payment_proof_attached'
                   BEGIN SELECT RAISE(ABORT, 'forced event failure'); END"""
            )
            connection.commit()
        with self.assertRaises(transfer_case_errors.TransferCaseIntegrityError):
            self.service.attach_transfer_payment_proof(
                case.case_id,
                application.application_id,
                requirement.requirement_id,
                record_type="payment",
                record_id=1500,
                **self.edit_arguments(),
            )
        self.assertEqual(
            self.counts(
                "case_record_links", "case_payment_requirement_proofs"
            ),
            {"case_record_links": 0, "case_payment_requirement_proofs": 0},
        )

    def test_confirmation_letter_mode_does_not_block_final_school_coe(self):
        case, application = self.prepare_final_application(
            school_sector="public",
            city_key="melbourne",
        )
        current = self.service.get_transfer_case(case.case_id)
        self.assertEqual(
            current.required_completion_document,
            "confirmation_letter",
        )
        coe = self.service.attach_transfer_coe(
            case.case_id,
            application.application_id,
            1100,
            **self.edit_arguments(),
        )
        self.assertEqual(coe.record_type, "coe")
        letter = self.service.attach_transfer_confirmation_letter(
            case.case_id,
            application.application_id,
            1200,
            **self.edit_arguments(),
        )
        self.assertEqual(letter.record_type, "confirmation_letter")
        self.assertTrue(
            self.service.get_transfer_admission_summary(
                case.case_id
            ).ready_to_complete
        )

    def test_payment_document_category_is_controlled_without_filename_guessing(self):
        case, application = self.prepare_final_application()
        requirement = self.service.create_transfer_payment_requirement(
            case.case_id,
            application.application_id,
            "deposit",
            source_offer_id=None,
            extracted_amount=1000,
            currency="AUD",
            **self.edit_arguments(),
        )
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE student_documents
                   SET folder_category='Applications',
                       document_type='Other',
                       original_name='OfferDepositReceipt payment proof.pdf'
                   WHERE id=1600"""
            )
            connection.commit()
        with self.assertRaises(
            transfer_case_errors.TransferPaymentProofInvalidError
        ):
            self.service.attach_transfer_payment_proof(
                case.case_id,
                application.application_id,
                requirement.requirement_id,
                record_type="student_document",
                record_id=1600,
                **self.edit_arguments(),
            )
        self.assertEqual(
            [row["record_id"] for row in
             self.service.list_available_payment_documents(case.case_id)],
            [1601],
        )

    def test_workflow_checks_offer_payment_rule_and_completion_document(self):
        case, application = self.prepare_final_application()
        for state in (
            "preparing",
            "school_search",
            "target_confirmed",
            "application_submitted",
            "waiting_offer",
        ):
            self.service.transition_workflow(
                case.case_id, state, actor_user_id=1
            )
        with self.assertRaises(
            transfer_case_errors.TransferWorkflowPrerequisiteError
        ):
            self.service.transition_workflow(
                case.case_id, "offer_received", actor_user_id=1
            )
        self.service.attach_transfer_offer(
            case.case_id,
            application.application_id,
            1000,
            **self.edit_arguments(),
        )
        self.service.transition_workflow(
            case.case_id, "offer_received", actor_user_id=1
        )
        with self.assertRaises(
            transfer_case_errors.TransferWorkflowPrerequisiteError
        ):
            self.service.transition_workflow(
                case.case_id, "waiting_payment", actor_user_id=1
            )
        requirement = self.service.create_transfer_payment_requirement(
            case.case_id,
            application.application_id,
            "deposit",
            source_offer_id=1000,
            **self.edit_arguments(),
        )
        self.service.transition_workflow(
            case.case_id, "waiting_payment", actor_user_id=1
        )
        with self.assertRaises(
            transfer_case_errors.TransferWorkflowPrerequisiteError
        ):
            self.service.transition_workflow(
                case.case_id, "payment_satisfied", actor_user_id=1
            )
        self.service.attach_transfer_payment_proof(
            case.case_id,
            application.application_id,
            requirement.requirement_id,
            record_type="payment",
            record_id=1500,
            **self.edit_arguments(),
        )
        self.service.change_transfer_payment_status(
            case.case_id,
            requirement.requirement_id,
            "paid",
            reason=None,
            **self.edit_arguments(),
        )
        self.service.transition_workflow(
            case.case_id, "payment_satisfied", actor_user_id=1
        )
        self.service.transition_workflow(
            case.case_id,
            "waiting_completion_document",
            actor_user_id=1,
        )
        with self.assertRaises(
            transfer_case_errors.TransferWorkflowPrerequisiteError
        ):
            self.service.transition_workflow(
                case.case_id, "ready_to_complete", actor_user_id=1
            )
        self.service.attach_transfer_coe(
            case.case_id,
            application.application_id,
            1100,
            **self.edit_arguments(),
        )
        with self.assertRaises(
            transfer_case_errors.TransferWorkflowPrerequisiteError
        ):
            self.service.transition_workflow(
                case.case_id, "ready_to_complete", actor_user_id=1
            )
        with closing(self.connect(self.database_path)) as connection:
            student_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(students)")
            }
            if "date_of_birth" not in student_columns:
                connection.execute(
                    "ALTER TABLE students ADD COLUMN date_of_birth TEXT"
                )
            connection.execute(
                """UPDATE students SET date_of_birth='2010-02-01'
                   WHERE student_id='student-1'"""
            )
            connection.commit()
        arrangements = (
            transfer_arrangement_service.TransferArrangementService(
                self.database_path
            )
        )
        arrangements.create_transfer_guardianship_arrangement(
            case.case_id,
            arrangement_mode="parent_guardian",
            guardian_person_name="Parent",
            guardian_organisation_name=None,
            relationship_to_student="parent",
            provider_name=None,
            start_date=None,
            end_date=None,
            source_type="staff_entered",
            arrangement_status="confirmed",
            notes=None,
            **self.edit_arguments(),
        )
        for arrangement_type in (
            "visa",
            "oshc",
            "accommodation",
        ):
            getattr(
                arrangements,
                f"mark_transfer_{arrangement_type}_not_applicable",
            )(
                case.case_id,
                requirement_reason=f"{arrangement_type} reviewed",
                **self.edit_arguments(),
            )
        ready = self.service.transition_workflow(
            case.case_id, "ready_to_complete", actor_user_id=1
        )
        self.assertEqual(ready.workflow_state, "ready_to_complete")


class TransferCaseT7ArrangementServiceTests(
    TransferCaseRepositoryServiceTests
):
    def setUp(self):
        super().setUp()
        if transfer_arrangement_service is not None:
            self.arrangements = (
                transfer_arrangement_service.TransferArrangementService(
                    self.database_path,
                    timeout=2.0,
                    clock=lambda: "2026-07-30T10:00:00+10:00",
                )
            )

    @staticmethod
    def edit_flags():
        return StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
        )

    def edit_arguments(self, **overrides):
        values = {
            "actor_user_id": 1,
            "actor_permissions": {"student_cases.edit"},
            "flags": self.edit_flags(),
            "request_id": "t7-arrangement-test",
            "correlation_id": "t7-arrangement-test",
        }
        values.update(overrides)
        return values

    def create_final_application(
        self,
        case_id,
        *,
        expected_start_date="2027-01-20",
    ):
        now = "2026-07-30T09:00:00+10:00"
        with closing(self.connect(self.database_path)) as connection:
            cursor = connection.execute(
                """INSERT INTO transfer_school_applications(
                       case_id,school_id,school_name_snapshot,
                       institution_group_id,city_key,campus_key,school_sector,
                       expected_start_date,preference_order,
                       application_status,notes,is_final,created_by,
                       created_at,updated_at
                   ) VALUES(
                       ?,10,'Target School',NULL,'melbourne',NULL,'private',
                       ?,1,'accepted',NULL,1,1,?,?
                   )""",
                (case_id, expected_start_date, now, now),
            )
            application_id = int(cursor.lastrowid)
            connection.execute(
                """UPDATE transfer_cases
                   SET final_application_id=?,target_city_key='melbourne',
                       target_school_sector='private',rule_status='determined',
                       same_city_result='same_city',
                       sector_transition='public_to_private',
                       required_completion_document='coe',
                       tuition_requirement='deposit_or_first_payment_required',
                       rule_calculated_at=?,rule_calculated_by=1,
                       rule_version='transfer-phase2-v1',updated_at=?
                   WHERE case_id=?""",
                (application_id, now, now, case_id),
            )
            connection.commit()
        return application_id

    def event_count(self, case_id):
        with closing(self.connect(self.database_path)) as connection:
            return connection.execute(
                "SELECT count(*) FROM case_events WHERE case_id=?",
                (case_id,),
            ).fetchone()[0]

    def test_t7_modules_are_side_effect_free_and_expose_stable_contracts(self):
        self.assertIsNotNone(transfer_arrangement_dto)
        self.assertIsNotNone(transfer_arrangement_errors)
        self.assertIsNotNone(transfer_arrangement_repository)
        self.assertIsNotNone(transfer_arrangement_service)
        expected_repository_methods = {
            "list_available_visas",
            "list_visa_record_links",
            "get_current_visa_record_link",
            "get_visa_confirmation",
            "insert_visa_record_link",
            "supersede_visa_record_link",
            "update_visa_record_link_validity",
            "insert_or_update_visa_confirmation",
            "list_available_oshc_records",
            "list_oshc_record_links",
            "get_current_oshc_record_link",
            "get_oshc_confirmation",
            "insert_oshc_record_link",
            "supersede_oshc_record_link",
            "update_oshc_record_link_validity",
            "insert_or_update_oshc_confirmation",
            "list_guardianship_versions",
            "get_current_guardianship",
            "get_guardianship_version",
            "insert_guardianship_version",
            "supersede_guardianship_version",
            "list_accommodation_versions",
            "get_current_accommodation",
            "get_accommodation_version",
            "insert_accommodation_version",
            "supersede_accommodation_version",
            "list_guardianship_evidence",
            "list_accommodation_evidence",
        }
        self.assertTrue(
            all(
                callable(getattr(transfer_arrangement_repository, name, None))
                for name in expected_repository_methods
            )
        )
        expected_codes = {
            "transfer_arrangement_not_found",
            "transfer_arrangement_invalid_type",
            "transfer_arrangement_invalid_status",
            "transfer_arrangement_invalid_transition",
            "transfer_arrangement_schema_not_ready",
            "transfer_arrangement_case_paused",
            "transfer_arrangement_case_terminal",
            "transfer_arrangement_record_not_found",
            "transfer_arrangement_record_student_mismatch",
            "transfer_arrangement_record_type_mismatch",
            "transfer_arrangement_version_conflict",
            "transfer_arrangement_current_conflict",
            "transfer_arrangement_evidence_not_found",
            "transfer_arrangement_evidence_student_mismatch",
            "transfer_arrangement_evidence_type_mismatch",
            "transfer_arrangement_requirement_reason_required",
            "transfer_arrangement_confirmation_incomplete",
            "transfer_arrangement_rule_input_missing",
            "transfer_arrangement_prerequisite_not_met",
            "transfer_arrangement_database_busy",
            "transfer_arrangement_integrity_error",
            "transfer_arrangement_confirmation_mismatch",
            "transfer_arrangement_current_record_missing",
            "transfer_arrangement_access_denied",
        }
        self.assertEqual(
            set(transfer_arrangement_errors.ERROR_TYPES_BY_CODE),
            expected_codes,
        )
        source = Path(transfer_arrangement_repository.__file__).read_text(
            encoding="utf-8"
        )
        self.assertNotIn("sqlite3.connect", source)
        self.assertNotIn(".commit(", source)
        self.assertNotIn("aaie_crm.db", source)
        with closing(self.connect(self.database_path)) as connection:
            self.assertTrue(
                transfer_arrangement_repository.schema_ready(connection)
            )
            rows = transfer_arrangement_repository.list_available_visas(
                connection, "student-1"
            )
            self.assertTrue(all(type(row) is dict for row in rows))
        empty_path = self.root / "t7-empty.db"
        sqlite3.connect(empty_path).close()
        with self.assertRaises(
            transfer_arrangement_errors.TransferArrangementSchemaNotReadyError
        ):
            transfer_arrangement_service.TransferArrangementService(
                empty_path
            ).get_transfer_arrangement_summary(1)

    def test_visa_lifecycle_replaces_links_preserves_source_and_noops(self):
        case = self.create_case()
        before_visas = self.counts("visas")["visas"]
        options = self.arrangements.list_transfer_visa_options(case.case_id)
        self.assertEqual([item.record_id for item in options], [1401, 1400])
        first = self.arrangements.carry_forward_transfer_visa(
            case.case_id, 1400, **self.edit_arguments()
        )
        self.assertEqual(first.confirmation.arrangement_status, "carried_forward")
        self.assertEqual(first.selected_visa.record_id, 1400)
        event_count = self.event_count(case.case_id)
        duplicate = self.arrangements.carry_forward_transfer_visa(
            case.case_id, 1400, **self.edit_arguments()
        )
        self.assertEqual(duplicate, first)
        self.assertEqual(self.event_count(case.case_id), event_count)
        updated = self.arrangements.update_transfer_visa(
            case.case_id, 1401, **self.edit_arguments()
        )
        self.assertEqual(updated.confirmation.arrangement_status, "updated")
        self.assertEqual(updated.current_record_link.record_id, 1401)
        self.assertEqual(updated.history[0].validity_status, "superseded")
        self.assertEqual(
            updated.history[0].superseded_by,
            updated.current_record_link.link_id,
        )
        confirmed = self.arrangements.confirm_transfer_visa(
            case.case_id, **self.edit_arguments()
        )
        self.assertEqual(confirmed.confirmation.confirmed_by, 1)
        self.assertIsNotNone(confirmed.confirmation.confirmed_at)
        pending = self.arrangements.set_transfer_visa_pending(
            case.case_id, **self.edit_arguments()
        )
        self.assertFalse(pending.satisfied)
        self.assertIsNone(pending.confirmation.confirmed_by)
        self.assertEqual(self.counts("visas")["visas"], before_visas)
        with closing(self.connect(self.database_path)) as connection:
            metadata = json.loads(
                connection.execute(
                    """SELECT metadata_json FROM case_events
                       WHERE case_id=?
                         AND event_type='transfer_visa_marked_pending'
                       ORDER BY id DESC LIMIT 1""",
                    (case.case_id,),
                ).fetchone()[0]
            )
        self.assertTrue(
            {
                "actor_user_id",
                "student_id",
                "case_id",
                "arrangement_type",
                "arrangement_id",
                "confirmation_id",
                "record_link_id",
                "evidence_link_id",
                "status",
                "version_no",
                "rule_version",
                "requirement_reason",
                "request_id",
                "correlation_id",
                "timestamp",
            }.issubset(metadata)
        )

    def test_visa_rejects_other_student_and_requires_reason_for_na(self):
        case = self.create_case()
        with self.assertRaises(
            transfer_arrangement_errors.TransferArrangementRecordStudentMismatchError
        ):
            self.arrangements.update_transfer_visa(
                case.case_id, 2400, **self.edit_arguments()
            )
        with self.assertRaises(
            transfer_arrangement_errors.TransferArrangementRequirementReasonRequiredError
        ):
            self.arrangements.mark_transfer_visa_not_applicable(
                case.case_id, requirement_reason=" ", **self.edit_arguments()
            )
        result = self.arrangements.mark_transfer_visa_not_applicable(
            case.case_id,
            requirement_reason="Visa is not required for this student.",
            **self.edit_arguments(),
        )
        self.assertTrue(result.satisfied)
        self.assertEqual(
            result.confirmation.arrangement_status, "not_applicable"
        )

    def test_oshc_only_accepts_oshc_and_preserves_source_records(self):
        case = self.create_case()
        before = self.counts("coe_oshc")["coe_oshc"]
        options = self.arrangements.list_transfer_oshc_options(case.case_id)
        self.assertEqual([item.record_id for item in options], [1301, 1300])
        with self.assertRaises(
            transfer_arrangement_errors.TransferArrangementRecordTypeMismatchError
        ):
            self.arrangements.update_transfer_oshc(
                case.case_id, 1100, **self.edit_arguments()
            )
        first = self.arrangements.carry_forward_transfer_oshc(
            case.case_id, 1300, **self.edit_arguments()
        )
        self.assertEqual(first.current_record_link.record_id, 1300)
        updated = self.arrangements.update_transfer_oshc(
            case.case_id, 1301, **self.edit_arguments()
        )
        self.assertEqual(updated.confirmation.arrangement_status, "updated")
        confirmed = self.arrangements.confirm_transfer_oshc(
            case.case_id, **self.edit_arguments()
        )
        self.assertTrue(confirmed.satisfied)
        self.assertEqual(self.counts("coe_oshc")["coe_oshc"], before)

    def test_guardianship_versions_are_immutable_and_confirmation_tracks_current(self):
        case = self.create_case()
        first = self.arrangements.create_transfer_guardianship_arrangement(
            case.case_id,
            arrangement_mode="pending_confirmation",
            guardian_person_name=None,
            guardian_organisation_name=None,
            relationship_to_student=None,
            provider_name=None,
            start_date=None,
            end_date=None,
            source_type="staff_entered",
            arrangement_status="pending",
            notes=None,
            **self.edit_arguments(),
        )
        self.assertEqual(first.current_arrangement.version_no, 1)
        second = self.arrangements.replace_transfer_guardianship_arrangement(
            case.case_id,
            first.current_arrangement.arrangement_id,
            arrangement_mode="parent_guardian",
            guardian_person_name="Parent One",
            guardian_organisation_name=None,
            relationship_to_student="parent",
            provider_name=None,
            start_date="2027-01-01",
            end_date="2027-12-31",
            source_type="updated_arrangement",
            arrangement_status="updated",
            notes="Reviewed",
            **self.edit_arguments(),
        )
        self.assertEqual(second.current_arrangement.version_no, 2)
        self.assertEqual(second.history[0].superseded_by, second.current_arrangement.arrangement_id)
        confirmed = self.arrangements.confirm_transfer_guardianship(
            case.case_id, **self.edit_arguments()
        )
        self.assertTrue(confirmed.satisfied)
        self.assertEqual(
            confirmed.confirmation.guardianship_arrangement_id,
            confirmed.current_arrangement.arrangement_id,
        )
        with closing(self.connect(self.database_path)) as connection:
            rows = connection.execute(
                """SELECT version_no,is_current,superseded_by
                   FROM transfer_guardianship_arrangements
                   WHERE case_id=? ORDER BY version_no""",
                (case.case_id,),
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (1, 0, second.current_arrangement.arrangement_id),
                (2, 0, confirmed.current_arrangement.arrangement_id),
                (3, 1, None),
            ],
        )
        with closing(self.connect(self.database_path)) as connection:
            event_types = [
                row[0]
                for row in connection.execute(
                    """SELECT event_type FROM case_events
                       WHERE case_id=? ORDER BY id""",
                    (case.case_id,),
                )
            ]
        self.assertIn("transfer_guardianship_confirmed", event_types)

    def test_guardianship_confirmed_requires_complete_structured_data(self):
        case = self.create_case()
        with self.assertRaises(
            transfer_arrangement_errors.TransferArrangementConfirmationIncompleteError
        ):
            self.arrangements.create_transfer_guardianship_arrangement(
                case.case_id,
                arrangement_mode="parent_guardian",
                guardian_person_name=None,
                guardian_organisation_name=None,
                relationship_to_student=None,
                provider_name=None,
                start_date=None,
                end_date=None,
                source_type="staff_entered",
                arrangement_status="confirmed",
                notes=None,
                **self.edit_arguments(),
            )
        self.assertEqual(
            self.counts(
                "transfer_guardianship_arrangements",
                "case_arrangement_confirmations",
            ),
            {
                "transfer_guardianship_arrangements": 0,
                "case_arrangement_confirmations": 0,
            },
        )

    def test_accommodation_versions_and_not_applicable_reason(self):
        case = self.create_case()
        first = self.arrangements.create_transfer_accommodation_arrangement(
            case.case_id,
            accommodation_type="homestay",
            provider_name="Provider",
            host_name="Host",
            address_line1="1 Test Street",
            address_line2=None,
            suburb="Melbourne",
            state_code="VIC",
            postcode="3000",
            country_code="AU",
            start_date="2027-01-01",
            end_date=None,
            source_type="staff_entered",
            arrangement_status="confirmed",
            notes=None,
            **self.edit_arguments(),
        )
        self.assertTrue(first.satisfied)
        second = self.arrangements.replace_transfer_accommodation_arrangement(
            case.case_id,
            first.current_arrangement.arrangement_id,
            accommodation_type="private_rental",
            provider_name=None,
            host_name=None,
            address_line1="2 Test Street",
            address_line2=None,
            suburb="Melbourne",
            state_code="VIC",
            postcode="3000",
            country_code="AU",
            start_date="2027-02-01",
            end_date=None,
            source_type="updated_arrangement",
            arrangement_status="updated",
            notes=None,
            **self.edit_arguments(),
        )
        self.assertEqual(second.current_arrangement.version_no, 2)
        self.assertEqual(second.confirmation.arrangement_status, "updated")
        with self.assertRaises(
            transfer_arrangement_errors.TransferArrangementRequirementReasonRequiredError
        ):
            self.arrangements.mark_transfer_accommodation_not_applicable(
                case.case_id, requirement_reason=None, **self.edit_arguments()
            )
        marked = (
            self.arrangements.mark_transfer_accommodation_not_applicable(
                case.case_id,
                requirement_reason="Accommodation not required.",
                **self.edit_arguments(),
            )
        )
        before_events = self.event_count(case.case_id)
        duplicate = (
            self.arrangements.mark_transfer_accommodation_not_applicable(
                case.case_id,
                requirement_reason="Accommodation not required.",
                **self.edit_arguments(),
            )
        )
        self.assertEqual(duplicate, marked)
        self.assertEqual(self.event_count(case.case_id), before_events)

    def test_evidence_is_case_student_type_scoped_and_history_is_retained(self):
        case = self.create_case()
        visa = self.arrangements.update_transfer_visa(
            case.case_id, 1400, **self.edit_arguments()
        )
        first = self.arrangements.attach_arrangement_evidence(
            case.case_id,
            "visa",
            arrangement_confirmation_id=visa.confirmation.confirmation_id,
            guardianship_arrangement_id=None,
            accommodation_arrangement_id=None,
            case_record_link_id=visa.current_record_link.link_id,
            document_id=1600,
            evidence_role="primary",
            **self.edit_arguments(),
        )
        duplicate = self.arrangements.attach_arrangement_evidence(
            case.case_id,
            "visa",
            arrangement_confirmation_id=visa.confirmation.confirmation_id,
            guardianship_arrangement_id=None,
            accommodation_arrangement_id=None,
            case_record_link_id=visa.current_record_link.link_id,
            document_id=1600,
            evidence_role="primary",
            **self.edit_arguments(),
        )
        self.assertEqual(first, duplicate)
        with self.assertRaises(
            transfer_arrangement_errors.TransferArrangementEvidenceStudentMismatchError
        ):
            self.arrangements.attach_arrangement_evidence(
                case.case_id,
                "visa",
                arrangement_confirmation_id=visa.confirmation.confirmation_id,
                guardianship_arrangement_id=None,
                accommodation_arrangement_id=None,
                case_record_link_id=visa.current_record_link.link_id,
                document_id=2600,
                evidence_role="supporting",
                **self.edit_arguments(),
            )
        replacement = self.arrangements.replace_arrangement_evidence(
            case.case_id,
            first.evidence_id,
            case_record_link_id=visa.current_record_link.link_id,
            document_id=1601,
            evidence_role="primary",
            **self.edit_arguments(),
        )
        with self.assertRaises(
            transfer_arrangement_errors.TransferArrangementEvidenceTypeMismatchError
        ):
            self.arrangements.replace_arrangement_evidence(
                case.case_id,
                replacement.evidence_id,
                case_record_link_id=visa.current_record_link.link_id,
                document_id=1600,
                evidence_role="unsupported",
                **self.edit_arguments(),
            )
        history = self.arrangements.list_arrangement_evidence(
            case.case_id, "visa"
        )
        old = next(item for item in history if item.evidence_id == first.evidence_id)
        self.assertEqual(old.validity_status, "superseded")
        self.assertEqual(old.superseded_by, replacement.evidence_id)
        self.assertFalse(hasattr(old, "file_content"))

    def test_structured_evidence_wrappers_bind_exact_version(self):
        case = self.create_case()
        guardianship = (
            self.arrangements.create_transfer_guardianship_arrangement(
                case.case_id,
                arrangement_mode="parent_guardian",
                guardian_person_name="Parent",
                guardian_organisation_name=None,
                relationship_to_student="parent",
                provider_name=None,
                start_date=None,
                end_date=None,
                source_type="staff_entered",
                arrangement_status="confirmed",
                notes=None,
                **self.edit_arguments(),
            )
        )
        attached = self.arrangements.attach_transfer_guardianship_evidence(
            case.case_id,
            guardianship.current_arrangement.arrangement_id,
            case_record_link_id=None,
            document_id=1600,
            evidence_role="authority",
            **self.edit_arguments(),
        )
        self.assertEqual(
            attached.guardianship_arrangement_id,
            guardianship.current_arrangement.arrangement_id,
        )
        historical = (
            self.arrangements.mark_transfer_guardianship_evidence_historical(
                case.case_id,
                attached.evidence_id,
                **self.edit_arguments(),
            )
        )
        self.assertEqual(historical.validity_status, "historical")

    def test_summary_uses_final_start_date_for_guardianship_requirement(self):
        case = self.create_case()
        self.create_final_application(
            case.case_id, expected_start_date="2027-01-20"
        )
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                "ALTER TABLE students ADD COLUMN date_of_birth TEXT"
            )
            connection.execute(
                """UPDATE students SET date_of_birth='2010-02-01'
                   WHERE student_id='student-1'"""
            )
            connection.commit()
        pending = self.arrangements.get_transfer_arrangement_summary(
            case.case_id
        )
        self.assertTrue(pending.guardianship_required)
        self.assertFalse(pending.arrangements_satisfied)
        self.assertEqual(pending.arrangements_required_count, 4)
        with self.assertRaises(
            transfer_arrangement_errors.TransferArrangementInvalidTransitionError
        ):
            self.arrangements.mark_transfer_guardianship_not_applicable(
                case.case_id,
                requirement_reason="Not a valid shortcut.",
                **self.edit_arguments(),
            )
        self.arrangements.create_transfer_guardianship_arrangement(
            case.case_id,
            arrangement_mode="parent_guardian",
            guardian_person_name="Parent",
            guardian_organisation_name=None,
            relationship_to_student="parent",
            provider_name=None,
            start_date=None,
            end_date=None,
            source_type="staff_entered",
            arrangement_status="confirmed",
            notes=None,
            **self.edit_arguments(),
        )
        for arrangement_type in ("visa", "oshc", "accommodation"):
            marker = getattr(
                self.arrangements,
                f"mark_transfer_{arrangement_type}_not_applicable",
            )
            marker(
                case.case_id,
                requirement_reason=f"{arrangement_type} reviewed",
                **self.edit_arguments(),
            )
        complete = self.arrangements.get_transfer_arrangement_summary(
            case.case_id
        )
        self.assertTrue(complete.arrangements_satisfied)
        self.assertEqual(complete.arrangements_satisfied_count, 4)
        self.assertEqual(complete.arrangements_pending_count, 0)

    def test_summary_reports_missing_age_rule_inputs_without_using_today(self):
        case = self.create_case()
        summary = self.arrangements.get_transfer_arrangement_summary(
            case.case_id
        )
        self.assertTrue(summary.guardianship_required)
        self.assertIn(
            "guardianship_rule_input_missing", summary.blocking_reasons
        )
        self.assertEqual(summary.rule_version, "transfer-arrangements-v1")

    def test_summary_rejects_corrupt_confirmation_link_state(self):
        case = self.create_case()
        current = self.arrangements.update_transfer_visa(
            case.case_id, 1400, **self.edit_arguments()
        )
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                "DROP TRIGGER case_record_links_arrangement_confirmation_guard"
            )
            connection.execute(
                """UPDATE case_record_links SET validity_status='historical'
                   WHERE id=?""",
                (current.current_record_link.link_id,),
            )
            connection.commit()
        summary = self.arrangements.get_transfer_arrangement_summary(
            case.case_id
        )
        self.assertFalse(summary.visa_satisfied)
        self.assertEqual(
            summary.visa_blocking_reason, "visa_current_record_missing"
        )

    def test_adult_guardianship_still_requires_manual_not_applicable(self):
        case = self.create_case()
        self.create_final_application(
            case.case_id, expected_start_date="2027-01-20"
        )
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                "ALTER TABLE students ADD COLUMN date_of_birth TEXT"
            )
            connection.execute(
                """UPDATE students SET date_of_birth='2000-01-01'
                   WHERE student_id='student-1'"""
            )
            connection.commit()
        summary = self.arrangements.get_transfer_arrangement_summary(
            case.case_id
        )
        self.assertFalse(summary.guardianship_required)
        self.assertFalse(summary.arrangements_satisfied)
        self.assertIn(
            "guardianship_manual_not_applicable_required",
            summary.blocking_reasons,
        )

    def test_concurrent_guardianship_replace_leaves_one_current_version(self):
        case = self.create_case()
        first = self.arrangements.create_transfer_guardianship_arrangement(
            case.case_id,
            arrangement_mode="pending_confirmation",
            guardian_person_name=None,
            guardian_organisation_name=None,
            relationship_to_student=None,
            provider_name=None,
            start_date=None,
            end_date=None,
            source_type="staff_entered",
            arrangement_status="pending",
            notes=None,
            **self.edit_arguments(),
        )
        barrier = threading.Barrier(2)
        outcomes = []

        def replace(name):
            service = (
                transfer_arrangement_service.TransferArrangementService(
                    self.database_path,
                    timeout=2.0,
                    clock=lambda: "2026-07-30T10:00:01+10:00",
                )
            )
            barrier.wait()
            try:
                outcomes.append(
                    service.replace_transfer_guardianship_arrangement(
                        case.case_id,
                        first.current_arrangement.arrangement_id,
                        arrangement_mode="parent_guardian",
                        guardian_person_name=name,
                        guardian_organisation_name=None,
                        relationship_to_student="parent",
                        provider_name=None,
                        start_date=None,
                        end_date=None,
                        source_type="updated_arrangement",
                        arrangement_status="updated",
                        notes=None,
                        **self.edit_arguments(),
                    )
                )
            except transfer_arrangement_errors.TransferArrangementError as error:
                outcomes.append(error)

        threads = [
            threading.Thread(target=replace, args=("Parent A",)),
            threading.Thread(target=replace, args=("Parent B",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(
            sum(
                isinstance(
                    item,
                    transfer_arrangement_dto.TransferGuardianshipArrangementDTO,
                )
                for item in outcomes
            ),
            1,
        )
        self.assertEqual(
            sum(
                isinstance(
                    item,
                    transfer_arrangement_errors.TransferArrangementVersionConflictError,
                )
                for item in outcomes
            ),
            1,
        )
        with closing(self.connect(self.database_path)) as connection:
            current_count = connection.execute(
                """SELECT count(*)
                   FROM transfer_guardianship_arrangements
                   WHERE case_id=? AND is_current=1""",
                (case.case_id,),
            ).fetchone()[0]
        self.assertEqual(current_count, 1)

    def test_write_requires_explicit_edit_and_active_lifecycle(self):
        case = self.create_case()
        for permissions in ({"*"}, {"student_cases.view"}, None):
            with self.subTest(permissions=permissions):
                with self.assertRaises(
                    transfer_arrangement_errors.TransferArrangementAccessDeniedError
                ):
                    self.arrangements.update_transfer_visa(
                        case.case_id,
                        1400,
                        **self.edit_arguments(
                            actor_permissions=permissions
                        ),
                    )
        with self.assertRaises(
            transfer_arrangement_errors.TransferArrangementAccessDeniedError
        ):
            self.arrangements.update_transfer_visa(
                case.case_id,
                1400,
                **self.edit_arguments(
                    flags=StudentCaseFeatureFlags(read_enabled=True)
                ),
            )
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE transfer_cases SET workflow_state='paused'
                   WHERE case_id=?""",
                (case.case_id,),
            )
            connection.execute(
                """UPDATE student_cases SET status='paused',paused_at='now'
                   WHERE id=?""",
                (case.case_id,),
            )
            connection.commit()
        with self.assertRaises(
            transfer_arrangement_errors.TransferArrangementCasePausedError
        ):
            self.arrangements.update_transfer_visa(
                case.case_id, 1400, **self.edit_arguments()
            )
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE transfer_cases SET workflow_state='cancelled'
                   WHERE case_id=?""",
                (case.case_id,),
            )
            connection.execute(
                """UPDATE student_cases
                   SET status='cancelled',terminal_result='cancelled',
                       active_slot=0,cancelled_at='now'
                   WHERE id=?""",
                (case.case_id,),
            )
            connection.commit()
        with self.assertRaises(
            transfer_arrangement_errors.TransferArrangementCaseTerminalError
        ):
            self.arrangements.update_transfer_visa(
                case.case_id, 1400, **self.edit_arguments()
            )

    def test_event_failure_rolls_back_link_confirmation_and_version(self):
        case = self.create_case()
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER t7_test_event_failure
                   BEFORE INSERT ON case_events
                   WHEN NEW.event_type='transfer_visa_updated'
                   BEGIN
                       SELECT RAISE(ABORT,'forced event failure');
                   END"""
            )
            connection.commit()
        with self.assertRaises(
            transfer_arrangement_errors.TransferArrangementIntegrityError
        ):
            self.arrangements.update_transfer_visa(
                case.case_id, 1400, **self.edit_arguments()
            )
        self.assertEqual(
            self.counts(
                "case_record_links", "case_arrangement_confirmations"
            ),
            {"case_record_links": 0, "case_arrangement_confirmations": 0},
        )


@unittest.skipIf(
    transfer_case_views is None,
    "Transfer views module is not implemented yet",
)
class TransferCaseT7ArrangementViewTests(
    TransferCaseT7ArrangementServiceTests
):
    @staticmethod
    def user(*permissions):
        return {"id": 1, "permissions": list(permissions)}

    def render(self, case_id, *permissions, write=True):
        return transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            case_id,
            self.user(*permissions),
            StudentCaseFeatureFlags(
                read_enabled=True,
                transfer_write_enabled=write,
            ),
            csrf="csrf-token",
            staff_users=(),
        )

    def test_t7c_view_renders_summary_four_cards_and_permission_isolation(self):
        case = self.create_case()
        readonly = self.render(case.case_id, "student_cases.view")
        self.assertIn('id="transfer-arrangements"', readonly)
        self.assertIn("转学后续文件", readonly)
        self.assertNotIn("Arrangement Summary", readonly)
        self.assertIn("签证", readonly)
        self.assertIn("OSHC", readonly)
        self.assertIn("监护安排", readonly)
        self.assertIn("住宿安排", readonly)
        self.assertNotIn("guardianship_rule_input_missing", readonly)
        self.assertNotIn('data-arrangement-write', readonly)
        self.assertNotIn("<form", readonly.split(
            'id="transfer-arrangements"', 1
        )[1].split('id="transfer-case-people"', 1)[0])

        wildcard = self.render(
            case.case_id, "student_cases.view", "*"
        )
        self.assertNotIn('data-arrangement-write', wildcard)

        editable = self.render(
            case.case_id,
            "student_cases.view",
            "student_cases.edit",
        )
        self.assertIn('data-arrangement-write', editable)
        self.assertIn('data-dialog-open="transfer-visa-select"', editable)
        self.assertIn(
            'data-dialog-open="transfer-guardianship-create"', editable
        )
        self.assertNotIn("Complete Case", editable)
        self.assertNotIn("Airport Pickup", editable)
        self.assertNotIn("Transcript/School Report arrangement", editable)

        write_off = self.render(
            case.case_id,
            "student_cases.view",
            "student_cases.edit",
            write=False,
        )
        self.assertNotIn('data-arrangement-write', write_off)

    def test_t7c_view_renders_current_history_evidence_warnings_and_status(self):
        case = self.create_case()
        visa = self.arrangements.update_transfer_visa(
            case.case_id, 1400, **self.edit_arguments()
        )
        self.arrangements.attach_arrangement_evidence(
            case.case_id,
            "visa",
            arrangement_confirmation_id=visa.confirmation.confirmation_id,
            guardianship_arrangement_id=None,
            accommodation_arrangement_id=None,
            case_record_link_id=visa.current_record_link.link_id,
            document_id=1600,
            evidence_role="primary",
            **self.edit_arguments(),
        )
        self.arrangements.update_transfer_visa(
            case.case_id, 1401, **self.edit_arguments()
        )
        guardianship = (
            self.arrangements.create_transfer_guardianship_arrangement(
                case.case_id,
                arrangement_mode="parent_guardian",
                guardian_person_name="Parent Example",
                guardian_organisation_name=None,
                relationship_to_student="parent",
                provider_name=None,
                start_date="2027-01-01",
                end_date=None,
                source_type="staff_entered",
                arrangement_status="confirmed",
                notes="Reviewed",
                **self.edit_arguments(),
            )
        )
        self.arrangements.attach_transfer_guardianship_evidence(
            case.case_id,
            guardianship.current_arrangement.arrangement_id,
            case_record_link_id=None,
            document_id=1601,
            evidence_role="authority",
            **self.edit_arguments(),
        )
        html = self.render(
            case.case_id,
            "student_cases.view",
            "student_cases.edit",
        )
        self.assertNotIn("visa_expiry_missing", html)
        self.assertIn("签证 #1401", html)
        self.assertIn("签证 #1400", html)
        self.assertIn("Parent Example", html)
        self.assertIn("Version 1", html)
        self.assertIn("Document #1601", html)
        self.assertIn("primary", html)
        self.assertIn("authority", html)
        self.assertNotIn("还需进入T8完成确认", html)

    def test_arrangement_evidence_displays_creator_name_not_internal_id(self):
        case = self.create_case()
        visa = self.arrangements.update_transfer_visa(
            case.case_id, 1400, **self.edit_arguments()
        )
        self.arrangements.attach_arrangement_evidence(
            case.case_id,
            "visa",
            arrangement_confirmation_id=visa.confirmation.confirmation_id,
            guardianship_arrangement_id=None,
            accommodation_arrangement_id=None,
            case_record_link_id=visa.current_record_link.link_id,
            document_id=1600,
            evidence_role="primary",
            **self.edit_arguments(),
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE users SET display_name='Sonny Zhang',is_active=0 WHERE id=1"
            )

        html = self.render(case.case_id, "student_cases.view")

        self.assertIn("创建人：Sonny Zhang（已停用）", html)
        self.assertNotIn("created by 1", html)
        self.assertNotIn("User #1", html)

    def test_t7c_write_controls_are_hidden_for_paused_and_terminal_cases(self):
        case = self.create_case()
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE transfer_cases SET workflow_state='paused'
                   WHERE case_id=?""",
                (case.case_id,),
            )
            connection.execute(
                """UPDATE student_cases SET status='paused',paused_at='now'
                   WHERE id=?""",
                (case.case_id,),
            )
            connection.commit()
        paused = self.render(
            case.case_id,
            "student_cases.view",
            "student_cases.edit",
        )
        self.assertNotIn('data-arrangement-write', paused)

        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE transfer_cases SET workflow_state='cancelled'
                   WHERE case_id=?""",
                (case.case_id,),
            )
            connection.execute(
                """UPDATE student_cases
                   SET status='cancelled',active_slot=0,
                       terminal_result='cancelled',cancelled_at='now'
                   WHERE id=?""",
                (case.case_id,),
            )
            connection.commit()
        terminal = self.render(
            case.case_id,
            "student_cases.view",
            "student_cases.edit",
        )
        self.assertNotIn('data-arrangement-write', terminal)


@unittest.skipIf(
    transfer_case_views is None,
    "Transfer views module is not implemented yet",
)
class TransferCaseT6AdmissionViewTests(
    TransferCaseT6AdmissionRecordTests
):
    @staticmethod
    def user(*permissions):
        return {"id": 1, "permissions": list(permissions)}

    def render(self, case_id, *permissions):
        return transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            case_id,
            self.user(*permissions),
            self.flags(),
            csrf="csrf-token",
            staff_users=(),
        )

    def test_admission_section_empty_and_read_only_boundaries(self):
        case, _application = self.prepare_final_application()
        editable = self.render(
            case.case_id, "student_cases.view", "student_cases.edit"
        )
        self.assertIn('id="transfer-admission-records"', editable)
        self.assertIn("最终学校核心文件", editable)
        self.assertIn("选择或上传Offer", editable)
        self.assertIn("创建付款要求", editable)
        self.assertIn("当前需要COE", editable)
        admission_editable = editable.split(
            'id="transfer-admission-records"', 1
        )[1].split('id="transfer-arrangements"', 1)[0]
        for forbidden in (
            "关联Visa",
            "关联OSHC",
            "guardianship",
            "accommodation",
            "完成Case",
        ):
            self.assertNotIn(forbidden, admission_editable)
        readonly = self.render(case.case_id, "student_cases.view")
        admission = readonly.split(
            'id="transfer-admission-records"', 1
        )[1].split("</section>", 1)[0]
        self.assertNotIn("<form", admission)
        self.assertNotIn("<button", admission)

    def test_admission_section_renders_offer_proof_history_and_coe(self):
        case, application = self.prepare_final_application()
        self.service.attach_transfer_offer(
            case.case_id,
            application.application_id,
            1000,
            **self.edit_arguments(),
        )
        self.service.replace_transfer_offer(
            case.case_id,
            application.application_id,
            1001,
            **self.edit_arguments(),
        )
        requirement = self.service.create_transfer_payment_requirement(
            case.case_id,
            application.application_id,
            "deposit",
            source_offer_id=1001,
            **self.edit_arguments(),
        )
        self.service.attach_transfer_payment_proof(
            case.case_id,
            application.application_id,
            requirement.requirement_id,
            record_type="payment",
            record_id=1500,
            **self.edit_arguments(),
        )
        self.service.replace_transfer_payment_proof(
            case.case_id,
            application.application_id,
            requirement.requirement_id,
            record_type="student_document",
            record_id=1600,
            **self.edit_arguments(),
        )
        self.service.attach_transfer_coe(
            case.case_id,
            application.application_id,
            1100,
            **self.edit_arguments(),
        )
        html = self.render(
            case.case_id, "student_cases.view", "student_cases.edit"
        )
        for expected in (
            "历史Offer · 已保留",
            "payment-proof.pdf",
            "Target School",
            "付款证明历史",
            "满足状态摘要",
        ):
            self.assertIn(expected, html)
        self.assertIn("/applications/1/offer/historical", html)
        self.assertIn("/applications/1/coe/replace", html)
        self.assertIn(
            f"/payment-requirements/{requirement.requirement_id}/proof",
            html,
        )


@unittest.skipIf(
    transfer_case_views is None,
    "Transfer views module is not implemented yet",
)
class TransferCaseT3ViewTests(TransferCaseRepositoryServiceTests):
    def flags(self, write=True):
        return StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=write,
        )

    @staticmethod
    def user(*permissions):
        return {"id": 1, "permissions": list(permissions)}

    @staticmethod
    def staff():
        return (
            {"id": 1, "display_name": "Admin", "is_current": True},
            {"id": 2, "display_name": "Consultant"},
            {"id": 3, "display_name": "Manager"},
        )

    def create_active_view_case(self, **values):
        draft = self.service.create_transfer_case(**values)
        return lifecycle_support.activate_transfer(
            self.service,
            draft,
            actor_user_id=values.get("actor_user_id", 1),
        )

    def test_create_form_defaults_to_current_user_and_hides_technical_fields(self):
        for current_id in (1, 2, 3):
            staff = tuple(
                {**item, "is_current": item["id"] == current_id}
                for item in self.staff()
            )
            html = transfer_case_views.render_transfer_case_tab(
                self.database_path,
                "student-1",
                self.user("student_cases.view", "student_cases.create"),
                self.flags(),
                csrf="csrf-token",
                staff_users=staff,
                school_choices=({
                    "id": 10,
                    "label": "Example College · Melbourne · VIC",
                    "name": "Example College",
                    "city_key": "melbourne",
                    "sector_key": "private",
                },),
                city_choices=({"key": "melbourne", "name": "Melbourne"},),
            )
            self.assertIn(
                f'<option value="{current_id}" selected>', html
            )
        self.assertIn(
            'class="stack-form compact-case-create-form '
            'transfer-case-form"',
            html,
        )
        for field_class in (
            "transfer-owner-field case-form-span-4",
            "transfer-current-school-field case-form-span-8",
            "transfer-city-field case-form-span-4",
            "transfer-sector-field case-form-span-4",
            "transfer-title-field case-form-span-4",
            "transfer-note-field case-form-span-8",
        ):
            self.assertIn(f'class="{field_class}"', html)
        self.assertIn(
            'class="case-form-actions case-form-span-4"', html
        )
        self.assertIn('data-transfer-school-picker', html)
        self.assertIn('Example College · Melbourne · VIC', html)
        self.assertIn('<option value="melbourne">Melbourne</option>', html)
        self.assertIn('type="hidden" name="current_school_id"', html)
        self.assertIn('type="hidden" name="current_school_name_snapshot"', html)
        self.assertIn('<select name="current_city_key"', html)
        self.assertIn('<option value="" selected>待选择</option>', html)
        self.assertNotIn("当前城市键", html)
        self.assertNotIn('placeholder="melbourne"', html)

    def test_approved_accounts_each_render_as_default_owner(self):
        accounts = (
            (5, "sonnyz", "Sonny Zhang"),
            (2, "TONGY", "Tong Yu"),
            (3, "TEDC", "Ted Chen"),
        )
        for current_id, username, display_name in accounts:
            with self.subTest(username=username):
                staff = [
                    {
                        "id": user_id,
                        "username": account_name,
                        "display_name": account_display,
                        "is_current": user_id == current_id,
                    }
                    for user_id, account_name, account_display in accounts
                ]
                html = transfer_case_views.render_transfer_case_tab(
                    self.database_path,
                    "student-1",
                    self.user("student_cases.view", "student_cases.create"),
                    self.flags(),
                    csrf="csrf-token",
                    staff_users=staff,
                )
                self.assertIn(
                    f'<option value="{current_id}" selected>{display_name}</option>',
                    html,
                )

    def test_detail_hides_completion_diagnostics_and_formats_business_history(self):
        created = self.service.create_transfer_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=1,
            title="Transfer detail",
            current_school_name_snapshot="Current School",
        )
        html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view"),
            self.flags(),
            csrf="csrf-token",
            staff_users=self.staff(),
        )
        self.assertIn("操作记录", html)
        self.assertIn("Admin", html)
        self.assertIn("创建了转学记录", html)
        for forbidden in (
            "Completion Readiness",
            "Completion Summary",
            "Blocking reasons",
            "transfer-readiness-v1",
            "transfer_material_confirmed",
            "User #",
            "Details unavailable",
            "不可修改历史",
        ):
            self.assertNotIn(forbidden, html)

    def test_tab_create_form_uses_only_approved_fields_and_exact_create_gate(self):
        html = transfer_case_views.render_transfer_case_tab(
            self.database_path,
            "student-1",
            self.user("student_cases.view", "student_cases.create"),
            self.flags(),
            csrf="csrf-token",
            staff_users=self.staff(),
        )
        self.assertIn('action="/students/student-1/transfer-cases"', html)
        self.assertIn(">建立转学记录<", html)
        self.assertIn('name="idempotency_key"', html)
        for field_name in (
            "owner_user_id",
            "title",
            "current_school_name_snapshot",
            "current_school_id",
            "current_city_key",
            "current_school_sector",
            "initial_note",
        ):
            self.assertIn(f'name="{field_name}"', html)
        for forbidden in (
            'name="student_id"',
            'name="actor_user_id"',
            'name="case_type"',
            'name="sequence_no"',
            'name="is_active"',
            'name="terminal_result"',
            'name="rule_status"',
            'name="completed_at"',
            'name="final_application_id"',
        ):
            self.assertNotIn(forbidden, html)

        for user, flags in (
            (self.user("student_cases.view", "student_cases.create"), self.flags(False)),
            (self.user("student_cases.view", "*"), self.flags()),
            (self.user("student_cases.view"), self.flags()),
        ):
            denied = transfer_case_views.render_transfer_case_tab(
                self.database_path,
                "student-1",
                user,
                flags,
                csrf="csrf-token",
                staff_users=self.staff(),
            )
            self.assertNotIn(">创建内部转学Case<", denied)

    def test_tab_lists_title_and_hides_create_when_any_active_case_exists(self):
        created = self.service.create_transfer_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            title="Year 10 transfer",
            current_school_name_snapshot="Current School",
        )
        html = transfer_case_views.render_transfer_case_tab(
            self.database_path,
            "student-1",
            self.user("student_cases.view", "student_cases.create"),
            self.flags(),
            csrf="csrf-token",
            staff_users=self.staff(),
        )
        self.assertIn("Year 10 transfer", html)
        self.assertIn(
            f"/students/student-1/transfer-cases/{created.case_id}",
            html,
        )
        self.assertNotIn(">创建内部转学Case<", html)

    def test_history_cards_bulk_load_owner_names_without_numeric_fallbacks(self):
        first = self.service.create_transfer_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=1,
            title="First transfer",
            current_school_name_snapshot="First School",
        )
        self.end_case(first.case_id)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE users SET display_name='Ted Chen',is_active=0 WHERE id=2"
            )
        self.service.create_transfer_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            title="Second transfer",
            current_school_name_snapshot="Second School",
        )
        queries = []
        real_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection

        with patch(
            "staff_display_formatter.sqlite3.connect",
            side_effect=traced_connect,
        ):
            html = transfer_case_views.render_transfer_case_tab(
                self.database_path,
                "student-1",
                self.user("student_cases.view", "student_cases.create"),
                self.flags(),
                csrf="csrf-token",
                staff_users=self.staff(),
            )

        user_selects = [
            query
            for query in queries
            if query.lstrip().upper().startswith("SELECT")
            and " FROM USERS" in query.upper()
        ]
        self.assertEqual(len(user_selects), 1)
        self.assertIn("主负责人</span><strong>Admin", html)
        self.assertIn("主负责人</span><strong>Ted Chen（已停用）", html)
        self.assertNotIn("主负责人</span><strong>1", html)
        self.assertNotIn("主负责人</span><strong>2", html)

    def test_detail_renders_edit_workflow_and_people_controls_independently(self):
        created = self.service.create_transfer_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            title="Transfer detail",
            current_school_name_snapshot="Current School",
        )
        edit_html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view", "student_cases.edit"),
            self.flags(),
            csrf="csrf-token",
            staff_users=self.staff(),
        )
        self.assertIn("Transfer detail", edit_html)
        self.assertIn(
            f'action="/students/student-1/transfer-cases/{created.case_id}/edit"',
            edit_html,
        )
        self.assertIn(
            f'action="/students/student-1/transfer-cases/{created.case_id}/pause"',
            edit_html,
        )
        self.assertNotIn(
            f'action="/students/student-1/transfer-cases/{created.case_id}/workflow"',
            edit_html,
        )
        self.assertNotIn("/owner", edit_html)
        self.assertNotIn("/collaborators", edit_html)
        self.assertNotIn("/watchers", edit_html)

        assign_html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view", "student_cases.assign"),
            self.flags(),
            csrf="csrf-token",
            staff_users=self.staff(),
        )
        self.assertNotIn(f"/{created.case_id}/edit", assign_html)
        self.assertNotIn(f"/{created.case_id}/workflow", assign_html)
        self.assertIn(f"/{created.case_id}/owner", assign_html)
        self.assertIn(f"/{created.case_id}/collaborators", assign_html)
        self.assertIn(f"/{created.case_id}/watchers", assign_html)

    def test_detail_renders_current_people_removal_and_append_only_event_history(self):
        created = self.create_active_view_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            title="History transfer",
            current_school_name_snapshot="Current School",
            initial_note="Original note remains immutable",
        )
        student_case_service.add_collaborator(
            self.database_path,
            case_id=created.case_id,
            user_id=1,
            actor_user_id=1,
        )
        student_case_service.add_watcher(
            self.database_path,
            case_id=created.case_id,
            user_id=2,
            actor_user_id=1,
        )
        html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view", "student_cases.assign"),
            self.flags(),
            csrf="csrf-token",
            staff_users=self.staff(),
        )
        self.assertIn(
            f'action="/students/student-1/transfer-cases/'
            f'{created.case_id}/collaborators/1/remove"',
            html,
        )
        self.assertIn(
            f'action="/students/student-1/transfer-cases/'
            f'{created.case_id}/watchers/2/remove"',
            html,
        )
        self.assertIn("移除协办人", html)
        self.assertIn("移除关注人", html)
        self.assertIn("Admin创建了转学记录", html)
        self.assertIn("Admin更新了负责人或协作人员", html)
        self.assertNotIn("transfer_case_created", html)
        self.assertNotIn("assignment_added", html)
        self.assertNotIn('name="initial_note"', html)

    def test_detail_bulk_loads_disabled_owner_people_and_event_names_once(self):
        created = self.service.create_transfer_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            title="Staff display transfer",
            current_school_name_snapshot="Current School",
        )
        student_case_service.add_collaborator(
            self.database_path,
            case_id=created.case_id,
            user_id=1,
            actor_user_id=1,
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE users SET display_name='Ted Chen',is_active=0 WHERE id=2"
            )
        queries = []
        real_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection

        with patch(
            "staff_display_formatter.sqlite3.connect",
            side_effect=traced_connect,
        ):
            html = transfer_case_views.render_transfer_case_detail(
                self.database_path,
                "student-1",
                created.case_id,
                self.user("student_cases.view", "student_cases.assign"),
                self.flags(),
                csrf="csrf-token",
                staff_users=self.staff(),
            )

        user_selects = [
            query
            for query in queries
            if query.lstrip().upper().startswith("SELECT")
            and " FROM USERS" in query.upper()
        ]
        self.assertEqual(len(user_selects), 1)
        self.assertIn("主负责人</span><strong>Ted Chen（已停用）", html)
        self.assertIn("Admin创建了转学记录", html)
        self.assertIn("Admin更新了负责人或协作人员", html)
        self.assertNotIn("User #", html)
        self.assertNotIn("主负责人</span><strong>2", html)

    def test_material_confirmers_use_shared_staff_map_without_numeric_fallbacks(self):
        created = self.create_active_view_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            title="Material staff display",
            current_school_name_snapshot="Current School",
        )
        materials = self.service.ensure_transfer_material_template(
            created.case_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        for material, document_id, actor_user_id in (
            (materials[0], 1600, 1),
            (materials[1], 1601, 2),
        ):
            self.service.attach_transfer_material_document(
                created.case_id,
                material.material_id,
                document_id,
                actor_user_id=actor_user_id,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            )
            self.service.confirm_transfer_material_item(
                created.case_id,
                material.material_id,
                actor_user_id=actor_user_id,
                actor_permissions={"student_cases.edit"},
                flags=self.flags(),
            )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE users SET display_name='Ted Chen',is_active=0 WHERE id=2"
            )
        queries = []
        real_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection

        with patch(
            "staff_display_formatter.sqlite3.connect",
            side_effect=traced_connect,
        ):
            html = transfer_case_views.render_transfer_case_detail(
                self.database_path,
                "student-1",
                created.case_id,
                self.user("student_cases.view"),
                self.flags(),
                csrf="csrf-token",
            )

        user_selects = [
            query
            for query in queries
            if query.lstrip().upper().startswith("SELECT")
            and " FROM USERS" in query.upper()
        ]
        self.assertEqual(len(user_selects), 1)
        self.assertIn("确认人：Admin", html)
        self.assertIn("确认人：Ted Chen（已停用）", html)
        self.assertIsNone(re.search(r"确认人[：:]\s*\d+", html))
        self.assertIn("主负责人</span><strong>Ted Chen（已停用）", html)
        self.assertIn("创建人</span><strong>Admin", html)
        self.assertIn("更新人</span><strong>Admin", html)
        self.assertIn("Admin创建了转学记录", html)

    def test_material_confirmer_system_and_missing_user_use_safe_labels(self):
        created = self.create_active_view_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            current_school_name_snapshot="Current School",
        )
        material = self.service.ensure_transfer_material_template(
            created.case_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )[0]
        summary = self.service.get_transfer_material_summary(created.case_id)
        system_material = replace(
            material,
            confirmed_by=None,
            confirmed_at="2026-08-04T10:00:00+10:00",
        )
        missing_user_material = replace(
            material,
            material_id=material.material_id + 1000,
            confirmed_by=999,
            confirmed_at="2026-08-04T10:01:00+10:00",
        )

        html = transfer_case_views._material_section(
            "student-1",
            created,
            (system_material, missing_user_material),
            summary,
            (),
            "csrf-token",
            False,
            staff_map={},
        )

        self.assertIn("确认人：系统", html)
        self.assertIn("确认人：未知员工", html)
        self.assertIsNone(re.search(r"确认人[：:]\s*\d+", html))

    def test_completion_history_uses_shared_disabled_staff_label(self):
        created = self.service.create_transfer_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=1,
            current_school_name_snapshot="Current School",
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE users SET display_name='Sonny Zhang',is_active=0 WHERE id=1"
            )
            TransferPhase2MigrationTests.insert_completion_confirmation(
                connection,
                case_id=created.case_id,
                idempotency_key="staff-display-completion",
            )

        history = self.service.list_transfer_completion_history(created.case_id)

        self.assertEqual(history[0]["actor_display"], "Sonny Zhang（已停用）")
        self.assertNotIn("User #", history[0]["actor_display"])

    def test_legacy_event_summary_uses_staff_map_without_user_number(self):
        event = SimpleNamespace(
            actor_user_id=2,
            event_type="transfer_case_updated",
            metadata={},
            old_value={},
            new_value={},
        )

        html = transfer_case_views._transfer_event_history_summary(
            event, {2: "Ted Chen"}
        )

        self.assertIn("操作人：Ted Chen", html)
        self.assertNotIn("User #", html)
        self.assertNotIn("Details unavailable", html)

    def test_detail_has_school_applications_but_no_completion_or_record_link_entry(self):
        created = self.create_active_view_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            current_school_name_snapshot="Current School",
        )
        html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user(
                "student_cases.view",
                "student_cases.create",
                "student_cases.edit",
                "student_cases.assign",
                "student_cases.complete",
                "student_cases.link_records",
            ),
            self.flags(),
            csrf="csrf-token",
            staff_users=self.staff(),
        )
        for forbidden in (
            ">完成Case<",
            "撤销完成",
            "关联Offer",
            "关联COE",
            "关联Visa",
            "关联OSHC",
            "关联Payment",
            "/record-links",
        ):
            self.assertNotIn(forbidden, html)
        self.assertIn("＋ 添加目标学校", html)
        self.assertIn(
            f"/transfer-cases/{created.case_id}/applications", html
        )
        self.assertNotIn('method="put"', html.lower())
        self.assertNotIn('method="patch"', html.lower())
        self.assertNotIn('method="delete"', html.lower())

    def test_target_school_form_reuses_canonical_school_picker(self):
        created = self.create_active_view_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            current_school_name_snapshot="Current School",
        )
        html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view", "student_cases.edit"),
            self.flags(),
            csrf="csrf-token",
            staff_users=self.staff(),
            school_choices=({
                "id": 10,
                "label": "Target College · Melbourne · VIC",
                "name": "Target College",
                "city_key": "melbourne",
                "sector_key": "private",
            },),
            city_choices=({"key": "melbourne", "name": "Melbourne"},),
        )
        self.assertIn('data-transfer-school-context="target"', html)
        self.assertIn('type="hidden" name="school_id"', html)
        self.assertIn('type="hidden" name="school_name_snapshot"', html)
        self.assertIn('name="city_key" data-transfer-city', html)
        self.assertIn('name="school_sector" data-transfer-school-sector', html)
        self.assertIn("Target College · Melbourne · VIC", html)
        self.assertNotIn("学校ID（可选）", html)
        self.assertNotIn("城市键（可选）", html)

    def test_transfer_business_labels_keep_protected_arrangements(self):
        created = self.service.create_transfer_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            current_school_name_snapshot="Current School",
        )
        html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view", "student_cases.edit"),
            self.flags(),
            csrf="csrf-token",
            staff_users=self.staff(),
        )
        self.assertIn(">暂停转学</button>", html)
        self.assertIn("<h3>转学后续文件</h3>", html)
        self.assertIn('data-arrangement-type="guardianship"', html)
        self.assertIn('data-arrangement-type="accommodation"', html)
        self.assertNotIn(">进入准备中</button>", html)
        self.assertNotIn("<h3>安排确认</h3>", html)

        self.service.ensure_transfer_material_template(
            created.case_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        material = next(
            item for item in self.service.list_transfer_material_items(created.case_id)
            if item.material_key == "current_visa"
        )
        self.service.attach_transfer_material_document(
            created.case_id,
            material.material_id,
            1600,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        material_html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view", "student_cases.edit"),
            self.flags(),
            csrf="csrf-token",
            staff_users=self.staff(),
        )
        self.assertIn(">确认材料</button>", material_html)
        self.assertNotIn("确认当前关联文件满足本Case材料要求？", material_html)

    def test_detail_rejects_cross_student_and_escapes_title(self):
        created = self.service.create_transfer_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            title="<script>alert(1)</script>",
            current_school_name_snapshot="Current School",
        )
        with self.assertRaises(
            transfer_case_errors.TransferCaseNotFoundError
        ):
            transfer_case_views.render_transfer_case_detail(
                self.database_path,
                "student-2",
                created.case_id,
                self.user("student_cases.view"),
                self.flags(),
                csrf="csrf-token",
                staff_users=self.staff(),
            )
        html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view"),
            self.flags(),
            csrf="csrf-token",
            staff_users=self.staff(),
        )
        self.assertNotIn("<script>", html)

    def test_transfer_css_is_scoped_and_has_desktop_and_mobile_form_layouts(self):
        css = (PROJECT_ROOT / "assets" / "student-case.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            ".progression-workspace .compact-case-create-form,\n"
            ".student-case-section .compact-case-create-form {\n"
            "  grid-template-columns: repeat(12, minmax(0, 1fr));",
            css,
        )
        self.assertIn("@media (max-width: 980px)", css)
        self.assertIn(
            ".progression-workspace .compact-case-create-form "
            ".case-form-actions,\n"
            "  .student-case-section .compact-case-create-form "
            ".case-form-actions {\n"
            "    grid-column: 1 / -1;",
            css,
        )
        self.assertIn("@media (max-width: 720px)", css)
        self.assertIn(
            ".student-case-section .compact-case-create-form {\n"
            "    grid-template-columns: minmax(0, 1fr);",
            css,
        )
        for selector in (
            ".transfer-case-form",
            ".compact-case-create-form",
            ".case-form-span-4",
            ".transfer-person-row",
            ".transfer-event-row",
            ".transfer-workflow-form",
        ):
            self.assertNotRegex(
                css,
                rf"(?m)^{re.escape(selector)}\\s*\\{{",
            )

    def test_t4_application_section_is_readable_and_exact_edit_gated(self):
        created = self.create_active_view_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            current_school_name_snapshot="Current School",
        )
        application = self.service.add_transfer_school_application(
            created.case_id,
            school_id=None,
            school_name_snapshot="Melbourne Target College",
            institution_group_id=None,
            city_key="melbourne",
            campus_key="city",
            school_sector="private",
            expected_start_date="2027-02-01",
            preference_order=None,
            application_status="preparing",
            notes="Preferred option",
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.service.set_final_transfer_school_application(
            created.case_id,
            application.application_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        view_html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view"),
            self.flags(),
            csrf="csrf-token",
        )
        self.assertIn("Melbourne Target College", view_html)
        self.assertIn("最终学校", view_html)
        self.assertNotIn("＋ 添加目标学校", view_html)
        self.assertNotIn("data-dialog-open", view_html)
        edit_html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view", "student_cases.edit"),
            self.flags(),
            csrf="csrf-token",
        )
        for label in (
            "＋ 添加目标学校",
            "编辑",
            "更新状态",
            "调整优先顺序",
            "取消最终学校",
        ):
            self.assertIn(label, edit_html)
        application_html = edit_html.split(
            'id="transfer-school-applications"', 1
        )[1].split('id="transfer-admission-records"', 1)[0]
        for forbidden in (
            ">付款<",
            ">Visa<",
            ">OSHC<",
            "删除申请",
        ):
            self.assertNotIn(forbidden, application_html)

    def test_t4_application_section_hides_all_writes_when_paused(self):
        created = self.create_active_view_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            current_school_name_snapshot="Current School",
        )
        self.service.pause_transfer_case(created.case_id, actor_user_id=1)
        html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view", "student_cases.edit"),
            self.flags(),
            csrf="csrf-token",
        )
        self.assertIn("目标学校申请", html)
        application_html = html.split(
            'id="transfer-school-applications"', 1
        )[1].split('id="transfer-admission-records"', 1)[0]
        self.assertNotIn("＋ 添加目标学校", application_html)
        self.assertNotIn("data-dialog-open", application_html)

    def test_t4_terminal_application_has_no_status_change_control(self):
        created = self.create_active_view_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            current_school_name_snapshot="Current School",
        )
        application = self.service.add_transfer_school_application(
            created.case_id,
            school_id=None,
            school_name_snapshot="Closed Target",
            institution_group_id=None,
            city_key="melbourne",
            campus_key=None,
            school_sector="private",
            expected_start_date=None,
            preference_order=None,
            application_status="preparing",
            notes=None,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.service.change_transfer_school_application_status(
            created.case_id,
            application.application_id,
            "rejected",
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view", "student_cases.edit"),
            self.flags(),
            csrf="csrf-token",
        )
        self.assertIn("Closed Target", html)
        self.assertNotIn(
            f'data-dialog-open="status-transfer-application-'
            f'{application.application_id}"',
            html,
        )
        self.assertNotIn(
            f'id="status-transfer-application-{application.application_id}"',
            html,
        )

    def test_t5_material_section_is_readable_and_exact_edit_gated(self):
        created = self.create_active_view_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            current_school_name_snapshot="Current School",
        )
        view_html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view"),
            self.flags(),
            csrf="csrf-token",
        )
        self.assertIn("材料清单", view_html)
        self.assertIn("尚未初始化标准材料清单", view_html)
        self.assertNotIn("初始化标准清单", view_html)
        edit_html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view", "student_cases.edit"),
            self.flags(),
            csrf="csrf-token",
        )
        self.assertIn("初始化标准清单", edit_html)
        self.assertIn("添加自定义材料", edit_html)

    def test_t5_material_section_lists_summary_documents_and_safe_controls(self):
        created = self.create_active_view_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            current_school_name_snapshot="Current School",
        )
        materials = self.service.ensure_transfer_material_template(
            created.case_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.service.attach_transfer_material_document(
            created.case_id,
            materials[0].material_id,
            1600,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.service.confirm_transfer_material_item(
            created.case_id,
            materials[0].material_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.service.change_transfer_material_status(
            created.case_id,
            materials[1].material_id,
            "needs_update",
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.service.attach_transfer_material_document(
            created.case_id,
            materials[2].material_id,
            1601,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view", "student_cases.edit"),
            self.flags(),
            csrf="csrf-token",
        )
        for expected in (
            "Current school report",
            "已确认",
            "需要更新",
            "Document #1600",
            "必需材料",
            "完成比例",
            "编辑材料",
            "更改状态",
            "选择已有文件",
            "解除文件关联",
            "确认材料",
        ):
            self.assertIn(expected, html)
        material_html = html.split(
            'id="transfer-materials"', 1
        )[1].split('id="transfer-applications"', 1)[0]
        for forbidden in (
            "上传新文件",
            "删除student_documents",
            "关联Offer",
            "关联COE",
            "关联Visa",
            "关联Payment",
            ">完成Case<",
        ):
            self.assertNotIn(forbidden, material_html)

    def test_t5_material_section_hides_all_writes_when_paused(self):
        created = self.create_active_view_case(
            student_id="student-1",
            actor_user_id=1,
            owner_user_id=2,
            current_school_name_snapshot="Current School",
        )
        self.service.ensure_transfer_material_template(
            created.case_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        self.service.pause_transfer_case(created.case_id, actor_user_id=1)
        html = transfer_case_views.render_transfer_case_detail(
            self.database_path,
            "student-1",
            created.case_id,
            self.user("student_cases.view", "student_cases.edit"),
            self.flags(),
            csrf="csrf-token",
        )
        self.assertIn("材料清单", html)
        self.assertNotIn("初始化标准清单", html)
        self.assertNotIn("添加自定义材料", html)
        self.assertNotIn("编辑材料", html)
        self.assertNotIn("选择已有文件", html)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, url):
        return None


@unittest.skipIf(
    transfer_case_views is None,
    "Transfer views module is not implemented yet",
)
class TransferCaseT3RouteTests(TransferCaseRepositoryServiceTests):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TABLE audit_logs(
                    id INTEGER PRIMARY KEY,user_id INTEGER,username TEXT,
                    action_type TEXT,module_name TEXT,module_key TEXT,
                    record_id TEXT,file_path TEXT,old_value TEXT,new_value TEXT,
                    student_id TEXT,contract_id TEXT,ip_address TEXT,
                    user_agent TEXT,device_info TEXT,status TEXT,
                    error_message TEXT,created_at TEXT
                )"""
            )
            connection.commit()
        self.app = importlib.import_module("app")
        self.database_module = importlib.import_module("database")
        self.original_db = self.app.db
        self.original_nas = self.app.nas
        self.original_backup = self.app.maybe_run_auto_backup
        self.app.db = self.database_module.Database(self.database_path)

        class NoNas:
            @staticmethod
            def status():
                return {"ready": False, "message": "isolated test"}

        self.app.nas = NoNas()
        self.app.maybe_run_auto_backup = lambda: None
        app_module = self.app

        class RouteHandler(app_module.CRMHandler):
            route_user = {
                "id": 1,
                "username": "admin",
                "display_name": "Admin",
                "role_name": "Admin",
                "permissions": [
                    "student_cases.view",
                    "student_cases.create",
                    "student_cases.edit",
                    "student_cases.assign",
                ],
                "permissions_json": json.dumps(
                    [
                        "students.view",
                        "student_cases.view",
                        "student_cases.create",
                        "student_cases.edit",
                        "student_cases.assign",
                    ]
                ),
            }
            student_case_feature_flags = StudentCaseFeatureFlags(
                read_enabled=True,
                transfer_write_enabled=True,
            )

            def log_message(self, _format, *_args):
                return None

            def current_user(self):
                return dict(type(self).route_user), "route-csrf"

            def touch_current_session(self):
                return None

            def transfer_case_staff_users(self):
                return [
                    {"id": 1, "display_name": "Admin", "is_current": True},
                    {"id": 2, "display_name": "Consultant"},
                ]

            def transfer_case_school_choices(self):
                return [
                    {
                        "id": 10,
                        "label": "Current School · Melbourne · VIC",
                        "name": "Current School",
                        "city_key": "melbourne",
                        "city_name": "Melbourne",
                        "sector_key": "public",
                        "state": "VIC",
                        "suburb": "Melbourne",
                        "postcode": "3000",
                    },
                    {
                        "id": 20,
                        "label": "School B · Melbourne · VIC",
                        "name": "School B",
                        "city_key": "melbourne",
                        "city_name": "Melbourne",
                        "sector_key": "private",
                        "state": "VIC",
                        "suburb": "Melbourne",
                        "postcode": "3000",
                    },
                ]

            def transfer_case_city_choices(self):
                return [
                    {"key": "melbourne", "name": "Melbourne"},
                    {"key": "sydney", "name": "Sydney"},
                ]

        self.handler_class = RouteHandler
        self.server = self.app.ReusableThreadingHTTPServer(
            ("127.0.0.1", 0), RouteHandler
        )
        self.server.service_mode = "admin"
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base_url = (
            f"http://127.0.0.1:{self.server.server_port}"
        )
        self.opener = urllib.request.build_opener(_NoRedirect())

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.app.db = self.original_db
        self.app.nas = self.original_nas
        self.app.maybe_run_auto_backup = self.original_backup
        super().tearDown()

    def request(self, path, *, form=None):
        data = None
        method = "GET"
        if form is not None:
            payload = {"csrf": "route-csrf", **form}
            data = urllib.parse.urlencode(payload).encode()
            method = "POST"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
        )
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        return (
            response.code,
            response.headers,
            response.read().decode("utf-8"),
        )

    def ensure_route_activation_authority(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            contract_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info('contracts')")
            }
            if "final_signed_file_path" not in contract_columns:
                connection.execute(
                    "ALTER TABLE contracts ADD COLUMN final_signed_file_path TEXT"
                )
            connection.execute(
                """INSERT OR IGNORE INTO student_school_enrolments(
                       id,student_id,school_id,school_name_snapshot,city_key,
                       school_sector,enrolment_role,status,created_by,created_at,
                       updated_by,updated_at)
                   VALUES(3001,'1001',10,'Current School','melbourne','government',
                          'current','active',1,'2026-08-20T00:00:00Z',
                          1,'2026-08-20T00:00:00Z')"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO contracts(
                       id,student_id,contract_type,contract_status,deleted_at,
                       voided_at,application_cycle_id,final_signed_file_path)
                   VALUES(3001,'1001','TransferApplicationContract',
                          'FullyExecuted',NULL,NULL,NULL,
                          '/isolated-fixture/final-transfer-contract.pdf')"""
            )
            connection.commit()

    def create_route_case(self, *, activate=True):
        self.ensure_route_activation_authority()
        status, headers, _body = self.request(
            "/students/1001/transfer-cases",
            form={
                "owner_user_id": "2",
                "title": "Route transfer",
                "current_school_name_snapshot": "Current School",
                "current_school_id": "10",
                "current_city_key": "melbourne",
                "current_school_sector": "public",
                "initial_note": "Created through route",
                "source_contract_id": "3001",
            },
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        with closing(self.connect(self.database_path)) as connection:
            case_id = connection.execute(
                """SELECT id FROM student_cases
                   WHERE student_id='1001' AND case_type='transfer'"""
            ).fetchone()[0]
        if activate:
            status, _activation_headers, _body = self.request(
                f"/students/1001/transfer-cases/{case_id}/activate",
                form={
                    "idempotency_key":
                        f"route-fixture-activate-transfer-{case_id}"
                },
            )
            self.assertEqual(status, HTTPStatus.SEE_OTHER)
        return case_id, headers["Location"]

    def test_explicit_activation_route_is_idempotent_and_binds_one_cycle(self):
        case_id, _location = self.create_route_case(activate=False)

        for _attempt in range(2):
            status, headers, _body = self.request(
                f"/students/1001/transfer-cases/{case_id}/activate",
                form={"idempotency_key": "route-activate-transfer"},
            )
            self.assertEqual(status, HTTPStatus.SEE_OTHER)
            self.assertIn(f"/transfer-cases/{case_id}", headers["Location"])

        with closing(self.connect(self.database_path)) as connection:
            self.assertEqual(("preparing", 1), tuple(connection.execute(
                "SELECT status,active_slot FROM student_cases WHERE id=?",
                (case_id,),
            ).fetchone()))
            self.assertEqual(1, connection.execute(
                """SELECT count(*) FROM application_cycles
                   WHERE source_student_case_id=? AND status='active'""",
                (case_id,),
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                """SELECT count(*) FROM student_lifecycle_idempotency_receipts
                   WHERE result_case_id=? AND operation='activate'""",
                (case_id,),
            ).fetchone()[0])

    def test_real_routes_create_detail_and_preserve_scroll_anchor(self):
        status, _headers, body = self.request(
            "/students/1001/transfer-cases"
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIn('id="transfer-case-create"', body)
        original_user = self.handler_class.route_user
        self.handler_class.route_user = {
            **original_user,
            "permissions": ["students.view"],
            "permissions_json": '["students.view"]',
        }
        status, _headers, _body = self.request(
            "/students/1001/transfer-cases"
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.handler_class.route_user = original_user
        case_id, location = self.create_route_case()
        self.assertIn("/students/1001?", location)
        self.assertTrue(location.endswith("#workflow-progress"))
        status, _headers, body = self.request(
            f"/students/1001/transfer-cases/{case_id}"
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIn("Lifecycle / Case 详情与历史摘要", body)
        self.assertIn("返回 Student Profile", body)
        self.assertNotIn('class="transfer-workbench', body)
        with closing(self.connect(self.database_path)) as connection:
            title = connection.execute(
                "SELECT title FROM transfer_cases WHERE case_id=?",
                (case_id,),
            ).fetchone()[0]
            event = connection.execute(
                """SELECT metadata_json FROM case_events
                   WHERE case_id=? AND event_type='transfer_case_created'""",
                (case_id,),
            ).fetchone()[0]
        self.assertEqual(title, "Route transfer")
        self.assertEqual(
            json.loads(event)["initial_note"],
            "Created through route",
        )

    def test_create_rejects_ineligible_owner_without_writing_case(self):
        status, _headers, body = self.request(
            "/students/1001/transfer-cases",
            form={
                "owner_user_id": "99",
                "current_school_name_snapshot": "Current School",
                "current_school_id": "10",
                "current_city_key": "melbourne",
                "current_school_sector": "public",
            },
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertIn("所选负责人无Transfer分配权限", body)
        with closing(self.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM student_cases WHERE student_id='1001'"
                ).fetchone()[0],
                0,
            )

    def test_create_rejects_school_name_identifier_mismatch(self):
        status, _headers, _body = self.request(
            "/students/1001/transfer-cases",
            form={
                "owner_user_id": "2",
                "current_school_name_snapshot": "Different School",
                "current_school_id": "10",
                "current_city_key": "melbourne",
                "current_school_sector": "public",
            },
        )
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        with closing(self.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM student_cases WHERE student_id='1001'"
                ).fetchone()[0],
                0,
            )

    def test_target_school_route_rejects_forged_id_name_pair(self):
        case_id, _location = self.create_route_case()
        status, _headers, _body = self.request(
            f"/students/1001/transfer-cases/{case_id}/applications",
            form={
                "school_id": "10",
                "school_name_snapshot": "Forged Target School",
                "city_key": "melbourne",
                "school_sector": "private",
                "application_status": "preparing",
            },
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        with closing(self.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM transfer_school_applications WHERE case_id=?",
                    (case_id,),
                ).fetchone()[0],
                0,
            )

    def test_owner_change_rejects_ineligible_post_target(self):
        case_id, _location = self.create_route_case()
        status, _headers, body = self.request(
            f"/students/1001/transfer-cases/{case_id}/owner",
            form={"user_id": "99"},
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertIn("所选员工无Transfer分配权限", body)
        self.assertEqual(
            self.service.get_transfer_case(case_id).owner_user_id,
            2,
        )

    def test_real_routes_edit_workflow_owner_collaborator_and_watcher(self):
        case_id, _location = self.create_route_case()
        actions = (
            (
                f"/students/1001/transfer-cases/{case_id}/edit",
                {
                    "title": "Edited route transfer",
                    "current_school_name_snapshot": "Current School",
                    "current_school_id": "10",
                    "current_city_key": "sydney",
                    "current_school_sector": "private",
                },
                "#transfer-case-overview",
            ),
            (
                f"/students/1001/transfer-cases/{case_id}/workflow",
                {"target_state": "preparing"},
                "#transfer-case-workflow",
            ),
            (
                f"/students/1001/transfer-cases/{case_id}/owner",
                {"user_id": "1"},
                "#transfer-case-people",
            ),
            (
                f"/students/1001/transfer-cases/{case_id}/collaborators",
                {"user_id": "2"},
                "#transfer-case-people",
            ),
            (
                f"/students/1001/transfer-cases/{case_id}/watchers",
                {"user_id": "2"},
                "#transfer-case-people",
            ),
            (
                f"/students/1001/transfer-cases/{case_id}/collaborators/2/remove",
                {},
                "#transfer-case-people",
            ),
            (
                f"/students/1001/transfer-cases/{case_id}/watchers/2/remove",
                {},
                "#transfer-case-people",
            ),
        )
        for path, form, anchor in actions:
            with self.subTest(path=path):
                status, headers, _body = self.request(path, form=form)
                self.assertEqual(status, HTTPStatus.SEE_OTHER)
                self.assertTrue(headers["Location"].endswith(anchor))
        current = self.service.get_transfer_case(case_id)
        self.assertEqual(
            (
                current.title,
                current.current_school_name_snapshot,
                current.current_city_key,
                current.current_school_sector,
                current.workflow_state,
                current.owner_user_id,
            ),
            (
                "Edited route transfer",
                "Current School",
                "sydney",
                "private",
                "preparing",
                1,
            ),
        )

    def test_real_routes_reject_cross_student_flag_off_wildcard_and_bad_csrf(self):
        case_id, _location = self.create_route_case()
        status, _headers, _body = self.request(
            f"/students/1002/transfer-cases/{case_id}"
        )
        self.assertEqual(status, HTTPStatus.NOT_FOUND)

        self.handler_class.student_case_feature_flags = (
            StudentCaseFeatureFlags(read_enabled=True)
        )
        status, _headers, _body = self.request(
            "/students/1002/transfer-cases",
            form={
                "owner_user_id": "2",
                "current_school_name_snapshot": "School",
            },
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)

        self.handler_class.student_case_feature_flags = (
            StudentCaseFeatureFlags(
                read_enabled=True,
                transfer_write_enabled=True,
            )
        )
        self.handler_class.route_user = {
            "id": 1,
            "username": "admin",
            "display_name": "Admin",
            "role_name": "Admin",
            "permissions": ["*"],
            "permissions_json": '["*"]',
        }
        status, _headers, _body = self.request(
            "/students/1002/transfer-cases",
            form={
                "owner_user_id": "2",
                "current_school_name_snapshot": "School",
            },
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)

        self.handler_class.route_user = {
            **self.handler_class.route_user,
            "permissions": [
                "student_cases.view",
                "student_cases.create",
            ],
            "permissions_json": json.dumps(
                [
                    "students.view",
                    "student_cases.view",
                    "student_cases.create",
                ]
            ),
        }
        status, _headers, _body = self.request(
            "/students/1002/transfer-cases",
            form={
                "owner_user_id": "2",
                "current_school_name_snapshot": "School",
                "student_id": "1001",
            },
        )
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        with closing(self.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM student_cases
                       WHERE student_id='1002'"""
                ).fetchone()[0],
                0,
            )

        request = urllib.request.Request(
            self.base_url + "/students/1002/transfer-cases",
            data=urllib.parse.urlencode(
                {
                    "csrf": "wrong",
                    "owner_user_id": "2",
                    "current_school_name_snapshot": "School",
                }
            ).encode(),
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, HTTPStatus.FORBIDDEN)

    def test_t4_real_routes_add_edit_status_reorder_final_and_clear(self):
        case_id, _location = self.create_route_case()
        base = f"/students/1001/transfer-cases/{case_id}/applications"
        for school_id, name in ((10, "Current School"), (20, "School B")):
            status, headers, _body = self.request(
                base,
                form={
                    "request_id": f"route-{name}",
                    "school_id": str(school_id),
                    "school_name_snapshot": name,
                    "institution_group_id": "",
                    "city_key": "melbourne",
                    "campus_key": "",
                    "school_sector": "private",
                    "expected_start_date": "2027-02-01",
                    "preference_order": "",
                    "application_status": "preparing",
                    "notes": "",
                },
            )
            self.assertEqual(status, HTTPStatus.SEE_OTHER)
            self.assertTrue(
                headers["Location"].endswith(
                    "#transfer-school-applications"
                )
            )
        applications = self.service.list_transfer_school_applications(case_id)
        first, second = applications
        actions = (
            (
                f"{base}/{first.application_id}/edit",
                {
                    "request_id": "route-edit",
                    "school_id": "10",
                    "school_name_snapshot": "Current School",
                    "institution_group_id": "",
                    "city_key": "sydney",
                    "campus_key": "city",
                    "school_sector": "public",
                    "expected_start_date": "2027-03-01",
                    "notes": "Updated",
                },
            ),
            (
                f"{base}/{first.application_id}/status",
                {
                    "request_id": "route-status",
                    "application_status": "submitted",
                },
            ),
            (
                f"{base}/reorder",
                {
                    "request_id": "route-reorder",
                    "application_ids": (
                        f"{second.application_id},{first.application_id}"
                    ),
                },
            ),
            (
                f"{base}/{second.application_id}/final",
                {"request_id": "route-final"},
            ),
            (
                f"{base}/final/clear",
                {"request_id": "route-clear"},
            ),
        )
        for path, form in actions:
            with self.subTest(path=path):
                status, headers, _body = self.request(path, form=form)
                self.assertEqual(status, HTTPStatus.SEE_OTHER)
                self.assertTrue(
                    headers["Location"].endswith(
                        "#transfer-school-applications"
                    )
                )
        rows = self.service.list_transfer_school_applications(case_id)
        self.assertEqual(
            [row.application_id for row in rows],
            [second.application_id, first.application_id],
        )
        self.assertFalse(any(row.is_final for row in rows))
        with closing(self.connect(self.database_path)) as connection:
            events = {
                row[0]
                for row in connection.execute(
                    """SELECT event_type FROM case_events
                       WHERE case_id=?""",
                    (case_id,),
                )
            }
        self.assertTrue(
            {
                "transfer_school_application_added",
                "transfer_school_application_updated",
                "transfer_school_application_status_changed",
                "transfer_school_applications_reordered",
                "transfer_final_school_set",
                "transfer_final_school_cleared",
                "transfer_rule_calculated",
            }.issubset(events)
        )

    def test_t4_route_rejects_cross_student_and_forbidden_fields(self):
        case_id, _location = self.create_route_case()
        base = f"/students/1002/transfer-cases/{case_id}/applications"
        status, _headers, _body = self.request(
            base,
            form={
                "school_name_snapshot": "Cross student",
                "application_status": "preparing",
            },
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        with closing(self.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM transfer_school_applications
                       WHERE case_id=?""",
                    (case_id,),
                ).fetchone()[0],
                0,
            )
        status, _headers, _body = self.request(
            f"/students/1001/transfer-cases/{case_id}/applications",
            form={
                "school_name_snapshot": "Injected",
                "application_status": "preparing",
                "is_final": "1",
            },
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.assertEqual(
            self.counts("transfer_school_applications")[
                "transfer_school_applications"
            ],
            0,
        )

    def test_t5_real_routes_cover_material_lifecycle_and_anchor(self):
        case_id, _location = self.create_route_case()
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                """INSERT INTO student_documents(id,student_id)
                   VALUES(3600,'1001'),(3601,'1001')"""
            )
            connection.commit()
        base = f"/students/1001/transfer-cases/{case_id}/materials"
        status, headers, _body = self.request(
            f"{base}/initialize",
            form={"request_id": "route-material-template"},
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.assertTrue(headers["Location"].endswith("#transfer-materials"))
        status, headers, _body = self.request(
            base,
            form={
                "request_id": "route-material-add",
                "material_key": "custom_route_item",
                "material_label_snapshot": "Route material",
                "is_required": "1",
                "initial_status": "missing",
                "document_id": "",
            },
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        materials = self.service.list_transfer_material_items(case_id)
        material = next(
            item for item in materials
            if item.material_key == "custom_route_item"
        )
        actions = (
            (
                f"{base}/{material.material_id}/edit",
                {
                    "request_id": "route-material-edit",
                    "material_label_snapshot": "Route material updated",
                    "is_required": "0",
                },
            ),
            (
                f"{base}/{material.material_id}/status",
                {
                    "request_id": "route-material-status",
                    "material_status": "needs_update",
                    "reason": "Needs a clearer copy",
                },
            ),
            (
                f"{base}/{material.material_id}/document",
                {
                    "request_id": "route-material-document",
                    "document_id": "3600",
                },
            ),
            (
                f"{base}/{material.material_id}/confirm",
                {"request_id": "route-material-confirm"},
            ),
            (
                f"{base}/{material.material_id}/document/detach",
                {"request_id": "route-material-detach"},
            ),
        )
        for path, form in actions:
            with self.subTest(path=path):
                status, headers, _body = self.request(path, form=form)
                self.assertEqual(status, HTTPStatus.SEE_OTHER)
                self.assertTrue(
                    headers["Location"].endswith("#transfer-materials")
                )
        current = self.service.get_transfer_material_item(
            case_id, material.material_id
        )
        self.assertEqual(
            current.material_label_snapshot, "Route material updated"
        )
        self.assertFalse(current.is_required)
        self.assertIsNone(current.document_id)
        self.assertEqual(current.material_status, "missing")

    def test_t7c_real_routes_cover_record_and_structured_arrangements(self):
        case_id, _location = self.create_route_case()
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO visas(id,student_id) VALUES(3400,'1001')"
            )
            connection.execute(
                """INSERT INTO coe_oshc(id,student_id,document_kind)
                   VALUES(3300,'1001','OSHC')"""
            )
            connection.commit()
        base = f"/students/1001/transfer-cases/{case_id}/arrangements"
        actions = (
            (f"{base}/visa/update", {"record_id": "3400"}),
            (f"{base}/visa/confirm", {}),
            (f"{base}/oshc/carry-forward", {"record_id": "3300"}),
            (
                f"{base}/guardianship",
                {
                    "arrangement_mode": "parent_guardian",
                    "guardian_person_name": "Route Parent",
                    "guardian_organisation_name": "",
                    "relationship_to_student": "parent",
                    "provider_name": "",
                    "start_date": "2027-01-01",
                    "end_date": "",
                    "source_type": "staff_entered",
                    "arrangement_status": "confirmed",
                    "notes": "Route guardianship",
                },
            ),
            (
                f"{base}/accommodation",
                {
                    "accommodation_type": "homestay",
                    "provider_name": "Route Provider",
                    "host_name": "Route Host",
                    "address_line1": "1 Route Street",
                    "address_line2": "",
                    "suburb": "Melbourne",
                    "state_code": "VIC",
                    "postcode": "3000",
                    "country_code": "AU",
                    "start_date": "2027-01-01",
                    "end_date": "",
                    "source_type": "staff_entered",
                    "arrangement_status": "confirmed",
                    "notes": "Route accommodation",
                },
            ),
        )
        for path, form in actions:
            with self.subTest(path=path):
                status, headers, _body = self.request(path, form=form)
                self.assertEqual(status, HTTPStatus.SEE_OTHER)
                self.assertTrue(
                    headers["Location"].endswith("#transfer-arrangements")
                )
        service = transfer_arrangement_service.TransferArrangementService(
            self.database_path
        )
        self.assertEqual(
            service.get_transfer_visa_arrangement(
                case_id
            ).confirmation.arrangement_status,
            "confirmed",
        )
        self.assertEqual(
            service.get_transfer_oshc_arrangement(
                case_id
            ).confirmation.arrangement_status,
            "carried_forward",
        )
        self.assertEqual(
            service.get_transfer_guardianship_arrangement(
                case_id
            ).current_arrangement.guardian_person_name,
            "Route Parent",
        )
        self.assertEqual(
            service.get_transfer_accommodation_arrangement(
                case_id
            ).current_arrangement.provider_name,
            "Route Provider",
        )

    def test_t7c_routes_reject_wildcard_cross_student_and_extra_fields(self):
        case_id, _location = self.create_route_case()
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO visas(id,student_id) VALUES(3400,'1001')"
            )
            connection.commit()
        base = f"/students/1001/transfer-cases/{case_id}/arrangements"
        self.handler_class.route_user = {
            **self.handler_class.route_user,
            "permissions": ["students.view", "student_cases.view", "*"],
            "permissions_json": (
                '["students.view","student_cases.view","*"]'
            ),
        }
        status, _headers, _body = self.request(
            f"{base}/visa/update", form={"record_id": "3400"}
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.handler_class.route_user = {
            **self.handler_class.route_user,
            "permissions": [
                "students.view",
                "student_cases.view",
                "student_cases.edit",
            ],
            "permissions_json": (
                '["students.view","student_cases.view",'
                '"student_cases.edit"]'
            ),
        }
        status, _headers, _body = self.request(
            f"/students/1002/transfer-cases/{case_id}/arrangements/visa/update",
            form={"record_id": "3400"},
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.assertTrue(
            _headers["Location"].endswith("#transfer-arrangements")
        )
        status, _headers, _body = self.request(
            f"{base}/visa/update",
            form={"record_id": "3400", "created_by": "999"},
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        with closing(self.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM case_arrangement_confirmations
                       WHERE case_id=?""",
                    (case_id,),
                ).fetchone()[0],
                0,
            )

    def test_t7c_evidence_routes_preserve_history_and_anchor(self):
        case_id, _location = self.create_route_case()
        with closing(self.connect(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO visas(id,student_id) VALUES(3400,'1001')"
            )
            connection.execute(
                """INSERT INTO student_documents(id,student_id)
                   VALUES(3600,'1001'),(3601,'1001')"""
            )
            connection.commit()
        base = f"/students/1001/transfer-cases/{case_id}/arrangements"
        self.request(f"{base}/visa/update", form={"record_id": "3400"})
        status, headers, _body = self.request(
            f"{base}/visa/evidence",
            form={
                "evidence_source": "document:3600",
                "evidence_role": "primary",
            },
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.assertTrue(headers["Location"].endswith("#transfer-arrangements"))
        service = transfer_arrangement_service.TransferArrangementService(
            self.database_path
        )
        first = service.list_arrangement_evidence(case_id, "visa")[0]
        status, _headers, _body = self.request(
            f"{base}/visa/evidence/{first.evidence_id}/replace",
            form={
                "evidence_source": "document:3601",
                "evidence_role": "primary",
            },
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        rows = service.list_arrangement_evidence(case_id, "visa")
        current = next(row for row in rows if row.validity_status == "current")
        old = next(row for row in rows if row.evidence_id == first.evidence_id)
        self.assertEqual(old.superseded_by, current.evidence_id)
        status, _headers, _body = self.request(
            f"{base}/visa/evidence/{current.evidence_id}/historical",
            form={},
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.assertEqual(
            service.list_arrangement_evidence(
                case_id, "visa"
            )[0].validity_status,
            "historical",
        )


if __name__ == "__main__":
    unittest.main()
