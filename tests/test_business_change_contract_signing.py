from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
from database import Database
from business_changes.schema import install


class ContractSigningLinkSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'signing.db');self.db.initialize()
        with self.db.transaction() as c:
            install(c)
            c.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19')")
            c.execute("INSERT INTO students(student_id,passport_first_name,passport_last_name,created_date,updated_at,form_data_json) VALUES('S1','Synthetic','Student','2026-11-19','2026-11-19','{}')")
            self.cid=c.execute("INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,contract_title,contract_status,updated_at) VALUES('S1','synthetic','study','StudyAbroadContract','Synthetic','Draft','2026-11-19')").lastrowid
        self.handler=object.__new__(app.CRMHandler)
        self.user={'id':1,'username':'admin','display_name':'Admin'}
        self.handler.contract_signing_source_pdf=lambda contract:Path('synthetic-prepared.pdf')
        self.scope=patch.object(app,'db',self.db);self.scope.start();self.addCleanup(self.scope.stop)
        # The PDF validation/rendering precedes the transaction under test.
        self.pdf=patch.object(app,'validate_pdf_contains_contract_body');self.pdf.start();self.addCleanup(self.pdf.stop)

    def link(self,regenerate=False):
        return self.handler.contract_signing_create_link(self.cid,self.user,regenerate=regenerate)

    def test_native_link_and_sent_fact_commit_once_and_reuse_active_link(self):
        first=self.link();second=self.link()
        self.assertEqual(first['id'],second['id'])
        self.assertEqual('Sent',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM contract_signing_links')[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.signing_link_changed'")[0])

    def test_publication_failure_rolls_back_native_link_sent_status_and_audit(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.link()
        self.assertEqual('Draft',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM contract_signing_links')[0])
        self.assertEqual(0,self.db.one("SELECT count(*) FROM audit_logs WHERE module_name='contracts'")[0])

    def test_explicit_regeneration_supersedes_old_link_in_same_change(self):
        first=self.link();second=self.link(regenerate=True)
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual('superseded',self.db.one('SELECT status FROM contract_signing_links WHERE id=?',(first['id'],))[0])
        self.assertEqual(2,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.signing_link_changed'")[0])
