import ast
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/20260907_035_visa_management_authorities.sql"

def legacy_db():
    tree = ast.parse((ROOT / "database.py").read_text())
    schema = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SCHEMA" for t in n.targets))
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    conn.executescript("CREATE TABLE application_cycles(id INTEGER PRIMARY KEY); CREATE TABLE visa_decision_events(id INTEGER PRIMARY KEY); ALTER TABLE visa_cases ADD COLUMN application_cycle_id INTEGER REFERENCES application_cycles(id); ALTER TABLE visa_cases ADD COLUMN lodged_by_user_id INTEGER REFERENCES users(id); ALTER TABLE visa_cases ADD COLUMN lodged_by_name TEXT; ALTER TABLE visa_cases ADD COLUMN lodgement_recorded_at TEXT;")
    conn.executescript("INSERT INTO roles(id,name,permissions_json) VALUES(1,'Admin','{}'); INSERT INTO users(id,username,display_name,password_hash,role_id,created_at) VALUES(1,'test','Test','none',1,'now');")
    conn.executescript("CREATE TRIGGER visa_decision_events_ownership_insert BEFORE INSERT ON visa_decision_events BEGIN SELECT 1; END;")
    from tests.visa_test_schema import prepare_production_visa_schema
    prepare_production_visa_schema(conn)
    return conn

class VisaApplicantTests(unittest.TestCase):
    def setUp(self):
        self.conn=legacy_db()
    def tearDown(self): self.conn.close()
    def migrated(self):
        self.assertTrue(MIGRATION.exists(), "Explicit Visa migration is missing")
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.executescript(MIGRATION.read_text())
        self.conn.execute("PRAGMA foreign_keys=ON")
    def test_nullable_student_case_without_fake_student(self):
        self.migrated()
        from visa_applicant_candidate import create_applicant, create_case
        applicant=create_applicant(self.conn, name="Visa Only", email="visa@example.test", actor_id=1, stamp="2026-09-07")
        case=create_case(self.conn, applicant_id=applicant, subclass="500", actor_id=1, stamp="2026-09-07")
        row=self.conn.execute("SELECT * FROM visa_cases WHERE id=?",(case,)).fetchone()
        self.assertIsNone(row["student_id"])
        self.assertEqual(row["applicant_id"], applicant)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM students").fetchone()[0],0)
    def test_migration_preserves_all_legacy_case_values(self):
        self.conn.execute("INSERT INTO students(student_id,passport_last_name,passport_first_name,created_date,updated_at,form_data_json) VALUES('LEGACY','L','E','old','old','{}')")
        self.conn.execute("INSERT INTO visa_cases(id,student_id,visa_case_name,created_at,updated_at,guardian_applicant) VALUES(1,'LEGACY','Legacy','old','old','Relative + 590')")
        before=dict(self.conn.execute("SELECT * FROM visa_cases").fetchone())
        self.migrated()
        after=dict(self.conn.execute("SELECT * FROM visa_cases WHERE id=1").fetchone())
        self.assertEqual(before,{key:after[key] for key in before})
    def test_manual_link_retains_two_entities_and_audit(self):
        self.migrated()
        from visa_applicant_candidate import create_applicant, create_case, link_student
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("INSERT INTO students(student_id,passport_last_name,passport_first_name,created_date,updated_at,form_data_json) VALUES('S1','Last','First','now','now','{}')")
        aid=create_applicant(self.conn,name="Independent",email="",actor_id=1,stamp="now")
        cid=create_case(self.conn,applicant_id=aid,subclass="590",actor_id=1,stamp="now")
        link_student(self.conn,applicant_id=aid,student_id="S1",actor_id=1,stamp="now")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM visa_applicants").fetchone()[0],1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM students").fetchone()[0],1)
        self.assertIsNone(self.conn.execute("SELECT student_id FROM visa_cases WHERE id=?",(cid,)).fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM visa_applicant_student_links").fetchone()[0],1)
    def test_missing_student_link_fails(self):
        self.migrated()
        from visa_applicant_candidate import link_student
        with self.assertRaises(ValueError): link_student(self.conn,applicant_id=999,student_id="none",actor_id=1,stamp="now")
if __name__ == "__main__": unittest.main()
