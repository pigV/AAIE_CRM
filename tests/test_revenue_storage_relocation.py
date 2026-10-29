import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from tests.revenue_secure_test_support import secure_directory
from revenue.secure_storage import write_immutable

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'migrations/20260908_036_revenue_storage_locations.sql'

class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.root = secure_directory(self)
        self.legacy = self.root/'revenue_documents'
        self.secure = self.root/'secure'
        self.legacy.mkdir(mode=0o700); self.secure.mkdir(mode=0o700)
        self.dbpath = ROOT/'SAFEBOX/storage-relocation-20260908'/('test-'+self.root.name+'.sqlite3')
        self.c = sqlite3.connect(self.dbpath)
        self.c.row_factory = sqlite3.Row
        self.addCleanup(self.c.close)
        self.c.executescript('''
          CREATE TABLE revenue_documents(id INTEGER PRIMARY KEY,uid TEXT UNIQUE,document_kind TEXT,
            storage_path TEXT,sha256 TEXT,size_bytes INTEGER,mime_type TEXT);
          CREATE TABLE revenue_payments(id INTEGER PRIMARY KEY,evidence_document_id INTEGER);
          CREATE TABLE revenue_receipts(id INTEGER PRIMARY KEY,document_id INTEGER);
          CREATE TRIGGER revenue_documents_financial_no_update BEFORE UPDATE ON revenue_documents
          WHEN EXISTS(SELECT 1 FROM revenue_payments p WHERE p.evidence_document_id=OLD.id)
            OR EXISTS(SELECT 1 FROM revenue_receipts r WHERE r.document_id=OLD.id)
          BEGIN SELECT RAISE(ABORT,'Issued Revenue financial document is immutable'); END;
          CREATE TRIGGER revenue_documents_financial_no_delete BEFORE DELETE ON revenue_documents
          WHEN EXISTS(SELECT 1 FROM revenue_payments p WHERE p.evidence_document_id=OLD.id)
            OR EXISTS(SELECT 1 FROM revenue_receipts r WHERE r.document_id=OLD.id)
          BEGIN SELECT RAISE(ABORT,'Issued Revenue financial document is immutable'); END;
        ''')
        self.payload = b'%PDF-1.4\nSynthetic relocation acceptance'
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.path = write_immutable(self.legacy,'test.pdf',self.payload)[0]
        self.c.execute('INSERT INTO revenue_documents VALUES(1,?,?,?,?,?,?)',
            ('document-test','receipt',str(self.path),self.sha,len(self.payload),'application/pdf'))
        self.c.execute('INSERT INTO revenue_receipts VALUES(1,1)')
        self.c.commit()

    def install(self):
        self.c.executescript(MIGRATION.read_text())
        self.c.commit()

    def plan(self, suffix='one'):
        from tools.revenue_storage_relocate import schema_digest
        return {'version':1, 'authority':'isolated-gate-test', 'reason':'synthetic storage acceptance',
            'legacy_root':str(self.legacy), 'secure_root':str(self.secure),
            'schema_sha256':schema_digest(self.c), 'operations':[{
                'operation_id':suffix, 'document':dict(self.c.execute('SELECT * FROM revenue_documents WHERE id=1').fetchone()),
                'expected_path':str(self.path),'expected_sequence':0,
                'new_path':str(self.secure/'test.pdf'),'kind':'relocate'}]}

    def relocate(self, plan=None):
        from tools.revenue_storage_relocate import execute_plan
        return execute_plan(self.dbpath, plan or self.plan())

    def effective(self):
        from revenue.storage_locations import resolve_location
        doc=dict(self.c.execute('SELECT * FROM revenue_documents WHERE id=1').fetchone())
        return resolve_location(doc,lambda q,p=():self.c.execute(q,p).fetchall())

    def test_success_audit_and_reverse_preserve_financial_row(self):
        self.install(); plan=self.plan(); before=plan['operations'][0]['document']
        self.relocate(plan)
        self.assertEqual(str(self.secure/'test.pdf'),self.effective())
        self.assertEqual(before,dict(self.c.execute('SELECT * FROM revenue_documents WHERE id=1').fetchone()))
        event=dict(self.c.execute('SELECT * FROM revenue_document_locations').fetchone())
        for k in ['authority','reason','occurred_at','manifest_sha256','document_fingerprint']:
            self.assertTrue(event[k])
        identity=json.loads(event['authority'])
        self.assertEqual('isolated-gate-test',identity['approval'])
        self.assertEqual(__import__('os').geteuid(),identity['uid'])
        self.assertEqual(64,len(identity['tool_sha256']))
        self.assertEqual(self.sha,event['source_sha256']);self.assertEqual(self.sha,event['destination_sha256'])
        restore=self.plan('restore-one'); op=restore['operations'][0]
        op.update(expected_path=event['new_path'],expected_sequence=1,new_path=event['old_path'],kind='restore')
        self.relocate(restore)
        self.assertEqual(str(self.path),self.effective())
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM revenue_document_locations').fetchone()[0])
        self.assertTrue(self.path.exists());self.assertTrue((self.secure/'test.pdf').exists())
        self.assertEqual(before,dict(self.c.execute('SELECT * FROM revenue_documents WHERE id=1').fetchone()))

    def test_wrong_old_path_has_no_db_change(self):
        self.install();p=self.plan();p['operations'][0]['expected_path']=str(self.legacy/'wrong.pdf')
        with self.assertRaises(ValueError):self.relocate(p)
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_document_locations').fetchone()[0])

    def test_wrong_target_checksum_does_not_update(self):
        self.install();write_immutable(self.secure,'test.pdf',b'incorrect')
        with self.assertRaises(ValueError):self.relocate()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_document_locations').fetchone()[0])
        self.assertEqual(str(self.path),self.effective())

    def test_ordinary_issued_update_still_fails_after_relocation(self):
        self.install();self.relocate()
        for column,value in [('storage_path','bad'),('sha256','0'*64),('document_kind','bad'),('uid','bad')]:
            with self.subTest(column=column),self.assertRaises(sqlite3.IntegrityError) as e:
                self.c.execute(f'UPDATE revenue_documents SET {column}=? WHERE id=1',(value,))
            self.assertEqual(sqlite3.SQLITE_CONSTRAINT_TRIGGER,e.exception.sqlite_errorcode)

    def test_ordinary_runtime_cannot_append_or_mutate_audit(self):
        self.install();self.relocate()
        from tools.revenue_storage_relocate import EVENT_COLUMNS
        event=dict(self.c.execute('SELECT * FROM revenue_document_locations').fetchone())
        event.update(operation_id='forged',sequence=2,previous_event_id=event['id'],old_path=event['new_path'],new_path=str(self.secure/'forged.pdf'))
        with self.assertRaises(sqlite3.Error):
            self.c.execute('INSERT INTO revenue_document_locations('+','.join(EVENT_COLUMNS)+') VALUES('+','.join('?' for _ in EVENT_COLUMNS)+')',tuple(event[k] for k in EVENT_COLUMNS))
        for sql in ['UPDATE revenue_document_locations SET new_path=new_path','DELETE FROM revenue_document_locations']:
            with self.assertRaises(sqlite3.IntegrityError):self.c.execute(sql)
        self.c.rollback()

    def test_idempotent_replay_no_duplicate_and_changed_request_rejected(self):
        self.install();p=self.plan();self.relocate(p);self.relocate(p)
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM revenue_document_locations').fetchone()[0])
        p['reason']='changed request'
        with self.assertRaises(ValueError):self.relocate(p)

    def test_corrupt_effective_file_never_falls_back_to_retained_legacy(self):
        self.install();self.relocate();(self.secure/'test.pdf').write_bytes(b'bad')
        from revenue.storage_locations import read_document
        doc=dict(self.c.execute('SELECT * FROM revenue_documents WHERE id=1').fetchone())
        with self.assertRaises(ValueError):
            read_document(doc,lambda q,p=():self.c.execute(q,p).fetchall(),self.legacy,self.secure)

    def test_restore_can_recover_from_corrupt_current_copy(self):
        self.install();self.relocate();(self.secure/'test.pdf').write_bytes(b'bad')
        p=self.plan('restore');p['operations'][0].update(expected_path=str(self.secure/'test.pdf'),expected_sequence=1,new_path=str(self.path),kind='restore')
        self.relocate(p)
        self.assertEqual(str(self.path),self.effective())
        event=self.c.execute('SELECT * FROM revenue_document_locations ORDER BY id DESC').fetchone()
        self.assertIn(event['source_verification'],['MISMATCH','UNAVAILABLE'])

    def test_transaction_error_retains_copy_without_publishing_event(self):
        self.install();from unittest.mock import patch
        with patch('tools.revenue_storage_relocate._append',side_effect=sqlite3.OperationalError('injected transaction failure')):
            with self.assertRaises(sqlite3.Error):self.relocate()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_document_locations').fetchone()[0])
        self.assertTrue((self.secure/'test.pdf').exists());self.assertTrue(self.path.exists())

    def test_application_verification_failure_restores_via_audited_authority(self):
        self.install();from unittest.mock import patch
        from tools import revenue_storage_relocate as migration
        original=migration._application_verify; calls=[]
        def fail_once(*args):
            calls.append(1)
            if len(calls)==1:raise ValueError('injected application verification failure')
            return original(*args)
        with patch.object(migration,'_application_verify',side_effect=fail_once):
            with self.assertRaises(ValueError):self.relocate()
        self.assertEqual(str(self.path),self.effective())
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM revenue_document_locations').fetchone()[0])

    def test_application_verifier_rejects_different_current_authority(self):
        self.install();p=self.plan();self.relocate(p)
        restore=self.plan('external-restore');restore['operations'][0].update(expected_path=str(self.secure/'test.pdf'),expected_sequence=1,new_path=str(self.path),kind='restore')
        self.relocate(restore)
        from tools.revenue_storage_relocate import _application_verify
        with self.assertRaises(ValueError):_application_verify(self.dbpath,p['operations'],self.legacy,self.secure)

    def test_mixed_batch_modes_rejected_before_copy(self):
        self.install();p=self.plan();op=dict(p['operations'][0]);op.update(operation_id='mixed',kind='restore',document=dict(op['document'],id=2))
        p['operations'].append(op)
        with self.assertRaises(ValueError):self.relocate(p)
        self.assertFalse((self.secure/'test.pdf').exists())

    def test_absent_location_and_absent_ledger_is_ambiguous_not_legacy(self):
        from revenue.secure_storage import StorageUnavailable
        with self.assertRaises(StorageUnavailable):self.effective()

    def test_known_pre036_schema_can_read_original_authority(self):
        self.c.execute('CREATE TABLE schema_migrations(version TEXT,name TEXT)')
        self.c.execute("INSERT INTO schema_migrations VALUES('20260901_034','revenue_client_payment_receipt')")
        self.assertEqual(str(self.path),self.effective())

    def test_real_isolated_http_legacy_secure_restore_and_authorization(self):
        import app, os, threading, urllib.request, urllib.error
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from types import SimpleNamespace
        from unittest.mock import patch
        self.install()
        def all_rows(sql, parameters=()):
            c=sqlite3.connect(self.dbpath);c.row_factory=sqlite3.Row
            try:return c.execute(sql,parameters).fetchall()
            finally:c.close()
        facade=SimpleNamespace(path=self.root/'test.db',all=all_rows,
            one=lambda q,p=(): next(iter(all_rows(q,p)),None))
        class H(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def error_page(self,message,status):
                self.send_response(int(status));self.end_headers()
            def do_GET(self):
                # Synthetic harness identity fixture only; real CRM Admin policy is used below.
                role=self.headers.get('X-Synthetic-Role')
                self.user={'role_name':role} if role else None
                app.CRMHandler.revenue_document(self,'GET','document-test','view')
        server=HTTPServer(('127.0.0.1',0),H)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def get(role):
            req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/synthetic-document',
                headers={'X-Synthetic-Role':role} if role else {})
            try:
                with urllib.request.urlopen(req) as response:return response.status,response.read()
            except urllib.error.HTTPError as e:return e.code,e.read()
        try:
            with patch.object(app,'db',facade),patch.dict(os.environ,{'REVENUE_SECURE_STORAGE_ROOT':str(self.secure)}):
                self.assertEqual((200,self.payload),get('Admin'))
                self.relocate()
                self.assertEqual((200,self.payload),get('Admin'));self.assertEqual((200,self.payload),get('Admin'))
                for role in ('Staff','Manager','Finance',None):self.assertEqual(403,get(role)[0])
                (self.secure/'test.pdf').write_bytes(b'corrupt')
                self.assertEqual(404,get('Admin')[0])
                p=self.plan('http-restore');p['operations'][0].update(expected_path=str(self.secure/'test.pdf'),expected_sequence=1,new_path=str(self.path),kind='restore')
                self.relocate(p)
                self.assertEqual((200,self.payload),get('Admin'))
        finally:
            server.shutdown();server.server_close();thread.join()

    def two_document_plan(self):
        second=write_immutable(self.legacy,'second.pdf',self.payload)[0]
        self.c.execute('INSERT INTO revenue_documents VALUES(2,?,?,?,?,?,?)',
            ('document-second','payment',str(second),self.sha,len(self.payload),'application/pdf'))
        self.c.execute('INSERT INTO revenue_payments VALUES(2,2)');self.c.commit()
        p=self.plan();p['operations'].append(dict(p['operations'][0],operation_id='two',
            document=dict(self.c.execute('SELECT * FROM revenue_documents WHERE id=2').fetchone()),
            expected_path=str(second),new_path=str(self.secure/'second.pdf')))
        return p

    def test_second_insert_failure_rolls_back_first_event_atomically(self):
        self.install();p=self.two_document_plan()
        from tools import revenue_storage_relocate as m
        from unittest.mock import patch
        actual=m._append;calls=[]
        def fail_second(*args):
            calls.append(1)
            if len(calls)==2:raise sqlite3.OperationalError('synthetic second insert failure')
            return actual(*args)
        with patch.object(m,'_append',side_effect=fail_second):
            with self.assertRaises(sqlite3.Error):self.relocate(p)
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_document_locations').fetchone()[0])
        for op in p['operations']:
            self.assertTrue(Path(op['new_path']).exists());self.assertTrue(Path(op['expected_path']).exists())
            self.assertEqual(op['document'],dict(self.c.execute('SELECT * FROM revenue_documents WHERE id=?',(op['document']['id'],)).fetchone()))

    def test_two_document_success_and_restore(self):
        self.install();p=self.two_document_plan();self.relocate(p)
        restore=dict(p);restore['operations']=[dict(o,operation_id=o['operation_id']+':restore',kind='restore',
            expected_path=o['new_path'],new_path=o['expected_path'],expected_sequence=1) for o in p['operations']]
        self.relocate(restore)
        self.assertEqual(4,self.c.execute('SELECT count(*) FROM revenue_document_locations').fetchone()[0])
        for op in p['operations']:
            self.assertEqual(op['document'],dict(self.c.execute('SELECT * FROM revenue_documents WHERE id=?',(op['document']['id'],)).fetchone()))

    def test_private_authorizer_denies_top_level_capability_and_business_dml(self):
        self.install()
        from tools.revenue_storage_relocate import _authorizer
        self.c.create_function('revenue_relocation_authorize',0,lambda:1)
        self.c.set_authorizer(_authorizer)
        for sql in ["SELECT revenue_relocation_authorize()","UPDATE revenue_documents SET uid=uid",'DROP TABLE revenue_documents',"ATTACH ':memory:' AS other"]:
            with self.subTest(sql=sql),self.assertRaises(sqlite3.DatabaseError):self.c.execute(sql)
        self.c.set_authorizer(None)

    def test_missing_schema_registered_in_ledger_fails_closed(self):
        self.c.execute('CREATE TABLE schema_migrations(version INTEGER,name TEXT)')
        self.c.execute("INSERT INTO schema_migrations VALUES(36,'revenue_storage_locations')")
        from revenue.secure_storage import StorageUnavailable
        with self.assertRaises(StorageUnavailable):self.effective()

    def test_feature_available_and_original_trigger_preserved(self):
        self.assertTrue(MIGRATION.exists(), 'controlled storage authority migration is missing')
        self.c.executescript(MIGRATION.read_text())
        with self.assertRaises(sqlite3.IntegrityError) as e:
            self.c.execute('UPDATE revenue_documents SET storage_path=? WHERE id=1',('changed',))
        self.assertEqual(sqlite3.SQLITE_CONSTRAINT_TRIGGER,e.exception.sqlite_errorcode)

if __name__ == '__main__': unittest.main()

class AvailabilityTests(unittest.TestCase):
    def test_controlled_authority_is_present(self):
        self.assertTrue((ROOT/'tools/revenue_storage_relocate.py').exists(), 'dedicated migration authority is missing')
