"""Visa list batching preserves the existing case and evidence authority."""
import json
from pathlib import Path
import sqlite3
import unittest

from tests.test_visa_documents_candidate import SCHEMA, STAMP
from visa_document_lifecycle import active_documents


class StudentListVisaBatchingTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.executescript((Path(__file__).parents[1] /
            'migrations/20260909_037_visa_document_metadata.sql').read_text())
        self.connection.execute("UPDATE visa_cases SET student_id='S1' WHERE id=1")
        self.connection.execute("""CREATE TABLE visa_case_document_links(
            visa_case_id INTEGER,student_document_id INTEGER,document_id INTEGER,
            document_category TEXT,document_type TEXT,used_in_this_case INTEGER,status TEXT
        )""")

    def test_active_documents_batch_sources_and_keep_case_ownership(self):
        connection = self.connection
        number_of_sources = 40
        for i in range(1, number_of_sources + 1):
            owner = 'FOREIGN' if i == 4 else 'S1'
            connection.execute('INSERT INTO student_documents VALUES(?,?,?,?,?,?,?,?,?)',
                (i, owner, '/synthetic/source.pdf', 'source.pdf', 1, 'VisaGrantLetter',
                 STAMP, int(i != 3), 'active'))
            connection.execute("""INSERT INTO visa_case_documents(
                id,visa_case_id,student_document_id,file_path,original_name,version,
                sha256,created_by,created_at,archived_at
            ) VALUES(?,1,?,?,'source.pdf',1,?,1,?,?)""",
                (i, i, '/synthetic/source.pdf', str(i).zfill(64), STAMP, STAMP if i == 5 else None))
        for i in (1, 3, 4, 5, 41):
            connection.execute("INSERT INTO visa_case_document_links VALUES(1,?,?,'Visa Result / S56','IMMI Grant Letter',1,'Linked')",(i,i))
        connection.execute('INSERT INTO student_documents VALUES(?,?,?,?,?,?,?,?,?)',
            (41, 'S1', '/synthetic/legacy.pdf', 'legacy.pdf', 1, 'VisaGrantLetter',
             STAMP, 1, 'active'))
        connection.execute("""INSERT INTO visa_document_metadata(document_id,manual_category,updated_by,updated_at)
            VALUES(1,'other',1,?)""", (STAMP,))
        for category in ('grant_letter', 'immi_acknowledgment'):
            connection.execute("""INSERT INTO visa_document_audit(
                visa_case_id,document_id,action,before_json,after_json,actor_user_id,occurred_at
            ) VALUES(1,2,'document_category_selected','{}',?,1,?)""",
            (json.dumps({'category': category, 'confirmed': True}), STAMP))
        statements = []
        connection.set_trace_callback(statements.append)
        try:
            documents = active_documents(connection, 1)
        finally:
            connection.set_trace_callback(None)
        by_source = {row['student_document_id']: row for row in documents}
        self.assertEqual(set(range(1, 42)) - {3, 4, 5}, set(by_source))
        self.assertEqual('other', by_source[1]['authority_category'])
        self.assertEqual('immi_acknowledgment', by_source[2]['authority_category'])
        self.assertIsNone(by_source[41]['id'])
        self.assertEqual('grant_letter', by_source[41]['authority_category'])
        self.assertLess(len(statements), 16, 'SQL should be batched by Case, not per document')

    def test_projection_map_uses_active_cycle_latest_case_and_keeps_empty_date(self):
        from visa_document_lifecycle import student_grant_date_map
        connection = self.connection
        connection.executescript("""
            ALTER TABLE visa_cases ADD COLUMN visa_subclass TEXT;
            ALTER TABLE visa_cases ADD COLUMN created_at TEXT;
            INSERT INTO application_cycles(id,student_id,status,opened_at) VALUES
              (10,'S1','active','2026-07-01'),(11,'S1','closed','2026-09-01'),(12,'S2','active','2026-09-01');
            UPDATE visa_cases SET application_cycle_id=10,visa_subclass='500',created_at='2026-08-01' WHERE id=1;
            INSERT INTO visa_cases(id,student_id,application_cycle_id,visa_subclass,created_at,archived_at) VALUES
              (3,'S1',11,'500','2026-11-01',NULL),(4,'S1',10,'590','2026-12-01',NULL),
              (5,'S1',10,'500','2026-10-01','2026-10-02'),(6,'S1',10,NULL,'2026-07-01',NULL),
              (7,'S2',NULL,NULL,NULL,NULL),(8,'S3',NULL,NULL,NULL,NULL);
            INSERT INTO visa_case_documents(id,visa_case_id,file_path,original_name,version,sha256,created_by,created_at)
              VALUES(101,1,'/synthetic/grant.pdf','grant.pdf',1,'hash',1,'2026-08-01');
            INSERT INTO visa_document_audit(visa_case_id,document_id,action,before_json,after_json,actor_user_id,occurred_at)
              VALUES(1,101,'document_category_selected','{}','{"category":"grant_letter","confirmed":true}',1,'2026-08-01');
            INSERT INTO visa_letter_extractions(visa_case_id,document_id,proposed_json,extraction_method,created_by,created_at)
              VALUES(1,101,'{"document_kind":"grant","decision_date":"2026-08-06"}','text',1,'2026-08-01');
        """)
        self.assertEqual({'S1':'2026-08-06','S3':''},
                         student_grant_date_map(connection, ('S1','S2','S3','S1')))
