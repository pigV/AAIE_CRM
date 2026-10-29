#!/usr/bin/env python3
"""Serve real CRM handlers on a synthetic 037 -> 038 -> exact 039 fixture."""
from contextlib import closing
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "SAFEBOX"
sys.path.insert(0, str(ROOT))

from database import Database
from migration_runner import apply_migrations
from tools.visa_candidate_browser import bootstrap, free_port, seed


def confined(path):
    path = Path(path).resolve()
    if path == BASE or not path.is_relative_to(BASE):
        raise ValueError("Browser runtime must be a new directory below this worktree SAFEBOX")
    return path


def build_database(runtime):
    database = runtime / "candidate.sqlite3"
    Database(database).initialize()
    # Add the exact lifecycle ownership shape needed by the 20260914 browser
    # scenarios, then replace only the empty synthetic visa_cases table with the
    # production-shaped fixture used by the dedicated Visa test authority.
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript("""
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY, cycle_uid TEXT NOT NULL UNIQUE,
              student_id TEXT REFERENCES students(student_id) ON DELETE RESTRICT,
              parent_form_token TEXT, source_student_case_id INTEGER,
              cycle_type TEXT NOT NULL, sequence_no INTEGER NOT NULL,
              status TEXT NOT NULL, outcome TEXT, opened_at TEXT NOT NULL,
              closed_at TEXT, created_by_user_id INTEGER REFERENCES users(id),
              created_by_name TEXT, created_at TEXT NOT NULL,
              updated_by_user_id INTEGER REFERENCES users(id), updated_by_name TEXT,
              updated_at TEXT NOT NULL, education_stream TEXT NOT NULL DEFAULT 'school_education'
            );
            CREATE TABLE visa_decision_events(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER NOT NULL UNIQUE
                REFERENCES application_cycles(id) ON DELETE RESTRICT,
              student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
              visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE RESTRICT,
              student_document_id INTEGER UNIQUE
                REFERENCES student_documents(id) ON DELETE RESTRICT,
              visa_record_id INTEGER UNIQUE REFERENCES visas(id) ON DELETE RESTRICT,
              decision_type TEXT NOT NULL CHECK(decision_type IN ('grant','refusal')),
              decision_date TEXT,
              confirmed_at TEXT NOT NULL,
              evidence_uploaded_at TEXT,
              decision_source TEXT NOT NULL DEFAULT 'visa_record'
                CHECK(decision_source IN ('visa_record','legacy_document')),
              idempotency_key TEXT NOT NULL UNIQUE CHECK(length(trim(idempotency_key)) > 0),
              recorded_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
              recorded_by_name TEXT,
              created_at TEXT NOT NULL,
              CHECK(
                (decision_source='visa_record' AND visa_record_id IS NOT NULL
                  AND decision_date IS NOT NULL AND length(trim(decision_date)) > 0)
                OR
                (decision_source='legacy_document' AND student_document_id IS NOT NULL)
              )
            );
            CREATE INDEX idx_visa_decision_events_type_date
            ON visa_decision_events(decision_type,decision_date,application_cycle_id);
            CREATE TRIGGER visa_decision_events_ownership_insert
            BEFORE INSERT ON visa_decision_events
            WHEN NOT EXISTS (
              SELECT 1 FROM application_cycles
              WHERE id=NEW.application_cycle_id AND student_id=NEW.student_id
            )
            OR NOT EXISTS (
              SELECT 1 FROM visa_cases
              WHERE id=NEW.visa_case_id
                AND student_id=NEW.student_id
                AND application_cycle_id=NEW.application_cycle_id
            )
            OR (
              NEW.student_document_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM student_documents
                WHERE id=NEW.student_document_id
                  AND student_id=NEW.student_id
                  AND is_active=1
                  AND (
                    (NEW.decision_type='grant' AND document_type='VisaGrantLetter')
                    OR
                    (NEW.decision_type='refusal' AND document_type='VisaRefusalLetter')
                  )
              )
            )
            OR (
              NEW.visa_record_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM visas
                WHERE id=NEW.visa_record_id
                  AND student_id=NEW.student_id
                  AND (
                    (NEW.decision_type='grant' AND lower(trim(visa_status)) IN ('granted','active'))
                    OR
                    (NEW.decision_type='refusal' AND lower(trim(visa_status))='refused')
                  )
              )
            )
            BEGIN
              SELECT RAISE(ABORT, 'visa decision ownership or evidence mismatch');
            END;
            CREATE TRIGGER visa_decision_events_no_update
            BEFORE UPDATE ON visa_decision_events
            BEGIN SELECT RAISE(ABORT, 'visa decision events are append-only'); END;
            CREATE TRIGGER visa_decision_events_no_delete
            BEFORE DELETE ON visa_decision_events
            BEGIN SELECT RAISE(ABORT, 'visa decision events are append-only'); END;
            CREATE TABLE visa_form_submission_originals(
              assignment_id INTEGER PRIMARY KEY REFERENCES visa_form_assignments(id),
              respondent_id INTEGER NOT NULL REFERENCES visa_information_collection_respondents(id),
              visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),
              original_json TEXT NOT NULL, captured_at TEXT NOT NULL, source TEXT NOT NULL
            );
            CREATE TRIGGER visa_form_submission_originals_immutable_update_037
            BEFORE UPDATE ON visa_form_submission_originals
            BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
            CREATE TRIGGER visa_form_submission_originals_immutable_delete_037
            BEFORE DELETE ON visa_form_submission_originals
            BEGIN SELECT RAISE(ABORT,'Visa audit/original is immutable'); END;
            CREATE TABLE visa_form_change_audit(
              id INTEGER PRIMARY KEY, assignment_id INTEGER NOT NULL REFERENCES visa_form_assignments(id),
              visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id), actor_id INTEGER REFERENCES users(id),
              action TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE visa_applicants(
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
            );
            CREATE TABLE visa_applicant_student_links(
              id INTEGER PRIMARY KEY, applicant_id INTEGER NOT NULL UNIQUE REFERENCES visa_applicants(id),
              student_id TEXT NOT NULL REFERENCES students(student_id),
              linked_by INTEGER NOT NULL REFERENCES users(id), linked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS visa_case_documents(
              id INTEGER PRIMARY KEY, visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),
              student_document_id INTEGER REFERENCES student_documents(id), file_path TEXT NOT NULL,
              original_name TEXT NOT NULL, version INTEGER NOT NULL, sha256 TEXT NOT NULL,
              created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
              archived_at TEXT, archived_by INTEGER REFERENCES users(id),
              UNIQUE(visa_case_id,student_document_id)
            );
            CREATE TABLE visa_immi_submissions(
              id INTEGER PRIMARY KEY, visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),
              document_id INTEGER NOT NULL REFERENCES visa_case_documents(id), file_path TEXT NOT NULL,
              version INTEGER NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL,
              submitted_by INTEGER NOT NULL REFERENCES users(id), submitted_at TEXT NOT NULL,
              supersedes_id INTEGER REFERENCES visa_immi_submissions(id), UNIQUE(visa_case_id,document_id)
            );
            CREATE TABLE visa_letter_extractions(
              id INTEGER PRIMARY KEY, visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),
              document_id INTEGER NOT NULL REFERENCES visa_case_documents(id), proposed_json TEXT NOT NULL,
              extraction_method TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
            );
            CREATE TABLE visa_letter_reviews(
              id INTEGER PRIMARY KEY, extraction_id INTEGER NOT NULL REFERENCES visa_letter_extractions(id),
              visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id), confirmed_json TEXT NOT NULL,
              confirmed_by INTEGER NOT NULL REFERENCES users(id), confirmed_at TEXT NOT NULL
            );
            CREATE TABLE visa_case_confirmed_decisions(
              id INTEGER PRIMARY KEY, visa_case_id INTEGER NOT NULL UNIQUE REFERENCES visa_cases(id),
              extraction_id INTEGER NOT NULL REFERENCES visa_letter_extractions(id), decision_type TEXT NOT NULL,
              decision_date TEXT NOT NULL, confirmed_json TEXT NOT NULL,
              confirmed_by INTEGER NOT NULL REFERENCES users(id), confirmed_at TEXT NOT NULL,
              canonical_event_id INTEGER
            );
            CREATE TABLE visa_document_audit(
              id INTEGER PRIMARY KEY, visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),
              document_id INTEGER REFERENCES visa_case_documents(id), action TEXT NOT NULL,
              before_json TEXT NOT NULL, after_json TEXT NOT NULL,
              actor_user_id INTEGER NOT NULL REFERENCES users(id), occurred_at TEXT NOT NULL
            );
        """)
    from tests.visa_test_schema import prepare_production_visa_schema
    with closing(sqlite3.connect(database)) as connection:
        prepare_production_visa_schema(connection)
        connection.execute("ALTER TABLE visa_cases ADD COLUMN applicant_id INTEGER REFERENCES visa_applicants(id)")
    package = runtime / "migration-package"
    checks = package / "checks"
    checks.mkdir(parents=True)
    for relative in (
        "migrations/20260805_012_parent_form_secure_draft.sql",
        "migrations/checks/20260805_012_parent_form_secure_draft_forward_check.sql",
        "migrations/20260805_013_parent_form_access_history.sql",
        "migrations/checks/20260805_013_parent_form_access_history_forward_check.sql",
        "migrations/20260909_037_visa_document_metadata.sql",
        "migrations/checks/20260909_037_visa_document_metadata_forward_check.sql",
        "migrations/20260909_038_visa_processing_dates.sql",
        "migrations/checks/20260909_038_visa_processing_dates_forward_check.sql",
        "migrations/20260914_039_visa_result_status_authority.sql",
        "migrations/checks/20260914_039_visa_result_status_authority_forward_check.sql",
    ):
        source = ROOT / relative
        target = package / source.name if source.parent.name == "migrations" else checks / source.name
        shutil.copyfile(source, target)
    with closing(sqlite3.connect(database)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
            connection.execute("DELETE FROM schema_migrations")
            connection.commit()
    result = apply_migrations(
        database, package, through="20260914_039",
        applied_by="staging-browser-synthetic", source_commit="staging-unsealed-candidate",
    )
    with closing(sqlite3.connect(database)) as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY rowid")]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if versions != ["20260805_012", "20260805_013", "20260909_037", "20260909_038", "20260914_039"]:
        raise RuntimeError("Unexpected synthetic migration authority: " + repr(versions))
    if integrity != "ok" or foreign_keys:
        raise RuntimeError("Synthetic browser database integrity failure")
    return database, {"migration_result": result, "versions": versions, "integrity": integrity, "foreign_key_violations": len(foreign_keys)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18980)
    parser.add_argument("--parent-port", type=int, default=18981)
    args = parser.parse_args()
    runtime = confined(args.runtime)
    runtime.mkdir(parents=True, exist_ok=False)
    free_port(args.port); free_port(args.parent_port)
    database, migration = build_database(runtime)
    # Reuse the application's Staging-only path routing. This fixture already
    # has a stricter process audit guard in bootstrap(), and never loads the
    # formal STAGING_DATA or Production authority.
    import staging_environment
    staging_environment.ACTIVE = staging_environment.Staging(
        runtime, args.port, args.parent_port, "aaie_staging_"
    )
    app = bootstrap(runtime, database, (args.port, args.parent_port))
    fixture = seed(app, runtime)
    fixture.update({
        "database": str(database), "runtime": str(runtime),
        "url": f"http://127.0.0.1:{args.port}",
        "parent_url": f"http://127.0.0.1:{args.parent_port}",
        "migration": migration, "production_access": "DENIED", "ai_requests": 0,
    })
    (runtime / "fixture.json").write_text(json.dumps(fixture, indent=2, default=str) + "\n")
    class ParentFixtureHandler(app.CRMHandler):
        def dispatch(self, method):
            if method == "GET" and self.path == "/__fixture__/parent":
                form = fixture["forms"][0]
                return self.redirect(
                    "/visa-information-collection/" + form["token"] + "/forms/visa_application_info_500_590_485/overview",
                    form["cookie_name"] + "=" + form["cookie_value"] + "; Path=/; HttpOnly; SameSite=Lax",
                )
            return super().dispatch(method)
    parent = app.ReusableThreadingHTTPServer(("127.0.0.1", args.parent_port), ParentFixtureHandler)
    parent.service_mode = "parent"
    admin = app.ReusableThreadingHTTPServer(("127.0.0.1", args.port), app.CRMHandler)
    admin.service_mode = "admin"
    threading.Thread(target=parent.serve_forever, daemon=True).start()
    print(json.dumps({"status": "READY", "fixture": str(runtime / "fixture.json"), "url": fixture["url"]}), flush=True)
    try:
        admin.serve_forever()
    finally:
        parent.shutdown(); admin.server_close(); parent.server_close()


if __name__ == "__main__":
    main()
