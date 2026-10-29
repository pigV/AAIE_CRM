"""Synthetic authority fixtures; tests do not represent migration acceptance."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest


def document_entries(folder):
    """Ignore only verified AppleDouble metadata in fixture directory assertions."""
    result=[]
    for path in folder.iterdir():
        metadata=False
        if path.name.startswith('._') and path.is_file() and not path.is_symlink():
            raw=path.read_bytes()
            if len(raw)>=26 and raw[:8]==b'\x00\x05\x16\x07\x00\x02\x00\x00':
                count=struct.unpack('>H',raw[24:26])[0]
                if 0<count and 26+12*count<=len(raw):
                    entries=[struct.unpack('>III',raw[26+i*12:38+i*12]) for i in range(count)]
                    metadata=all(offset>=26+12*count and offset+size<=len(raw) for _,offset,size in entries)
        if not metadata:result.append(path)
    return result

class TransferPaymentProofAppendTests(unittest.TestCase):
    def setUp(self):
        from student_lifecycle import transfer_payment_proof_append as subject
        self.subject=subject
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.folder=self.root/'Payments';self.folder.mkdir()
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row;self.addCleanup(self.conn.close)
        self.conn.executescript('''
        CREATE TABLE schema_migrations(version TEXT,applied_at TEXT);
        INSERT INTO schema_migrations VALUES('20260904_035','2026-09-01T00:00:00+10:00');
        CREATE TABLE student_cases(id INTEGER,student_id TEXT,case_type TEXT,status TEXT);
        INSERT INTO student_cases VALUES(1,'S1','transfer','completed');
        CREATE TABLE application_cycles(id INTEGER,student_id TEXT,cycle_type TEXT,status TEXT,outcome TEXT,created_at TEXT,source_student_case_id INTEGER);
        INSERT INTO application_cycles VALUES(2,'S1','transfer','closed','completed','2026-09-07T00:00:00+10:00',1);
        CREATE TABLE transfer_cases(case_id INTEGER,v2_version INTEGER,source_contract_id INTEGER,transfer_source_label TEXT);
        INSERT INTO transfer_cases VALUES(1,2,100,'new_transfer');
        CREATE TABLE transfer_school_applications(id INTEGER,case_id INTEGER,application_cycle_id INTEGER,removed_at TEXT);
        INSERT INTO transfer_school_applications VALUES(3,1,2,NULL);
        CREATE TABLE payments(id INTEGER PRIMARY KEY,student_id TEXT,amount REAL,currency TEXT,paid_date TEXT,payment_category TEXT,receipt_file_path TEXT);
        INSERT INTO payments VALUES(5,'S1',1200,'AUD','2026-09-07','Offer Deposit',NULL);
        CREATE TABLE case_record_links(id INTEGER PRIMARY KEY,case_id INTEGER,transfer_application_id INTEGER,record_type TEXT,record_id INTEGER,document_id INTEGER,link_role TEXT,validity_status TEXT,effective_from TEXT,created_by INTEGER,created_at TEXT);
        INSERT INTO case_record_links VALUES(4,1,3,'payment',5,NULL,'current','current','2026-09-07',1,'2026-09-07');
        CREATE TABLE transfer_payment_confirmations(id INTEGER,payment_link_id INTEGER);
        CREATE TABLE transfer_completion_events(id INTEGER,application_cycle_id INTEGER);
        INSERT INTO transfer_completion_events VALUES(8,2);
        CREATE TABLE transfer_source_authorities(id INTEGER,student_id TEXT,application_cycle_id INTEGER,transfer_contract_id INTEGER,lifecycle_state TEXT,source_classification TEXT);
        INSERT INTO transfer_source_authorities VALUES(7,'S1',2,100,'consumed','new_transfer');
        CREATE VIEW transfer_existing_source_evidence AS SELECT 1;
        CREATE TABLE contracts(id INTEGER,student_id TEXT,application_cycle_id INTEGER,contract_type TEXT);
        INSERT INTO contracts VALUES(100,'S1',2,'TransferApplicationContract');
        CREATE TABLE student_documents(id INTEGER PRIMARY KEY,student_id TEXT,folder_category TEXT,document_type TEXT,original_name TEXT,stored_name TEXT,file_path TEXT,version INTEGER,file_size INTEGER,uploaded_by INTEGER,uploaded_at TEXT,is_active INTEGER);
        CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id INTEGER,username TEXT,action_type TEXT,module_name TEXT,module_key TEXT,record_id TEXT,file_path TEXT,student_id TEXT,status TEXT,created_at TEXT);
        ''')
        self.commit_failure=False
        @contextmanager
        def transaction():
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                yield self.conn
                if self.commit_failure: raise RuntimeError('synthetic commit failure')
                self.conn.commit()
            except Exception:
                self.conn.rollback();raise
        self.db=type('DB',(),{'transaction':staticmethod(transaction)})()

    def append(self,**overrides):
        args=dict(student_id='S1',payment_id=5,payment_link_id=4,filename='receipt.pdf',data=b'%PDF-1.4\nsynthetic proof',actor_user_id=1,actor_username='synthetic',occurred_at='2026-09-08',folder_provider=lambda:self.folder)
        args.update(overrides);return self.subject.append_payment_proof(self.db,**args)

    def frozen(self):
        return {t:[tuple(r) for r in self.conn.execute('SELECT * FROM '+t)] for t in ('student_cases','application_cycles','transfer_cases','transfer_school_applications','transfer_source_authorities','transfer_payment_confirmations','transfer_completion_events','contracts')}

    def test_completed_same_payment_append_and_exact_retry_preserve_facts(self):
        before=self.frozen();payment=tuple(self.conn.execute('SELECT * FROM payments').fetchone())
        result=self.append();retry=self.append()
        self.assertEqual(result.document_id,retry.document_id);self.assertTrue(retry.replayed)
        self.assertEqual(self.frozen(),before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM transfer_payment_confirmations').fetchone()[0],0)
        after=tuple(self.conn.execute('SELECT * FROM payments').fetchone());self.assertEqual(payment[:-1],after[:-1])
        self.assertEqual(len(document_entries(self.folder)),1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM student_documents').fetchone()[0],1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM case_record_links WHERE record_type='payment'").fetchone()[0],1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM audit_logs').fetchone()[0],1)

    def test_invalid_authority_rejected_before_filesystem(self):
        for sql in ("UPDATE application_cycles SET created_at='2020-01-01'","UPDATE transfer_source_authorities SET lifecycle_state='pending'","UPDATE case_record_links SET validity_status='historical'","UPDATE transfer_school_applications SET case_id=999","UPDATE application_cycles SET outcome='cancelled'"):
            with self.subTest(sql=sql):
                self.conn.execute('SAVEPOINT invalid');self.conn.execute(sql)
                with self.assertRaises(self.subject.PaymentProofAppendError):
                    self.subject.resolve_payment_proof_target(self.conn,student_id='S1',payment_id=5,payment_link_id=4)
                self.conn.execute('ROLLBACK TO invalid');self.conn.execute('RELEASE invalid')
        with self.assertRaises(self.subject.PaymentProofAppendError):self.append(student_id='OTHER',folder_provider=lambda:self.fail('filesystem reached'))
        self.assertEqual(document_entries(self.folder),[])

    def test_replacement_is_refused_without_new_evidence(self):
        self.append();before=list(self.conn.iterdump());files=document_entries(self.folder)
        with self.assertRaises(self.subject.PaymentProofAppendError):self.append(data=b'%PDF-1.4\ndifferent')
        self.assertEqual(before,list(self.conn.iterdump()));self.assertEqual(files,document_entries(self.folder))

    def test_commit_failure_rolls_back_rows_and_only_attempt_file(self):
        old=self.folder/'old.pdf';old.write_bytes(b'old');before=list(self.conn.iterdump());self.commit_failure=True
        with self.assertRaisesRegex(RuntimeError,'commit failure'):self.append()
        self.assertEqual(before,list(self.conn.iterdump()));self.assertEqual(document_entries(self.folder),[old]);self.assertEqual(old.read_bytes(),b'old')

    def test_symlink_folder_denied_without_database_writes(self):
        link=self.root/'alias';link.symlink_to(self.folder,target_is_directory=True);before=list(self.conn.iterdump())
        with self.assertRaises(self.subject.PaymentProofAppendError):self.append(folder_provider=lambda:link)
        self.assertEqual(before,list(self.conn.iterdump()));self.assertEqual(document_entries(self.folder),[])

    def test_active_and_carried_forward_target_is_valid(self):
        self.conn.execute("UPDATE application_cycles SET status='active',outcome=NULL")
        self.conn.execute("UPDATE student_cases SET status='in_progress'")
        self.conn.execute("UPDATE case_record_links SET validity_status='carried_forward'");self.conn.commit()
        self.assertFalse(self.append().replayed)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM transfer_payment_confirmations').fetchone()[0],0)

    def test_published_case_status_check_allows_in_progress_only(self):
        # Extract the exact first status CHECK from published 001. This is
        # a focused status contract, not a full migration acceptance claim.
        sql=(Path(__file__).resolve().parents[1]/'migrations/20260728_001_student_case_phase1.sql').read_text()
        start=sql.index('CHECK (',sql.index("status TEXT NOT NULL DEFAULT 'draft'"))
        end=sql.index('\n    terminal_result',start)
        check=sql[start:end].strip().removesuffix(',')
        self.conn.executescript('DROP TABLE student_cases; CREATE TABLE student_cases(id INTEGER,student_id TEXT,case_type TEXT,status TEXT NOT NULL '+check+"); INSERT INTO student_cases VALUES(1,'S1','transfer','completed');")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE student_cases SET status='active'")
        self.conn.rollback()
        self.conn.execute("UPDATE application_cycles SET status='active',outcome=NULL")
        self.conn.execute("UPDATE student_cases SET status='in_progress'")
        self.conn.commit()
        before=self.frozen()
        self.append()
        self.assertEqual(before,self.frozen())
        self.assertEqual(self.conn.execute('SELECT count(*) FROM transfer_payment_confirmations').fetchone()[0],0)
        for status in ('draft','preparing','waiting','paused','cancelled','failed','archived'):
            with self.subTest(status=status):
                self.conn.execute('SAVEPOINT disallowed_status')
                self.conn.execute('UPDATE student_cases SET status=?',(status,))
                with self.assertRaises(self.subject.PaymentProofAppendError):
                    self.subject.resolve_payment_proof_target(self.conn,student_id='S1',payment_id=5,payment_link_id=4)
                self.conn.execute('ROLLBACK TO disallowed_status');self.conn.execute('RELEASE disallowed_status')

    def test_file_open_failure_does_not_create_business_rows(self):
        from unittest.mock import patch
        before=list(self.conn.iterdump())
        with patch.object(self.subject.os,'open',side_effect=OSError('synthetic storage failure')):
            with self.assertRaises(OSError):self.append()
        self.assertEqual(before,list(self.conn.iterdump()));self.assertEqual(document_entries(self.folder),[])

    def test_duplicate_ownership_and_confirmation_fail_closed(self):
        for table in ('case_record_links','transfer_payment_confirmations'):
            with self.subTest(table=table):
                self.conn.execute('SAVEPOINT duplicate')
                if table=='transfer_payment_confirmations':
                    # Corruption fixture only: formal 035 UNIQUE prevents this.
                    self.conn.executemany('INSERT INTO transfer_payment_confirmations VALUES(?,4)',[(6,),(7,)])
                else:
                    cols=[r[1] for r in self.conn.execute('PRAGMA table_info('+table+')') if r[1]!='id']
                    self.conn.execute('INSERT INTO '+table+' ('+','.join(cols)+') SELECT '+','.join(cols)+' FROM '+table)
                with self.assertRaises(self.subject.PaymentProofAppendError):self.subject.resolve_payment_proof_target(self.conn,student_id='S1',payment_id=5,payment_link_id=4)
                self.conn.execute('ROLLBACK TO duplicate');self.conn.execute('RELEASE duplicate')

    def test_read_failure_fails_closed(self):
        self.conn.set_authorizer(lambda *a:sqlite3.SQLITE_DENY)
        with self.assertRaises(self.subject.PaymentProofAppendError):self.subject.resolve_payment_proof_target(self.conn,student_id='S1',payment_id=5,payment_link_id=4)
        self.conn.set_authorizer(None)

    def test_only_exact_payment_proof_reminder_is_closed(self):
        from student_lifecycle.transfer_v2_reminders import payment_proof_occurrence_key
        self.assertEqual(payment_proof_occurrence_key(5,4),'payment-5-event-4')
        self.conn.executescript('''CREATE TABLE reminders(id INTEGER PRIMARY KEY,student_id TEXT,source_module TEXT,source_object_id TEXT,source_event_type TEXT,source_event_id TEXT,status TEXT,completed_at TEXT,completed_by INTEGER,snooze_until TEXT,updated_at TEXT,metadata_json TEXT,notes TEXT);
          INSERT INTO reminders VALUES(1,'S1','transfer_v2','2','transfer_v2.payment_proof','payment-5-event-4','Pending',NULL,NULL,NULL,NULL,'{}','');
          INSERT INTO reminders SELECT 2,student_id,source_module,source_object_id,'transfer_v2.missing_material',source_event_id,status,completed_at,completed_by,snooze_until,updated_at,metadata_json,notes FROM reminders WHERE id=1;
          INSERT INTO reminders SELECT 3,student_id,source_module,source_object_id,source_event_type,'payment-6-event-9',status,completed_at,completed_by,snooze_until,updated_at,metadata_json,notes FROM reminders WHERE id=1;
        ''')
        self.db.all=lambda sql,args=():self.conn.execute(sql,args).fetchall()
        frozen=self.frozen();other=[tuple(r) for r in self.conn.execute('SELECT * FROM reminders WHERE id>1')]
        result=self.append();self.assertEqual(self.subject.close_payment_proof_reminders(self.db,result,student_id='S1',occurred_at='2026-09-08'),1)
        self.assertEqual(self.conn.execute('SELECT status FROM reminders WHERE id=1').fetchone()[0],'Completed')
        self.assertEqual(other,[tuple(r) for r in self.conn.execute('SELECT * FROM reminders WHERE id>1')]);self.assertEqual(frozen,self.frozen())
        self.assertEqual(self.subject.close_payment_proof_reminders(self.db,result,student_id='S1',occurred_at='2026-09-08'),0)

    def test_two_real_connections_same_bytes_converge(self):
        self.concurrent_uploads(same=True)

    def test_two_real_connections_different_bytes_conflict(self):
        self.concurrent_uploads(same=False)

    def concurrent_uploads(self, *, same):
        import threading
        target=self.root/'concurrency.sqlite3';disk=sqlite3.connect(target);self.conn.backup(disk);disk.close()
        barrier=threading.Barrier(2);outcomes=[]
        def work(index):
            @contextmanager
            def transaction():
                conn=sqlite3.connect(target,timeout=10)
                try:
                    conn.execute('BEGIN IMMEDIATE');yield conn;conn.commit()
                except Exception:conn.rollback();raise
                finally:conn.close()
            db=type('DB',(),{'transaction':staticmethod(transaction)})()
            barrier.wait()
            try:
                result=self.subject.append_payment_proof(db,student_id='S1',payment_id=5,payment_link_id=4,filename='proof.pdf',data=b'%PDF-1.4\n'+(b'same' if same else str(index).encode()),actor_user_id=1,actor_username='synthetic',occurred_at='2026-09-08',folder_provider=lambda:self.folder)
                outcomes.append(result)
            except self.subject.PaymentProofAppendError as error:outcomes.append(error)
        threads=[threading.Thread(target=work,args=(i,)) for i in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(15);self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes),2)
        successful=[v for v in outcomes if isinstance(v,self.subject.PaymentProofAppendResult)]
        self.assertEqual(len(successful),2 if same else 1)
        if same:self.assertEqual(successful[0].document_id,successful[1].document_id)
        conn=sqlite3.connect(target)
        try:
            self.assertEqual(conn.execute('SELECT count(*) FROM payments').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT count(*) FROM student_documents').fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT count(*) FROM case_record_links WHERE record_type='student_document'").fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT count(*) FROM transfer_payment_confirmations').fetchone()[0],0)
        finally:conn.close()
        self.assertEqual(len(document_entries(self.folder)),1)

    def test_published_035_receipt_gate_allows_unconfirmed_additional_proof(self):
        # Scoped published table/trigger integration, not a full migration claim.
        # Replace the earlier unconstrained confirmation fixture before baseline.
        self.conn.executescript("DROP TABLE transfer_payment_confirmations; CREATE TABLE users(id INTEGER PRIMARY KEY); INSERT INTO users VALUES(1);")
        sql=(Path(__file__).resolve().parents[1]/'migrations/20260904_035_transfer_v2_final_schema.sql').read_text()
        start=sql.index('CREATE TABLE transfer_payment_confirmations (')
        end=sql.index('CREATE TRIGGER transfer_payment_confirmation_validate_035',start)
        self.conn.executescript(sql[start:end])
        start=end;end=sql.index('\nEND;',start)+len('\nEND;')
        self.conn.executescript(sql[start:end])
        self.conn.execute('PRAGMA foreign_keys=ON')
        confirm="INSERT INTO transfer_payment_confirmations(payment_link_id,confirmed_by,confirmed_at) VALUES(?,1,'2026-09-08')"
        with self.assertRaisesRegex(sqlite3.IntegrityError,'Transfer Payment confirmation authority mismatch'):
            self.conn.execute(confirm,(4,))
        self.conn.rollback()
        # Another branch already has its required paid receipt. The extra
        # branch's proof must not create or rewrite that authoritative fact.
        self.conn.executescript("""INSERT INTO transfer_school_applications VALUES(9,1,2,NULL);
          INSERT INTO payments VALUES(10,'S1',2400,'AUD','2026-09-07','Offer Deposit','final-receipt.pdf');
          INSERT INTO case_record_links VALUES(11,1,9,'payment',10,NULL,'current','current','2026-09-07',1,'2026-09-07');""")
        self.conn.execute(confirm,(11,));self.conn.commit()
        before=self.frozen()
        result=self.append()
        self.assertEqual(before,self.frozen())
        self.assertEqual(self.conn.execute('SELECT count(*) FROM transfer_payment_confirmations WHERE payment_link_id=4').fetchone()[0],0)
        self.assertEqual(self.conn.execute('SELECT receipt_file_path FROM payments WHERE id=10').fetchone()[0],'final-receipt.pdf')
        # A separate explicit confirmation is legal only after the proof exists.
        self.conn.execute(confirm,(4,));self.conn.commit()
        confirmations=[tuple(r) for r in self.conn.execute('SELECT * FROM transfer_payment_confirmations ORDER BY id')]
        self.assertTrue(self.append().replayed)
        self.assertEqual(confirmations,[tuple(r) for r in self.conn.execute('SELECT * FROM transfer_payment_confirmations ORDER BY id')])
        self.assertEqual(self.conn.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertTrue(Path(result.receipt_path).is_file())

    def test_actual_migration_link_trigger_accepts_completed_case_evidence(self):
        # Executes the published link insert trigger unchanged; not a full migration claim.
        self.conn.executescript('''CREATE TABLE offers(id INTEGER,student_id TEXT);
          CREATE TABLE coe_oshc(id INTEGER,student_id TEXT,document_kind TEXT);
          CREATE TABLE visas(id INTEGER,student_id TEXT);''')
        sql=(Path(__file__).resolve().parents[1]/'migrations/20260729_002_student_case_phase2_transfer.sql').read_text()
        start=sql.index('CREATE TRIGGER case_record_links_validate_insert')
        end=sql.index('\nEND;',start)+len('\nEND;')
        self.conn.executescript(sql[start:end])
        self.append()
        doc=self.conn.execute('SELECT id,document_type FROM student_documents').fetchone()
        self.assertEqual(doc['document_type'],'OfferDepositReceipt')
        self.assertEqual(self.conn.execute("SELECT document_id FROM case_record_links WHERE record_type='payment'").fetchone()[0],None)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM case_record_links WHERE record_type='student_document' AND document_id=?",(doc['id'],)).fetchone()[0],1)

class TransferPaymentProofAppendRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ast
        from http import HTTPStatus
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
        cls.node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='payment_proof_append')

    def test_create_only_and_denied_upload_never_reach_database_or_storage(self):
        import ast
        from http import HTTPStatus
        from types import SimpleNamespace
        for create in (False,True):
            with self.subTest(create=create):
                seen=[]
                def can_payment_action(user,action):
                    seen.append(action);return create if action=='create' else False
                ns={'can_payment_action':can_payment_action,'HTTPStatus':HTTPStatus}
                exec(compile(ast.Module(body=[self.node],type_ignores=[]),'<actual-app-handler>','exec'),ns)
                handler=SimpleNamespace(user={'id':2,'role_name':'Staff'},error_page=lambda text,status:(int(status),text))
                self.assertEqual(ns['payment_proof_append'](handler,'POST','S1','5')[0],403)
                self.assertEqual(seen,['upload'])

    def test_get_rejected_without_permission_database_or_storage_calls(self):
        import ast
        from http import HTTPStatus
        from types import SimpleNamespace
        ns={'HTTPStatus':HTTPStatus};exec(compile(ast.Module(body=[self.node],type_ignores=[]),'<actual-app-handler>','exec'),ns)
        handler=SimpleNamespace(error_page=lambda text,status:(int(status),text))
        self.assertEqual(ns['payment_proof_append'](handler,'GET','S1','5')[0],405)

    def test_upload_permission_reaches_exact_payment_without_creation(self):
        import ast
        from http import HTTPStatus
        from types import SimpleNamespace
        from student_lifecycle.transfer_payment_proof_append import PaymentProofAppendError
        calls=[]
        def append(database,**kwargs):calls.append(kwargs);return SimpleNamespace()
        ns={'HTTPStatus':HTTPStatus,'can_payment_action':lambda user,action:action=='upload',
            'db':SimpleNamespace(one=lambda *args:{'student_id':'S1'}),'now_iso':lambda:'2026-09-08',
            'append_payment_proof':append,'close_payment_proof_reminders':lambda *args,**kwargs:0,
            'PaymentProofAppendError':PaymentProofAppendError,'sqlite3':sqlite3}
        exec(compile(ast.Module(body=[self.node],type_ignores=[]),'<actual-app-handler>','exec'),ns)
        handler=SimpleNamespace(user={'id':2,'username':'synthetic','role_name':'Staff'},form={'csrf':'test','payment_link_id':'4'},files=[SimpleNamespace(filename='receipt.pdf',field_name='proof',data=b'%PDF')],error_page=lambda text,status:self.fail(text),flash_redirect=lambda *args:args)
        result=ns['payment_proof_append'](handler,'POST','S1','5')
        self.assertEqual(len(calls),1);self.assertEqual(calls[0]['payment_id'],'5');self.assertEqual(calls[0]['payment_link_id'],'4')
        self.assertIn('原付款记录保持不变',result[1])

    def test_nas_safety_failure_is_controlled_503(self):
        import ast
        from http import HTTPStatus
        from types import SimpleNamespace
        from nas import NasSafetyError
        from student_lifecycle.transfer_payment_proof_append import PaymentProofAppendError
        def append(*args,**kwargs):raise NasSafetyError('synthetic storage denied')
        ns={'HTTPStatus':HTTPStatus,'can_payment_action':lambda *args:True,
            'db':SimpleNamespace(one=lambda *args:{'student_id':'S1'}),'now_iso':lambda:'2026-09-08',
            'append_payment_proof':append,'PaymentProofAppendError':PaymentProofAppendError,
            'NasSafetyError':NasSafetyError,'sqlite3':sqlite3}
        exec(compile(ast.Module(body=[self.node],type_ignores=[]),'<actual-app-handler>','exec'),ns)
        handler=SimpleNamespace(user={'id':2,'username':'synthetic'},form={'payment_link_id':'4'},files=[SimpleNamespace(filename='receipt.pdf',field_name='proof',data=b'%PDF')],error_page=lambda text,status:(int(status),text))
        response=ns['payment_proof_append'](handler,'POST','S1','5')
        self.assertEqual(response[0],503);self.assertNotIn('synthetic storage denied',response[1])

    def test_cleanup_failure_cannot_reach_success_redirect(self):
        import ast
        from http import HTTPStatus
        from types import SimpleNamespace
        from nas import NasSafetyError
        from student_lifecycle.transfer_payment_proof_append import PaymentProofAppendError, PaymentProofCleanupError
        failure=PaymentProofCleanupError('remove_failed','original','retained')
        def append(*args,**kwargs):raise failure
        ns={'HTTPStatus':HTTPStatus,'can_payment_action':lambda *args:True,
            'db':SimpleNamespace(one=lambda *args:{'student_id':'S1'}),'now_iso':lambda:'2026-09-08',
            'append_payment_proof':append,'PaymentProofAppendError':PaymentProofAppendError,
            'NasSafetyError':NasSafetyError,'sqlite3':sqlite3}
        exec(compile(ast.Module(body=[self.node],type_ignores=[]),'<actual-app-handler>','exec'),ns)
        handler=SimpleNamespace(user={'id':2,'username':'synthetic'},form={'payment_link_id':'4'},
            files=[SimpleNamespace(filename='receipt.pdf',field_name='proof',data=b'%PDF')],
            error_page=lambda *args:self.fail('cleanup failure must remain explicit'),
            flash_redirect=lambda *args:self.fail('cleanup failure reported success'))
        with self.assertRaises(PaymentProofCleanupError) as raised:
            ns['payment_proof_append'](handler,'POST','S1','5')
        self.assertIs(raised.exception,failure)

class TransferPaymentDocumentContextTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.addCleanup(self.conn.close)
        self.conn.executescript('''
          CREATE TABLE student_documents(id INTEGER,student_id TEXT);
          INSERT INTO student_documents VALUES(9,'S1');
          CREATE TABLE payments(id INTEGER,student_id TEXT);
          INSERT INTO payments VALUES(5,'S1');
          CREATE TABLE case_record_links(id INTEGER,record_type TEXT,record_id INTEGER,document_id INTEGER,case_id INTEGER,transfer_application_id INTEGER,link_role TEXT);
          INSERT INTO case_record_links VALUES(4,'payment',5,9,1,3,'current');
          CREATE TABLE student_cases(id INTEGER,student_id TEXT,case_type TEXT);
          INSERT INTO student_cases VALUES(1,'S1','transfer');
          CREATE TABLE transfer_school_applications(id INTEGER,case_id INTEGER,application_cycle_id INTEGER,school_name_snapshot TEXT);
          INSERT INTO transfer_school_applications VALUES(3,1,2,'Synthetic School');
          CREATE TABLE application_cycles(id INTEGER,student_id TEXT,cycle_type TEXT,source_student_case_id INTEGER,cycle_uid TEXT,sequence_no INTEGER,status TEXT);
          INSERT INTO application_cycles VALUES(2,'S1','transfer',1,'cycle2',2,'closed');
        ''')

    def context(self):
        from student_lifecycle.presentation import load_record_contexts
        before=list(self.conn.iterdump());result=load_record_contexts(self.conn,'S1','document',[9])[9]
        self.assertEqual(before,list(self.conn.iterdump()));return result

    def test_existing_payment_document_has_known_completed_cycle_badge(self):
        from student_lifecycle.presentation import render_record_context
        context=self.context();self.assertEqual(context.record_context,'HISTORICAL');self.assertEqual(context.cycle_id,2)
        self.assertIn('业务周期 #2',render_record_context(context));self.assertNotIn('待确认',render_record_context(context))

    def test_conflicting_claim_remains_unconfirmed(self):
        self.conn.execute("INSERT INTO case_record_links VALUES(6,'payment',5,9,99,99,'current')")
        self.assertEqual(self.context().record_context,'LEGACY_UNCONFIRMED')

    def test_cross_student_payment_claim_remains_unconfirmed(self):
        self.conn.execute("UPDATE payments SET student_id='OTHER'")
        self.assertEqual(self.context().record_context,'LEGACY_UNCONFIRMED')

    def test_consistent_explicit_document_and_payment_claims_agree(self):
        self.conn.execute("INSERT INTO case_record_links VALUES(6,'student_document',9,9,1,3,'payment_proof')")
        self.assertEqual(self.context().record_context,'HISTORICAL')

    def test_legacy_schema_without_branch_cycle_identity_stays_unconfirmed(self):
        self.conn.execute('ALTER TABLE transfer_school_applications RENAME COLUMN application_cycle_id TO legacy_unused')
        self.assertEqual(self.context().record_context,'LEGACY_UNCONFIRMED')
