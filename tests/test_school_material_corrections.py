import sqlite3,unittest
import school_material_corrections as materials

class MaterialCorrectionsTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.addCleanup(self.c.close)
        self.c.executescript('''CREATE TABLE users(id INTEGER PRIMARY KEY,is_active INTEGER);INSERT INTO users VALUES(1,1);
          CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,status TEXT);INSERT INTO application_cycles VALUES(7,'S1','active'),(8,'S2','active'),(9,'S1','closed');
          CREATE TABLE offers(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER,offer_status TEXT,offer_file_path TEXT,signed_offer_file_path TEXT);
          INSERT INTO offers VALUES(1,'S1',7,'received','/original/offer.pdf',NULL);
          CREATE TABLE coe_oshc(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER,document_kind TEXT,file_path TEXT);
          INSERT INTO coe_oshc VALUES(2,'S1',7,'COE','/original/coe.pdf');
          CREATE TABLE student_documents(id INTEGER PRIMARY KEY,student_id TEXT,file_path TEXT,is_active INTEGER,document_status TEXT,voided_at TEXT,voided_by INTEGER,void_reason TEXT);
          INSERT INTO student_documents VALUES(1,'S1','/original/offer.pdf',1,'active',NULL,NULL,NULL),(2,'S1','/original/coe.pdf',1,'active',NULL,NULL,NULL);
          CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id INTEGER,action_type TEXT,module_name TEXT,module_key TEXT,record_id TEXT,student_id TEXT,old_value TEXT,new_value TEXT,created_at TEXT);''')
        self.c.execute('BEGIN')
    def change(self,kind='offer',action='void',record=1,**extra):
        state=materials.material_state(self.c,'S1',kind,record)
        return materials.correct_material(self.c,student_id='S1',kind=kind,record_id=record,action=action,
            revision=state['revision'],reason='Wrong source corrected',actor_id=1,stamp='2026-11-23T22:00:00+11:00',**extra)
    def test_void_and_restore_keep_original_paths_and_prior_status(self):
        self.change();self.assertEqual('invalid_record',self.c.execute('SELECT offer_status FROM offers').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT is_active FROM student_documents WHERE id=1').fetchone()[0])
        self.change(action='restore')
        self.assertEqual(('received','/original/offer.pdf'),tuple(self.c.execute('SELECT offer_status,offer_file_path FROM offers').fetchone()))
        self.assertEqual(1,self.c.execute('SELECT is_active FROM student_documents WHERE id=1').fetchone()[0])
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
    def test_coe_reassignment_rejects_foreign_cycle_and_preserves_original(self):
        with self.assertRaises(ValueError):self.change('coe','reassign',2,target_cycle=8)
        self.change('coe','reassign',2,target_cycle=9)
        self.assertEqual((9,'/original/coe.pdf'),tuple(self.c.execute('SELECT application_cycle_id,file_path FROM coe_oshc').fetchone()))
    def test_formally_adopted_coe_cannot_be_hidden_or_reassigned(self):
        self.c.executescript('CREATE TABLE final_school_coe_confirmations(coe_id INTEGER);INSERT INTO final_school_coe_confirmations VALUES(2);')
        self.c.execute('BEGIN')
        with self.assertRaisesRegex(ValueError,'采用'):self.change('coe','void',2)
        self.assertEqual('COE',self.c.execute('SELECT document_kind FROM coe_oshc').fetchone()[0])
    def test_stale_revision_and_foreign_student_are_rejected(self):
        with self.assertRaises(ValueError):materials.material_state(self.c,'S2','offer',1)
        state=materials.material_state(self.c,'S1','offer',1)
        self.c.execute("UPDATE offers SET offer_status='accepted' WHERE id=1")
        with self.assertRaises(ValueError):
            materials.correct_material(self.c,student_id='S1',kind='offer',record_id=1,action='void',revision=state['revision'],reason='Wrong',actor_id=1,stamp='2026-11-23T22:00:00+11:00')
    def events(self):
        self.c.execute('CREATE TABLE offer_received_events(id INTEGER PRIMARY KEY,legacy_offer_id INTEGER)')
        self.c.execute('INSERT INTO offer_received_events VALUES(11,1)')
        self.c.execute('CREATE TABLE offer_received_event_state_events(id INTEGER PRIMARY KEY,offer_received_event_id INTEGER,state TEXT,reason TEXT,occurred_at TEXT,actor_user_id INTEGER,actor_name TEXT,idempotency_key TEXT,created_at TEXT)')
        self.c.execute("INSERT INTO offer_received_event_state_events VALUES(1,11,'WITHDRAWN','original','2026-11-22T22:00:00+11:00',1,'reviewer','original','2026-11-22T22:00:00+11:00')")
    def test_restore_preserves_formal_withdrawn_state_instead_of_reactivating_offer(self):
        self.events();self.change();self.change(action='restore')
        self.assertEqual('WITHDRAWN',self.c.execute('SELECT state FROM offer_received_event_state_events ORDER BY id DESC LIMIT 1').fetchone()[0])
    def test_new_formal_event_after_void_blocks_restoration(self):
        self.events();self.change()
        self.c.execute("INSERT INTO offer_received_event_state_events VALUES(99,11,'SUPERSEDED','later','2026-11-24T00:00:00+11:00',1,'reviewer','later','2026-11-24T00:00:00+11:00')")
        with self.assertRaises(ValueError):self.change(action='restore')
    def test_transfer_and_financial_offer_adoption_are_blocked(self):
        self.c.execute('CREATE TABLE transfer_coe_confirmations(coe_oshc_id INTEGER)');self.c.execute('INSERT INTO transfer_coe_confirmations VALUES(2)')
        with self.assertRaisesRegex(ValueError,'采用'):self.change('coe','void',2)
        self.c.execute('CREATE TABLE revenue_school_course_evidence(offer_id INTEGER)');self.c.execute('INSERT INTO revenue_school_course_evidence VALUES(1)')
        with self.assertRaisesRegex(ValueError,'采用'):self.change()
    def test_caaw_linked_as_student_document_cannot_be_hidden(self):
        self.c.execute("CREATE TABLE case_record_links(record_type TEXT,record_id INTEGER,document_id INTEGER)")
        self.c.execute("INSERT INTO case_record_links VALUES('student_document',2,2)")
        with self.assertRaisesRegex(ValueError,'采用'):self.change('coe','void',2)
