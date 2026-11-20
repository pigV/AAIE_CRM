from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from database import Database
from business_changes.schema import install
from business_changes.contract_records import invalidate_contract


class ContractInvalidationSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.db=Database(Path(self.tmp.name)/'contracts.db')
        with self.db.transaction() as c:
            install(c);c.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
            c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
            c.execute('CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT)');c.execute("INSERT INTO application_cycles VALUES(7,'S1')")
            c.execute('CREATE TABLE contracts(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER,contract_status TEXT,signed_file_path TEXT,final_signed_file_path TEXT)')
            c.execute("INSERT INTO contracts VALUES(8,'S1',7,'FullyExecuted','/synthetic/signed.pdf','')")
            c.execute('CREATE TABLE contract_signing_links(id INTEGER PRIMARY KEY,contract_id INTEGER,status TEXT)');c.execute("INSERT INTO contract_signing_links VALUES(9,8,'active')")
            c.execute('CREATE TABLE student_documents(id INTEGER PRIMARY KEY,student_id TEXT,file_path TEXT,document_status TEXT)')
            c.execute("INSERT INTO student_documents VALUES(10,'S1','/synthetic/signed.pdf','active')")
    def void(self):
        def legacy(row):
            self.db.execute("UPDATE contracts SET contract_status='Voided' WHERE id=?",(row['id'],))
            self.db.execute("UPDATE contract_signing_links SET status='revoked' WHERE contract_id=?",(row['id'],))
            self.db.execute("UPDATE student_documents SET document_status='voided' WHERE file_path=?",(row['signed_file_path'],))
            return 'Voided'
        with self.db.transaction() as c:return invalidate_contract(c,database=self.db,contract_id=8,actor_user_id=1,operation=legacy)
    def test_legacy_void_links_document_and_event_share_outer_commit(self):
        self.assertEqual('Voided',self.void());self.assertEqual('Voided',self.void())
        self.assertEqual(1,self.db.one('SELECT count(*) FROM business_change_events')[0])
        self.assertEqual('revoked',self.db.one('SELECT status FROM contract_signing_links')[0])
        self.assertEqual('voided',self.db.one('SELECT document_status FROM student_documents')[0])
    def test_publication_failure_rolls_back_entire_legacy_helper(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.void()
        self.assertEqual('FullyExecuted',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual('active',self.db.one('SELECT status FROM contract_signing_links')[0])
        self.assertEqual('active',self.db.one('SELECT document_status FROM student_documents')[0])
    def test_all_actual_shared_document_owners_receive_versions(self):
        self.db.execute("INSERT INTO student_documents VALUES(11,'S2','/synthetic/signed.pdf','active')")
        self.void()
        owners={r[0] for r in self.db.all("SELECT subject_id FROM business_change_impacts WHERE subject_kind='student'")}
        self.assertEqual({'S1','S2'},owners)
