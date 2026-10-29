from contextlib import closing
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_031 = ROOT / "migrations/20260827_031_transfer_v2_authoritative_lifecycle.sql"
TRANSFER_SOURCE_TEST_SCHEMA = ROOT / "tests/fixtures/transfer_source_authority_schema.sql"


def create_transfer_v2_database(path):
    path = Path(path)
    signed_contract_dir = path.parent / "signed-contracts"
    signed_contract_dir.mkdir(parents=True, exist_ok=True)
    signed_contract_101 = signed_contract_dir / "101.pdf"
    signed_contract_102 = signed_contract_dir / "102.pdf"
    signed_contract_101.write_bytes(b"%PDF-1.4\n% transfer test artifact\n")
    signed_contract_102.write_bytes(b"%PDF-1.4\n% transfer test artifact\n")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE roles(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,permissions_json TEXT NOT NULL);
            CREATE TABLE users(
              id INTEGER PRIMARY KEY,name TEXT,
              role_id INTEGER NOT NULL REFERENCES roles(id),
              is_active INTEGER NOT NULL DEFAULT 1,deleted_at TEXT
            );
            CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_by INTEGER,updated_at TEXT NOT NULL);
            CREATE TABLE students(
              student_id TEXT PRIMARY KEY,
              passport_last_name TEXT NOT NULL,
              passport_first_name TEXT NOT NULL,
              current_year_level TEXT,
              current_status TEXT NOT NULL DEFAULT 'New',
              created_date TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              form_data_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE schools(
              id INTEGER PRIMARY KEY,
              school_name TEXT NOT NULL,
              sector_key TEXT NOT NULL,
              school_type TEXT NOT NULL,
              state TEXT,
              region TEXT,
              deleted_at TEXT
            );
            CREATE TABLE contracts(
              id INTEGER PRIMARY KEY,
              student_id TEXT REFERENCES students(student_id),
              contract_type TEXT NOT NULL,
              contract_status TEXT NOT NULL,
              final_signed_file_path TEXT,
              generated_at TEXT,
              fully_executed_at TEXT,
              updated_at TEXT NOT NULL,
              deleted_at TEXT,
              voided_at TEXT,
              application_cycle_id INTEGER
            );
            CREATE TABLE offers(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id)
            );
            CREATE TABLE student_cases(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              case_type TEXT NOT NULL,
              sequence_no INTEGER NOT NULL,
              status TEXT NOT NULL,
              terminal_result TEXT,
              active_slot INTEGER NOT NULL DEFAULT 0,
              started_at TEXT,
              paused_at TEXT,
              completed_at TEXT,
              cancelled_at TEXT,
              failed_at TEXT,
              archived_at TEXT,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX uq_student_cases_active
              ON student_cases(student_id) WHERE active_slot=1;
            CREATE TABLE parent_form_tokens(
              token TEXT PRIMARY KEY,
              student_id TEXT REFERENCES students(student_id),
              intake_type TEXT,
              submitted_at TEXT,
              invalidated_at TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,
              cycle_uid TEXT NOT NULL UNIQUE,
              student_id TEXT REFERENCES students(student_id),
              parent_form_token TEXT REFERENCES parent_form_tokens(token),
              source_student_case_id INTEGER REFERENCES student_cases(id),
              cycle_type TEXT NOT NULL,
              sequence_no INTEGER NOT NULL,
              status TEXT NOT NULL,
              outcome TEXT,
              opened_at TEXT NOT NULL,
              closed_at TEXT,
              created_by_user_id INTEGER,
              created_by_name TEXT,
              created_at TEXT NOT NULL,
              updated_by_user_id INTEGER,
              updated_by_name TEXT,
              updated_at TEXT NOT NULL,
              education_stream TEXT NOT NULL DEFAULT 'school_education'
            );
            CREATE UNIQUE INDEX uq_application_cycles_parent_form
              ON application_cycles(parent_form_token) WHERE parent_form_token IS NOT NULL;
            CREATE TABLE private_school_applications(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id),
              student_id TEXT NOT NULL REFERENCES students(student_id),
              confirmed_school_id INTEGER,
              school_id INTEGER REFERENCES schools(id),
              school_name_snapshot TEXT NOT NULL,
              city_id INTEGER,
              status TEXT NOT NULL,
              submitted_at TEXT,
              submitted_by_user_id INTEGER REFERENCES users(id),
              submitted_by_name TEXT,
              result_at TEXT,
              result_note TEXT,
              created_by_user_id INTEGER REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE public_application_groups(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id),
              student_id TEXT NOT NULL REFERENCES students(student_id),
              city_id INTEGER,
              report_id INTEGER,
              confirmation_id INTEGER,
              status TEXT NOT NULL,
              submitted_at TEXT,
              submitted_by_user_id INTEGER REFERENCES users(id),
              submitted_by_name TEXT,
              created_by_user_id INTEGER REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE student_school_enrolments(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              school_id INTEGER REFERENCES schools(id),
              school_name_snapshot TEXT NOT NULL,
              institution_group_id INTEGER,
              city_key TEXT,
              campus_key TEXT,
              school_sector TEXT,
              enrolment_role TEXT NOT NULL CHECK(enrolment_role IN ('current','upcoming','historical')),
              course_name_snapshot TEXT,
              start_date TEXT,
              end_date TEXT,
              source_case_id INTEGER REFERENCES student_cases(id),
              status TEXT NOT NULL DEFAULT 'active',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              application_cycle_id INTEGER REFERENCES application_cycles(id),
              year_level_snapshot TEXT,
              lifecycle_context TEXT,
              confirmation_reason TEXT,
              confirmed_by INTEGER REFERENCES users(id),
              confirmed_at TEXT
            );
            CREATE UNIQUE INDEX uq_student_school_enrolments_one_current
              ON student_school_enrolments(student_id) WHERE enrolment_role='current';
            CREATE UNIQUE INDEX uq_student_school_enrolments_one_upcoming
              ON student_school_enrolments(student_id) WHERE enrolment_role='upcoming';
            CREATE TABLE school_enrolment_transition_intents(
              id INTEGER PRIMARY KEY,
              intent_uid TEXT NOT NULL UNIQUE,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              source_case_id INTEGER REFERENCES student_cases(id),
              application_cycle_id INTEGER REFERENCES application_cycles(id),
              current_enrolment_id INTEGER NOT NULL REFERENCES student_school_enrolments(id),
              upcoming_enrolment_id INTEGER NOT NULL REFERENCES student_school_enrolments(id),
              effective_date TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              reason TEXT NOT NULL,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              applied_by INTEGER REFERENCES users(id),
              applied_at TEXT,
              cancelled_by INTEGER REFERENCES users(id),
              cancelled_at TEXT,
              cancellation_reason TEXT,
              reversed_by INTEGER REFERENCES users(id),
              reversed_at TEXT,
              reversal_reason TEXT
            );
            CREATE TABLE school_enrolment_events(
              id INTEGER PRIMARY KEY,
              event_uid TEXT NOT NULL UNIQUE,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              source_case_id INTEGER REFERENCES student_cases(id),
              application_cycle_id INTEGER REFERENCES application_cycles(id),
              transition_intent_id INTEGER REFERENCES school_enrolment_transition_intents(id),
              enrolment_id INTEGER REFERENCES student_school_enrolments(id),
              event_type TEXT NOT NULL,
              old_value_json TEXT,
              new_value_json TEXT,
              reason TEXT NOT NULL,
              actor_user_id INTEGER NOT NULL REFERENCES users(id),
              occurred_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX uq_application_cycles_active_student_stream
              ON application_cycles(student_id,education_stream)
              WHERE status='active' AND student_id IS NOT NULL;
            CREATE UNIQUE INDEX uq_application_cycles_student_case
              ON application_cycles(source_student_case_id)
              WHERE source_student_case_id IS NOT NULL;
            CREATE UNIQUE INDEX uq_application_cycles_student_sequence
              ON application_cycles(student_id,sequence_no)
              WHERE student_id IS NOT NULL;
            CREATE TABLE transfer_cases(
              case_id INTEGER PRIMARY KEY REFERENCES student_cases(id),
              transfer_origin TEXT NOT NULL,
              source_contract_id INTEGER REFERENCES contracts(id),
              workflow_state TEXT NOT NULL,
              current_school_name_snapshot TEXT NOT NULL,
              current_school_id INTEGER REFERENCES schools(id),
              current_city_key TEXT,
              current_school_sector TEXT,
              target_city_key TEXT,
              target_school_sector TEXT,
              rule_status TEXT NOT NULL,
              same_city_result TEXT,
              sector_transition TEXT,
              required_completion_document TEXT,
              tuition_requirement TEXT,
              final_application_id INTEGER,
              rule_calculated_at TEXT,
              rule_calculated_by INTEGER REFERENCES users(id),
              rule_version TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              title TEXT,
              current_school_enrolment_id INTEGER,
              upcoming_school_enrolment_id INTEGER,
              relationship_status TEXT NOT NULL
            );
            CREATE TABLE transfer_school_applications(
              id INTEGER PRIMARY KEY,
              case_id INTEGER NOT NULL REFERENCES transfer_cases(case_id),
              school_id INTEGER REFERENCES schools(id),
              school_name_snapshot TEXT NOT NULL,
              institution_group_id INTEGER,
              city_key TEXT,
              campus_key TEXT,
              school_sector TEXT,
              expected_start_date TEXT,
              preference_order INTEGER,
              application_status TEXT NOT NULL,
              notes TEXT,
              is_final INTEGER NOT NULL DEFAULT 0,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE offer_received_events(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id),
              student_id TEXT NOT NULL REFERENCES students(student_id),
              source_kind TEXT NOT NULL CHECK(source_kind IN ('public','private','higher_education')),
              public_application_group_id INTEGER,
              private_application_id INTEGER,
              higher_education_offer_id INTEGER,
              legacy_offer_id INTEGER REFERENCES offers(id),
              school_id INTEGER,
              school_name_snapshot TEXT NOT NULL,
              external_offer_key TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              received_at TEXT NOT NULL,
              recorded_by_user_id INTEGER REFERENCES users(id),
              recorded_by_name TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE cycle_final_acceptances(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id),
              student_id TEXT NOT NULL REFERENCES students(student_id),
              offer_received_event_id INTEGER NOT NULL UNIQUE REFERENCES offer_received_events(id),
              school_id INTEGER,
              school_name_snapshot TEXT NOT NULL,
              signed_offer_completed_at TEXT,
              payment_completed_at TEXT,
              selected_at TEXT NOT NULL,
              selected_by_user_id INTEGER REFERENCES users(id),
              selected_by_name TEXT,
              acceptance_status TEXT NOT NULL DEFAULT 'active'
                CHECK(acceptance_status IN ('active','withdrawn','superseded')),
              ended_at TEXT,
              ended_by_user_id INTEGER REFERENCES users(id),
              ended_by_name TEXT,
              status_reason TEXT,
              replaces_acceptance_id INTEGER REFERENCES cycle_final_acceptances(id),
              superseded_by_acceptance_id INTEGER REFERENCES cycle_final_acceptances(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX uq_cycle_final_acceptances_one_active
              ON cycle_final_acceptances(application_cycle_id)
              WHERE acceptance_status='active';
            CREATE TABLE cycle_final_acceptance_history(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id),
              final_acceptance_id INTEGER NOT NULL REFERENCES cycle_final_acceptances(id),
              event_type TEXT NOT NULL,
              old_value_json TEXT,
              new_value_json TEXT,
              occurred_at TEXT NOT NULL,
              actor_user_id INTEGER REFERENCES users(id),
              actor_name TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE offer_received_event_state_events(
              id INTEGER PRIMARY KEY,
              offer_received_event_id INTEGER NOT NULL REFERENCES offer_received_events(id),
              state TEXT NOT NULL,
              reason TEXT,
              occurred_at TEXT NOT NULL,
              actor_user_id INTEGER REFERENCES users(id),
              actor_name TEXT,
              idempotency_key TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            );
            CREATE TABLE student_documents(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              document_type TEXT NOT NULL,
              file_path TEXT NOT NULL,
              uploaded_at TEXT NOT NULL
            );
            CREATE TABLE coe_oshc(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              document_kind TEXT NOT NULL,
              file_path TEXT,
              application_cycle_id INTEGER REFERENCES application_cycles(id)
            );
            CREATE TABLE case_events(
              id INTEGER PRIMARY KEY,
              case_id INTEGER NOT NULL REFERENCES student_cases(id),
              event_type TEXT NOT NULL,
              entity_type TEXT,
              entity_id TEXT,
              old_value_json TEXT,
              new_value_json TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              actor_type TEXT NOT NULL,
              actor_user_id INTEGER REFERENCES users(id),
              request_id TEXT,
              correlation_id TEXT,
              correction_of_event_id INTEGER,
              created_at TEXT NOT NULL
            );
            CREATE TABLE student_lifecycle_idempotency_receipts(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              operation TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL,
              result_case_id INTEGER NOT NULL REFERENCES student_cases(id),
              result_cycle_id INTEGER REFERENCES application_cycles(id),
              result_json TEXT,
              actor_user_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(student_id,operation,idempotency_key)
            );
            """
        )
        connection.executescript(MIGRATION_031.read_text(encoding="utf-8"))
        connection.executescript(
            """
            INSERT INTO roles VALUES(1,'Transfer operator','["student_cases.view","student_cases.create","student_cases.edit"]');
            INSERT INTO users(id,name,role_id) VALUES(1,'Alice',1),(2,'Bob',1);
            INSERT INTO system_settings VALUES('STUDENT_CASES_READ_ENABLED','true',1,'2026-08-01'),('TRANSFER_CASES_WRITE_ENABLED','true',1,'2026-08-01');
            INSERT INTO students VALUES
              ('S1','Existing','Student','Year 8','Active','2026-08-01','2026-08-01','{}'),
              ('S2','New','Transfer','Year 7','New','2026-08-01','2026-08-01','{}'),
              ('S3','Legacy','Active','Year 9','Active','2026-08-01','2026-08-01','{}');
            INSERT INTO schools VALUES
              (10,'Current College','private','Independent','VIC','Melbourne',NULL),
              (20,'Target Grammar','private','Independent','VIC','Melbourne',NULL),
              (30,'Target High','public','Government','NSW','Sydney',NULL);
            INSERT INTO contracts VALUES
              (100,'S1','TransferApplicationContract','Draft',NULL,'2026-08-20',NULL,'2026-08-20',NULL,NULL,NULL),
              (101,'S1','TransferApplicationContract','FullyExecuted','/contracts/101.pdf','2026-08-21','2026-08-22','2026-08-22',NULL,NULL,NULL),
              (102,'S2','TransferApplicationContract','FullyExecuted','/contracts/102.pdf','2026-08-21','2026-08-22','2026-08-22',NULL,NULL,NULL);
            INSERT INTO student_school_enrolments(
              id,student_id,school_id,school_name_snapshot,school_sector,
              enrolment_role,start_date,status,created_by,created_at,updated_by,
              updated_at,year_level_snapshot)
            VALUES(80,'S1',10,'Current College','private','current','2025-01-28',
                   'active',1,'2025-01-01',1,'2026-08-20','Year 8');
            INSERT INTO student_cases(
              id,student_id,case_type,sequence_no,status,active_slot,started_at,
              created_by,created_at,updated_by,updated_at)
            VALUES(90,'S3','transfer',1,'in_progress',1,'2026-08-20',1,'2026-08-20',1,'2026-08-20');
            INSERT INTO application_cycles(
              id,cycle_uid,student_id,source_student_case_id,cycle_type,sequence_no,
              status,opened_at,created_by_user_id,created_by_name,created_at,
              updated_by_user_id,updated_by_name,updated_at,education_stream)
            VALUES(190,'legacy-active','S3',90,'transfer',2,'active','2026-08-20',
                   1,'Alice','2026-08-20',1,'Alice','2026-08-20','school_education');
            INSERT INTO transfer_cases(
              case_id,transfer_origin,source_contract_id,workflow_state,
              current_school_name_snapshot,current_school_id,rule_status,
              required_completion_document,tuition_requirement,rule_version,
              created_at,updated_at,relationship_status)
            VALUES(90,'internal',NULL,'preparing','Legacy School',10,
                   'pending_confirmation','pending_confirmation','pending_confirmation',
                   'transfer-rule-v1','2026-08-20','2026-08-20','needs_repair');
            """
        )
        connection.execute(
            "UPDATE contracts SET final_signed_file_path=? WHERE id=101",
            (str(signed_contract_101),),
        )
        connection.execute(
            "UPDATE contracts SET final_signed_file_path=? WHERE id=102",
            (str(signed_contract_102),),
        )
        # Test-only schema mirrors accepted source authority without creating a migration.
        connection.executescript(TRANSFER_SOURCE_TEST_SCHEMA.read_text(encoding="utf-8"))
        connection.executescript("""
          INSERT INTO application_cycles(
            id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,
            created_at,updated_at,education_stream)
          VALUES(8800,'S1-historical-ordinary','S1','initial',2,'closed',
                 '2025-01-01','2025-01-01','2025-02-01','school_education');
          INSERT INTO contracts(id,student_id,contract_type,contract_status,updated_at,application_cycle_id)
          VALUES(900,'S1','LocalStudentApplicationContract','FullyExecuted','2025-01-01',8800);
          UPDATE student_school_enrolments SET confirmed_by=1,confirmed_at='2025-01-28' WHERE id=80;
          INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at,created_at)
          VALUES('S2-transfer-intake','S2','new_student_transfer','2026-08-20','2026-08-20');
        """)
        from student_lifecycle.transfer_source_authority import save_authority
        save_authority(connection, 'S1', 101, current_enrolment_id=80,
                       ordinary_relationship_cycle_id=8800, ordinary_relationship_contract_id=900,
                       actor_user_id=1, occurred_at='2026-08-22')
        save_authority(connection, 'S2', 102, parent_form_token='S2-transfer-intake',
                       actor_user_id=1, occurred_at='2026-08-22')
        connection.commit()


def query_one(path, sql, parameters=()):
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, parameters).fetchone()


def query_all(path, sql, parameters=()):
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, parameters).fetchall()
