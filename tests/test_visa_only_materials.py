import sqlite3
import unittest
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


class VisaOnlyMaterialOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE visa_cases(id INTEGER PRIMARY KEY, student_id TEXT, applicant_id INTEGER, archived_at TEXT);
            CREATE TABLE visa_checklist_items(id INTEGER PRIMARY KEY, visa_case_id INTEGER);
            CREATE TABLE visa_case_documents(
                id INTEGER PRIMARY KEY, visa_case_id INTEGER, student_document_id INTEGER, sha256 TEXT, file_path TEXT, archived_at TEXT
            );
            INSERT INTO visa_cases VALUES (1,NULL,7,NULL),(2,NULL,8,NULL),(3,'S1',NULL,NULL);
            INSERT INTO visa_checklist_items VALUES (11,1),(22,2);
            INSERT INTO visa_case_documents VALUES (10,1,NULL,'digest','/one',NULL),(20,2,NULL,'digest','/one',NULL);
        """)
        self.db.executescript((MIGRATIONS / "20261117_049_visa_only_materials.sql").read_text())

    def pending(self, case=1):
        return self.db.execute(
            """INSERT INTO visa_only_material_uploads(visa_case_id, original_name, file_path, sha256)
               VALUES (?, 'passport.pdf', '/one', 'digest')""", (case,)
        ).lastrowid

    def test_pending_to_confirmed_requires_owned_item_and_document(self):
        upload = self.pending()
        for item, doc in ((22,10),(11,20),(11,None)):
            with self.subTest(item=item,doc=doc), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute("UPDATE visa_only_material_uploads SET item_id=?,document_id=? WHERE id=?",
                                (item,doc,upload))
        self.db.execute("UPDATE visa_only_material_uploads SET item_id=11,document_id=10 WHERE id=?", (upload,))
        self.assertEqual((11,10), self.db.execute(
            "SELECT item_id,document_id FROM visa_only_material_uploads WHERE id=?", (upload,)
        ).fetchone())

    def test_same_digest_reuses_pending_case_and_cannot_target_student_case(self):
        self.pending()
        self.pending(2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.pending()
        with self.assertRaises(sqlite3.IntegrityError):
            self.pending(3)
