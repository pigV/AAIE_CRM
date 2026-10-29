"""Test-only Transfer source schema support; no repository migration is created or applied."""
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "tests" / "fixtures" / "transfer_source_authority_schema.sql"


def fixture():
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE users(id INTEGER PRIMARY KEY);
    CREATE TABLE students(student_id TEXT PRIMARY KEY);
    CREATE TABLE parent_form_tokens(token TEXT PRIMARY KEY,student_id TEXT REFERENCES students(student_id),intake_type TEXT,submitted_at TEXT,invalidated_at TEXT,created_at TEXT DEFAULT '2026-09-07');
    CREATE TABLE student_cases(id INTEGER PRIMARY KEY,student_id TEXT REFERENCES students(student_id),case_type TEXT DEFAULT 'transfer');
    CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT REFERENCES students(student_id),cycle_type TEXT,education_stream TEXT,status TEXT,parent_form_token TEXT REFERENCES parent_form_tokens(token),source_student_case_id INTEGER REFERENCES student_cases(id));
    CREATE UNIQUE INDEX uq_application_cycles_parent_form ON application_cycles(parent_form_token) WHERE parent_form_token IS NOT NULL;
    CREATE TABLE contracts(id INTEGER PRIMARY KEY,student_id TEXT REFERENCES students(student_id),contract_type TEXT,contract_status TEXT,deleted_at TEXT,voided_at TEXT,application_cycle_id INTEGER REFERENCES application_cycles(id));
    CREATE TABLE student_school_enrolments(id INTEGER PRIMARY KEY,student_id TEXT REFERENCES students(student_id),enrolment_role TEXT,status TEXT,confirmed_at TEXT);
    CREATE TABLE private_school_applications(id INTEGER PRIMARY KEY,student_id TEXT REFERENCES students(student_id),application_cycle_id INTEGER REFERENCES application_cycles(id),status TEXT,submitted_at TEXT);
    CREATE TABLE public_application_groups(id INTEGER PRIMARY KEY,student_id TEXT REFERENCES students(student_id),application_cycle_id INTEGER REFERENCES application_cycles(id),status TEXT,submitted_at TEXT);
    CREATE TABLE transfer_cases(case_id INTEGER PRIMARY KEY REFERENCES student_cases(id),source_contract_id INTEGER REFERENCES contracts(id),transfer_source_label TEXT);
    CREATE TABLE transfer_v2_origin_events(id INTEGER PRIMARY KEY,application_cycle_id INTEGER REFERENCES application_cycles(id),new_source_label TEXT,event_uid TEXT UNIQUE);
    INSERT INTO users VALUES(1);
    INSERT INTO students VALUES('new'),('existing'),('other');
    INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at,invalidated_at) VALUES('new-intake','new','new_student_transfer','2026-09-07',NULL),('other-intake','other','new_student_transfer','2026-09-07',NULL);
    INSERT INTO application_cycles VALUES(10,'new','initial','school_education','closed','new-intake',NULL),(20,'existing','initial','school_education','closed',NULL,NULL);
    INSERT INTO contracts VALUES(100,'new','TransferApplicationContract','Draft',NULL,NULL,NULL),(200,'existing','TransferApplicationContract','Draft',NULL,NULL,NULL),(201,'existing','LocalStudentApplicationContract','FullyExecuted',NULL,NULL,20),(300,'other','TransferApplicationContract','Draft',NULL,NULL,NULL);
    INSERT INTO student_school_enrolments VALUES(21,'existing','current','active','2026-09-01');
    """)
    return db
