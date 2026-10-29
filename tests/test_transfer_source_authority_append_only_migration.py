"""Migration 041 constraint tests; synthetic in-memory data only."""
from pathlib import Path
import sqlite3
import unittest

ROOT = Path(__file__).resolve().parents[1]
STEM = "20260920_041_transfer_source_authority"
MIGRATION = ROOT / "migrations" / (STEM + ".sql")
CHECK = ROOT / "migrations/checks" / (STEM + "_forward_check.sql")
ROLLBACK = ROOT / "migrations/rollback" / (STEM + "_rollback.sql")


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


class SourceAuthorityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = fixture()
        self.addCleanup(self.db.close)
        self.assertTrue(MIGRATION.exists(), "Migration 041 is required")
        self.db.executescript(MIGRATION.read_text())

    def pending(self, student="new", contract=100, **kw):
        values = dict(student_id=student,transfer_contract_id=contract,rule_version="transfer-source-v1",created_by=1,created_at="2026-09-07")
        values.update(kw or dict(parent_form_token="new-intake"))
        columns = ",".join(values)
        self.db.execute(f"INSERT INTO transfer_source_authorities({columns}) VALUES({','.join('?' for _ in values)})",tuple(values.values()))

    def consume(self, student="new", contract=100, source="new_transfer"):
        self.db.execute("INSERT INTO student_cases(id,student_id) VALUES(99,?)",(student,))
        self.db.execute("INSERT INTO application_cycles VALUES(99,?,'transfer','school_education','active',NULL,99)",(student,))
        self.db.execute("UPDATE contracts SET contract_status='FullyExecuted',application_cycle_id=99 WHERE id=?",(contract,))
        self.db.execute("INSERT INTO transfer_cases VALUES(99,?,?)",(contract,source))
        self.db.execute("INSERT INTO transfer_v2_origin_events(id,application_cycle_id,new_source_label,event_uid) VALUES(99,99,?,'origin-99')",(source,))
        self.db.execute("UPDATE transfer_source_authorities SET lifecycle_state='consumed',source_classification=?,application_cycle_id=99,verified_by=1,verified_at='2026-09-07' WHERE transfer_contract_id=?",(source,contract))

    def test_closed_initial_keeps_parent_ownership(self):
        self.pending()
        self.consume()
        self.assertEqual(self.db.execute("SELECT parent_form_token FROM application_cycles WHERE id=10").fetchone()[0],"new-intake")
        self.assertEqual(self.db.execute("SELECT source_classification FROM transfer_source_authorities").fetchone()[0],"new_transfer")
        self.assertFalse(self.db.execute("PRAGMA foreign_key_check").fetchall())

    def test_new_without_initial(self):
        self.db.execute("DELETE FROM application_cycles WHERE id=10")
        self.pending()
        self.consume()

    def test_existing_dual_evidence(self):
        self.pending("existing",200,current_enrolment_id=21,ordinary_relationship_cycle_id=20,ordinary_relationship_contract_id=201)
        self.consume("existing",200,"aaie_existing")

    def test_missing_authority_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.pending(parent_form_token=None)

    def test_cross_student_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.pending(parent_form_token="other-intake")

    def test_existing_missing_relationship_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.pending("existing",200,current_enrolment_id=21)

    def test_empty_initial_is_not_relationship(self):
        self.db.execute("UPDATE contracts SET contract_status='Draft' WHERE id=201")
        with self.assertRaises(sqlite3.IntegrityError):
            self.pending("existing",200,current_enrolment_id=21,ordinary_relationship_cycle_id=20,ordinary_relationship_contract_id=201)

    def test_conflicting_new_and_existing_rejected(self):
        self.db.execute("INSERT INTO student_school_enrolments VALUES(11,'new','current','active','2026-09-01')")
        self.db.execute("INSERT INTO contracts VALUES(101,'new','LocalStudentApplicationContract','FullyExecuted',NULL,NULL,10)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.pending()
            self.consume()

    def test_pending_evidence_immutable(self):
        self.pending()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE transfer_source_authorities SET parent_form_token='other-intake'")

    def test_consumed_immutable_and_intake_correction_blocked(self):
        self.pending()
        self.consume()
        for sql in ("UPDATE transfer_source_authorities SET source_classification='aaie_existing'", "DELETE FROM transfer_source_authorities", "UPDATE parent_form_tokens SET intake_type='ordinary_new_application' WHERE token='new-intake'"):
            with self.assertRaises(sqlite3.IntegrityError): self.db.execute(sql)

    def test_uniqueness(self):
        self.pending()
        with self.assertRaises(sqlite3.IntegrityError): self.pending()

    def test_rollback_rejects_facts(self):
        self.pending()
        with self.assertRaises(sqlite3.IntegrityError): self.db.executescript(ROLLBACK.read_text())
        self.assertEqual(self.db.execute("SELECT count(*) FROM transfer_source_authorities").fetchone()[0],1)

    def test_typed_submitted_application_relationship(self):
        self.db.execute("UPDATE contracts SET contract_status='Draft' WHERE id=201")
        self.db.execute("INSERT INTO private_school_applications VALUES(31,'existing',20,'submitted','2026-09-01')")
        self.pending("existing",200,current_enrolment_id=21,ordinary_relationship_cycle_id=20,private_school_application_id=31)
        self.consume("existing",200,"aaie_existing")

    def test_current_requires_confirmation(self):
        self.db.execute("UPDATE student_school_enrolments SET confirmed_at=NULL")
        with self.assertRaises(sqlite3.IntegrityError):
            self.pending("existing",200,current_enrolment_id=21,ordinary_relationship_cycle_id=20,ordinary_relationship_contract_id=201)

    def test_new_conflict_introduced_after_context_save_rechecked(self):
        self.pending()
        self.db.execute("INSERT INTO student_school_enrolments VALUES(11,'new','current','active','2026-09-01')")
        self.db.execute("INSERT INTO contracts VALUES(101,'new','LocalStudentApplicationContract','FullyExecuted',NULL,NULL,10)")
        with self.assertRaisesRegex(sqlite3.IntegrityError,"conflicting"):
            self.consume()

    def test_current_ambiguity_rejected(self):
        self.db.execute("INSERT INTO student_school_enrolments VALUES(22,'existing','current','active','2026-09-01')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.pending("existing",200,current_enrolment_id=21,ordinary_relationship_cycle_id=20,ordinary_relationship_contract_id=201)

    def test_evidence_invalidated_between_save_and_consume_rejected(self):
        self.pending("existing",200,current_enrolment_id=21,ordinary_relationship_cycle_id=20,ordinary_relationship_contract_id=201)
        self.db.execute("UPDATE contracts SET voided_at='2026-09-07' WHERE id=201")
        with self.assertRaises(sqlite3.IntegrityError):
            self.consume("existing",200,"aaie_existing")

    def test_submitted_public_application_proof(self):
        self.db.execute("UPDATE contracts SET contract_status='Draft' WHERE id=201")
        self.db.execute("INSERT INTO public_application_groups VALUES(31,'existing',20,'submitted','2026-09-01')")
        self.pending("existing",200,current_enrolment_id=21,ordinary_relationship_cycle_id=20,public_application_group_id=31)
        self.consume("existing",200,"aaie_existing")

    def test_multiple_proof_kinds_cannot_be_stored(self):
        self.db.execute("INSERT INTO private_school_applications VALUES(31,'existing',20,'submitted','2026-09-01')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.pending("existing",200,current_enrolment_id=21,ordinary_relationship_cycle_id=20,ordinary_relationship_contract_id=201,private_school_application_id=31)

    def test_direct_consumed_insert_rejected(self):
        with self.assertRaisesRegex(sqlite3.IntegrityError,"start pending"):
            self.pending(lifecycle_state="consumed",parent_form_token="new-intake",source_classification="new_transfer",application_cycle_id=10,verified_by=1,verified_at="2026-09-07")

    def test_new_parent_requires_explicit_correct_intake(self):
        self.db.execute("UPDATE parent_form_tokens SET intake_type=NULL WHERE token='new-intake'")
        with self.assertRaises(sqlite3.IntegrityError): self.pending()

    def test_consumed_projection_drift_rejected(self):
        self.pending()
        self.consume()
        for sql in ("UPDATE transfer_cases SET transfer_source_label='aaie_existing' WHERE case_id=99", "INSERT INTO transfer_v2_origin_events(id,application_cycle_id,new_source_label) VALUES(100,99,'aaie_existing')", "UPDATE application_cycles SET student_id='other' WHERE id=99"):
            with self.assertRaises(sqlite3.IntegrityError): self.db.execute(sql)

    def test_consumed_fk_and_identity_remain_retained(self):
        self.pending("existing",200,current_enrolment_id=21,ordinary_relationship_cycle_id=20,ordinary_relationship_contract_id=201)
        self.consume("existing",200,"aaie_existing")
        for sql in ("DELETE FROM student_school_enrolments WHERE id=21", "UPDATE student_school_enrolments SET student_id='other' WHERE id=21", "UPDATE contracts SET application_cycle_id=NULL WHERE id=201"):
            with self.assertRaises(sqlite3.IntegrityError): self.db.execute(sql)
        self.db.execute("UPDATE student_school_enrolments SET enrolment_role='historical',status='closed' WHERE id=21")
        self.assertEqual(self.db.execute("SELECT source_classification FROM transfer_source_authorities").fetchone()[0],"aaie_existing")

    def test_invalidated_new_intake_rejected(self):
        self.db.execute("UPDATE parent_form_tokens SET invalidated_at='2026-09-07' WHERE token='new-intake'")
        with self.assertRaises(sqlite3.IntegrityError): self.pending()

    def test_missing_confirmation_or_submission_does_not_prove_existing(self):
        self.db.execute("UPDATE contracts SET contract_status='Draft' WHERE id=201")
        self.db.execute("INSERT INTO private_school_applications VALUES(31,'existing',20,'submitted',NULL)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.pending("existing",200,current_enrolment_id=21,ordinary_relationship_cycle_id=20,private_school_application_id=31)

    def test_legacy_data_untouched_by_additive_schema(self):
        before=fixture()
        self.addCleanup(before.close)
        for table in ("parent_form_tokens","application_cycles","contracts","student_school_enrolments","student_cases","transfer_cases"):
            self.assertEqual(before.execute(f"SELECT * FROM {table}").fetchall(),self.db.execute(f"SELECT * FROM {table}").fetchall())

    def test_unsubmitted_new_intake_rejected(self):
        self.db.execute("UPDATE parent_form_tokens SET submitted_at=NULL WHERE token='new-intake'")
        with self.assertRaises(sqlite3.IntegrityError): self.pending()

    def test_new_authority_cannot_attach_to_historical_bound_contract(self):
        self.db.execute("UPDATE contracts SET application_cycle_id=10 WHERE id=100")
        with self.assertRaisesRegex(sqlite3.IntegrityError,"unbound"):
            self.pending()

    def test_new_authority_cannot_attach_to_existing_case_contract(self):
        self.db.execute("INSERT INTO student_cases(id,student_id) VALUES(88,'new')")
        self.db.execute("INSERT INTO transfer_cases VALUES(88,100,'new_transfer')")
        with self.assertRaisesRegex(sqlite3.IntegrityError,"unbound"):
            self.pending()

    def test_replace_pending_authority_cannot_delete_audit_identity(self):
        self.pending()
        self.assertEqual(self.db.execute("PRAGMA recursive_triggers").fetchone()[0],0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT OR REPLACE INTO transfer_source_authorities(student_id,transfer_contract_id,parent_form_token,rule_version,created_by,created_at) VALUES('new',100,'new-intake','changed',1,'later')")
        self.assertEqual(self.db.execute("SELECT rule_version FROM transfer_source_authorities").fetchone()[0],"transfer-source-v1")

    def test_replace_consumed_authority_id_with_new_contract_rejected(self):
        self.pending()
        self.consume()
        self.db.execute("INSERT INTO contracts VALUES(301,'new','TransferApplicationContract','Draft',NULL,NULL,NULL)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT OR REPLACE INTO transfer_source_authorities(id,student_id,transfer_contract_id,parent_form_token,rule_version,created_by,created_at) VALUES(1,'new',301,'new-intake','changed',1,'later')")
        self.assertEqual(self.db.execute("SELECT lifecycle_state FROM transfer_source_authorities").fetchone()[0],"consumed")

    def test_replace_consumed_origin_by_id_or_uid_rejected(self):
        self.pending()
        self.consume()
        for sql in ("INSERT OR REPLACE INTO transfer_v2_origin_events(id,application_cycle_id,new_source_label,event_uid) VALUES(99,10,'aaie_existing','new-origin')", "INSERT OR REPLACE INTO transfer_v2_origin_events(id,application_cycle_id,new_source_label,event_uid) VALUES(100,10,'aaie_existing','origin-99')"):
            with self.assertRaises(sqlite3.IntegrityError): self.db.execute(sql)
        self.assertEqual(self.db.execute("SELECT application_cycle_id FROM transfer_v2_origin_events WHERE id=99").fetchone()[0],99)

    def test_consumed_case_and_cycle_semantic_identity_immutable(self):
        self.pending()
        self.consume()
        for sql in ("UPDATE student_cases SET student_id='other' WHERE id=99", "UPDATE student_cases SET case_type='progression' WHERE id=99", "UPDATE application_cycles SET education_stream='higher_education' WHERE id=99"):
            with self.assertRaises(sqlite3.IntegrityError): self.db.execute(sql)

    def test_replace_consumed_case_projection_rejected(self):
        self.pending()
        self.consume()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT OR REPLACE INTO transfer_cases(case_id,source_contract_id,transfer_source_label) VALUES(99,100,'aaie_existing')")

    def test_consumption_rejects_wrong_case_type_or_education_stream(self):
        for target_sql in ("UPDATE student_cases SET case_type='progression' WHERE id=99", "UPDATE application_cycles SET education_stream='higher_education' WHERE id=99"):
            db=fixture()
            try:
                db.executescript(MIGRATION.read_text())
                original=self.db
                self.db=db
                self.pending()
                self.db.execute("INSERT INTO student_cases(id,student_id) VALUES(99,'new')")
                self.db.execute("INSERT INTO application_cycles VALUES(99,'new','transfer','school_education','active',NULL,99)")
                self.db.execute("UPDATE contracts SET contract_status='FullyExecuted',application_cycle_id=99 WHERE id=100")
                self.db.execute("INSERT INTO transfer_cases VALUES(99,100,'new_transfer')")
                self.db.execute("INSERT INTO transfer_v2_origin_events(id,application_cycle_id,new_source_label) VALUES(99,99,'new_transfer')")
                self.db.execute(target_sql)
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute("UPDATE transfer_source_authorities SET lifecycle_state='consumed',source_classification='new_transfer',application_cycle_id=99,verified_by=1,verified_at='today'")
            finally:
                self.db=original
                db.close()

if __name__ == "__main__": unittest.main()
