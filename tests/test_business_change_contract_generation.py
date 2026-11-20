from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from database import Database
from business_changes.schema import install
from contract_generation_cutover import create_contract_atomic, replace_draft_atomic


class ContractGenerationSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / 'generation.db')
        self.db.initialize()
        with self.db.transaction() as c:
            install(c)
            c.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19')")
            c.execute("INSERT INTO students(student_id,passport_last_name,passport_first_name,current_status,created_date,updated_at,form_data_json) VALUES('S1','Synthetic','Student','Active','2026-11-19','2026-11-19','{}')")
        self.bound = {'generation_request_id': 'generation-1', 'request_fingerprint': 'fingerprint-1',
                      'contract_type': 'NewInternationalStudentContract'}

    def create(self):
        return create_contract_atomic(self.db, bound=self.bound,
            rendered=SimpleNamespace(docx_bytes=b'synthetic contract bytes'), student_data={},
            student_id='S1', contract_uid='synthetic-contract', target=self.root / 'first.docx',
            file_version=1, user={'id': 1, 'username': 'admin'}, occurred_at='2026-11-19T00:00:00+00:00')

    def rebuild(self, contract_id):
        return replace_draft_atomic(self.db, contract_id=contract_id, student_id='S1',
            bound={**self.bound, 'generation_request_id': 'generation-2', 'request_fingerprint': 'fingerprint-2'},
            rendered=SimpleNamespace(docx_bytes=b'synthetic rebuilt bytes'), student_data={},
            target=self.root / 'second.docx', file_version=2,
            user={'id': 1, 'username': 'admin'}, occurred_at='2026-11-19T00:01:00+00:00')

    def test_generation_and_rebuild_keep_original_retry_identity(self):
        first = self.create()
        self.assertFalse(self.create().created)
        self.rebuild(first.contract_id)
        self.assertFalse(self.rebuild(first.contract_id).created)
        self.assertEqual(['contract.generated', 'contract.draft_regenerated'],
            [r[0] for r in self.db.all('SELECT event_type FROM business_change_events ORDER BY seq')])
        self.assertEqual(2, self.db.one("SELECT version FROM business_change_versions WHERE subject_kind='contract'")[0])
        self.assertEqual(2, self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(b'synthetic rebuilt bytes', (self.root / 'second.docx').read_bytes())

    def test_generation_publication_failure_compensates_rows_and_new_file(self):
        with patch('business_changes.commands.execute_command', side_effect=RuntimeError('synthetic publication failure')):
            with self.assertRaises(RuntimeError): self.create()
        self.assertEqual(0, self.db.one('SELECT count(*) FROM contracts')[0])
        self.assertEqual(0, self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(0, self.db.one('SELECT count(*) FROM business_change_events')[0])
        self.assertFalse((self.root / 'first.docx').exists())
        self.assertEqual([], list(self.root.glob('*.staging')))

    def test_failed_rebuild_keeps_previous_contract_and_file(self):
        first = self.create()
        with patch('business_changes.commands.execute_command', side_effect=RuntimeError('synthetic publication failure')):
            with self.assertRaises(RuntimeError): self.rebuild(first.contract_id)
        self.assertEqual(str(self.root / 'first.docx'), self.db.one('SELECT contract_file_path FROM contracts')[0])
        self.assertTrue((self.root / 'first.docx').is_file())
        self.assertFalse((self.root / 'second.docx').exists())
        self.assertEqual(1, self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(1, self.db.one('SELECT count(*) FROM business_change_events')[0])
