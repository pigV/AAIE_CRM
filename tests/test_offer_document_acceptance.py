"""Document acceptance is independent of the student's final school choice."""
from unittest.mock import patch
from tests.test_offer_acceptance import OfferAcceptanceFixture


class DocumentAcceptanceTests(OfferAcceptanceFixture):
    def accept(self, offer_id):
        import offer_acceptance
        action = offer_acceptance.accept_offer_document
        return action(self.connection, 'S001', offer_id, self.actor)

    def test_accepting_second_document_preserves_first_and_final_choice(self):
        from offer_acceptance import assign_offer_school, set_accepted_offer
        assign_offer_school(self.connection, 101, 11, self.actor)
        set_accepted_offer(self.connection, 'S001', 101, self.actor)
        self.accept(102)
        self.assertEqual({101:'accepted',102:'accepted'}, dict(self.connection.execute(
            'SELECT id,offer_status FROM offers WHERE id IN (101,102)')))
        self.assertEqual(101, self.connection.execute('SELECT offer_id FROM student_offer_acceptances').fetchone()[0])

    def test_unclassified_valid_document_can_be_accepted_without_final_selection(self):
        self.accept(103)
        self.assertEqual('accepted', self.connection.execute('SELECT offer_status FROM offers WHERE id=103').fetchone()[0])
        self.assertEqual(0, self.connection.execute('SELECT count(*) FROM student_offer_acceptances').fetchone()[0])

    def test_document_acceptance_never_creates_canonical_or_commission_events(self):
        from offer_acceptance import assign_offer_school
        assign_offer_school(self.connection, 101, 11, self.actor)
        # This guard raises if document acceptance enters the final-school gateway.
        with patch('offer_acceptance.ensure_canonical_offer_event', side_effect=AssertionError('final gateway used')):
            self.accept(101)
            self.accept(102)
        self.assertEqual(0, self.connection.execute('SELECT count(*) FROM student_offer_acceptances').fetchone()[0])

    def test_retry_is_idempotent_and_audited_once(self):
        first = self.accept(102)
        repeated = self.accept(102)
        self.assertFalse(first.unchanged)
        self.assertTrue(repeated.unchanged)
        self.assertEqual(1, self.connection.execute('SELECT count(*) FROM offer_acceptance_history').fetchone()[0])

    def test_explicit_final_school_switch_preserves_document_acceptances(self):
        from offer_acceptance import assign_offer_school, set_accepted_offer
        assign_offer_school(self.connection,101,11,self.actor)
        self.accept(101)
        self.accept(102)
        set_accepted_offer(self.connection,'S001',101,self.actor)
        set_accepted_offer(self.connection,'S001',102,self.actor)
        self.assertEqual({101:'accepted',102:'accepted'},dict(self.connection.execute(
            'SELECT id,offer_status FROM offers WHERE id IN (101,102)')))

    def test_invalid_document_and_wrong_student_are_blocked(self):
        from offer_acceptance import OfferAcceptanceError
        self.connection.execute("UPDATE offers SET offer_status='withdrawn' WHERE id=102")
        with self.assertRaises(OfferAcceptanceError): self.accept(102)
        with self.assertRaises(OfferAcceptanceError): self.accept(201)

    def test_failed_history_rolls_back_document_status(self):
        import sqlite3
        self.connection.executescript("CREATE TRIGGER fail_doc_history BEFORE INSERT ON offer_acceptance_history BEGIN SELECT RAISE(ABORT,'history failed'); END;")
        with self.assertRaises(sqlite3.IntegrityError): self.accept(102)
        self.assertEqual('signed',self.connection.execute('SELECT offer_status FROM offers WHERE id=102').fetchone()[0])

    def test_withdrawing_final_school_preserves_independent_acceptance_dates(self):
        from offer_acceptance import set_accepted_offer, withdraw_accepted_offer
        self.accept(102)
        before = tuple(self.connection.execute('SELECT offer_status,accepted_date,accepted_by FROM offers WHERE id=102').fetchone())
        set_accepted_offer(self.connection,'S001',102,self.actor,occurred_at='2027-01-02T10:00:00+10:00')
        withdraw_accepted_offer(self.connection,'S001',self.actor,reason='Choosing final school later')
        self.assertEqual(before, tuple(self.connection.execute('SELECT offer_status,accepted_date,accepted_by FROM offers WHERE id=102').fetchone()))

    def test_document_acceptance_publishes_once_and_rolls_back_on_sync_failure(self):
        from business_changes.schema import install
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("BEGIN")
        install(self.connection)
        self.connection.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
        self.connection.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
        self.accept(102)
        self.accept(102)
        self.assertEqual(['offer.document_accepted'], [r[0] for r in self.connection.execute('SELECT event_type FROM business_change_events')])
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('sync failed')):
            with self.assertRaises(RuntimeError): self.accept(103)
        self.assertEqual('sent_to_parent', self.connection.execute('SELECT offer_status FROM offers WHERE id=103').fetchone()[0])

    def test_signed_copy_is_accepted_without_creating_a_second_commission_source(self):
        self.connection.execute("ALTER TABLE offers ADD COLUMN document_type TEXT DEFAULT 'Offer'")
        self.connection.execute("UPDATE offers SET document_type='SignedOffer',school_name='Melbourne Public School',school_id=11 WHERE id=102")
        self.accept(101)
        self.accept(102)
        self.assertEqual(0, self.connection.execute('SELECT count(*) FROM student_offer_acceptances').fetchone()[0])
        self.assertEqual('accepted',self.connection.execute('SELECT offer_status FROM offers WHERE id=102').fetchone()[0])

    def test_language_and_signed_documents_do_not_replace_canonical_final_school(self):
        self.connection.executescript('''
            CREATE TABLE offer_received_events(id INTEGER PRIMARY KEY,higher_education_offer_id INTEGER);
            CREATE TABLE cycle_final_acceptances(id INTEGER PRIMARY KEY,application_cycle_id INTEGER,offer_received_event_id INTEGER,acceptance_status TEXT);
            INSERT INTO cycle_final_acceptances VALUES(1,7,555,'active');
        ''')
        self.accept(102)
        self.accept(103)
        self.assertEqual([(1,7,555,'active')], [tuple(r) for r in self.connection.execute('SELECT * FROM cycle_final_acceptances')])
        self.assertEqual(0, self.connection.execute('SELECT count(*) FROM offer_received_events').fetchone()[0])

    def test_legacy_final_without_marker_is_preserved_when_final_school_changes(self):
        from offer_acceptance import set_accepted_offer, assign_offer_school
        assign_offer_school(self.connection,101,11,self.actor)
        self.connection.execute("UPDATE offers SET offer_status='accepted',accepted_date='2026-01-01' WHERE id=101")
        self.connection.execute("INSERT INTO student_offer_acceptances VALUES('S001',101,'received','2026-01-01',9,'Old staff','2026-01-01')")
        self.accept(102)
        set_accepted_offer(self.connection,'S001',102,self.actor)
        self.assertEqual(('accepted','2026-01-01'),tuple(self.connection.execute('SELECT offer_status,accepted_date FROM offers WHERE id=101').fetchone()))


class DocumentWithdrawalTests(OfferAcceptanceFixture):
    def setUp(self):
        super().setUp()
        self.connection.execute('CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id INTEGER,action_type TEXT,module_name TEXT,module_key TEXT,record_id TEXT,student_id TEXT,old_value TEXT,new_value TEXT,created_at TEXT)')

    def withdraw(self,offer_id=102,reason='Family withdrew this acceptance'):
        from offer_acceptance import withdraw_offer_document
        return withdraw_offer_document(self.connection,'S001',offer_id,self.actor,reason=reason)

    def test_withdraw_only_one_document_and_retry_has_one_audit(self):
        from offer_acceptance import accept_offer_document
        accept_offer_document(self.connection,'S001',102,self.actor)
        accept_offer_document(self.connection,'S001',103,self.actor)
        self.assertEqual('signed',self.withdraw().restored_status)
        self.withdraw()
        self.assertEqual({102:'signed',103:'accepted'},dict(self.connection.execute('SELECT id,offer_status FROM offers WHERE id IN(102,103)')))
        self.assertEqual(1,self.connection.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
        self.assertIn('Family withdrew',self.connection.execute('SELECT new_value FROM audit_logs').fetchone()[0])

    def test_final_selection_must_be_withdrawn_before_document(self):
        from offer_acceptance import set_accepted_offer,withdraw_accepted_offer,OfferAcceptanceError
        set_accepted_offer(self.connection,'S001',102,self.actor)
        with self.assertRaisesRegex(OfferAcceptanceError,'最终'):self.withdraw()
        withdraw_accepted_offer(self.connection,'S001',self.actor,reason='Final choice withdrawn')
        self.assertEqual('accepted',self.connection.execute('SELECT offer_status FROM offers WHERE id=102').fetchone()[0])
        self.assertEqual('signed',self.withdraw().restored_status)

    def test_reason_ownership_permissions_and_unknown_original_are_protected(self):
        from offer_acceptance import accept_offer_document,withdraw_offer_document,OfferAcceptanceError
        with self.assertRaises(OfferAcceptanceError):self.withdraw(reason='')
        with self.assertRaises(OfferAcceptanceError):self.withdraw(201)
        with self.assertRaises(OfferAcceptanceError):withdraw_offer_document(self.connection,'S001',102,{},reason='test')
        self.connection.execute("UPDATE offers SET offer_status='accepted' WHERE id=102")
        accept_offer_document(self.connection,'S001',102,self.actor)
        with self.assertRaisesRegex(OfferAcceptanceError,'历史'):self.withdraw()

    def test_withdraw_then_reaccept_uses_new_original_status(self):
        from offer_acceptance import accept_offer_document
        accept_offer_document(self.connection,'S001',102,self.actor)
        self.withdraw()
        self.connection.execute("UPDATE offers SET offer_status='expired' WHERE id=102")
        accept_offer_document(self.connection,'S001',102,self.actor)
        self.assertEqual('expired',self.withdraw().restored_status)


class OfferDedicatedWorkflowBoundaryTests(OfferAcceptanceFixture):
    def assert_rejected_unchanged(self):
        from offer_acceptance import accept_offer_document,set_accepted_offer,OfferAcceptanceError
        before=[tuple(r) for r in self.connection.execute('SELECT * FROM offers ORDER BY id')]
        for action in (accept_offer_document,set_accepted_offer):
            with self.subTest(action=action.__name__),self.assertRaises(OfferAcceptanceError):
                action(self.connection,'S001',102,self.actor)
        self.assertEqual(before,[tuple(r) for r in self.connection.execute('SELECT * FROM offers ORDER BY id')])
        self.assertEqual(0,self.connection.execute('SELECT count(*) FROM offer_acceptance_history').fetchone()[0])

    def test_transfer_sql_binding_rejects_direct_document_and_final_actions(self):
        self.connection.executescript('''
            CREATE TABLE offer_received_events(id INTEGER PRIMARY KEY,legacy_offer_id INTEGER,transfer_branch_id INTEGER);
            CREATE TABLE case_record_links(id INTEGER PRIMARY KEY,record_type TEXT,record_id INTEGER,transfer_application_id INTEGER);
            INSERT INTO offer_received_events VALUES(1,102,88);
            INSERT INTO case_record_links VALUES(1,'offer',102,88);
        ''')
        self.assert_rejected_unchanged()

    def test_transfer_link_without_receipt_still_cannot_bypass_gate(self):
        self.connection.executescript("CREATE TABLE case_record_links(record_type TEXT,record_id INTEGER,transfer_application_id INTEGER); INSERT INTO case_record_links VALUES('offer',102,88);")
        self.assert_rejected_unchanged()

    def test_transfer_cycle_closed_cycle_and_wrong_student_cycle_are_rejected(self):
        self.connection.execute('ALTER TABLE offers ADD COLUMN application_cycle_id INTEGER')
        self.connection.execute('CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,status TEXT,cycle_type TEXT)')
        self.connection.execute('UPDATE offers SET application_cycle_id=7 WHERE id=102')
        for student,status,kind in [('S001','active','transfer'),('S001','closed','initial'),('S002','active','initial')]:
            self.connection.execute('DELETE FROM application_cycles')
            self.connection.execute('INSERT INTO application_cycles VALUES(7,?,?,?)',(student,status,kind))
            with self.subTest(student=student,status=status,kind=kind):self.assert_rejected_unchanged()


class DocumentWithdrawalSyncTests(DocumentWithdrawalTests):
    def test_canonical_final_protects_document_even_if_legacy_pointer_missing(self):
        from offer_acceptance import accept_offer_document,OfferAcceptanceError
        accept_offer_document(self.connection,'S001',102,self.actor)
        self.connection.executescript('''
            CREATE TABLE offer_received_events(id INTEGER PRIMARY KEY,legacy_offer_id INTEGER);
            INSERT INTO offer_received_events VALUES(7,102);
            CREATE TABLE cycle_final_acceptances(offer_received_event_id INTEGER,acceptance_status TEXT);
            INSERT INTO cycle_final_acceptances VALUES(7,'active');
        ''')
        with self.assertRaisesRegex(OfferAcceptanceError,'最终'):self.withdraw()

    def test_withdraw_sync_failure_rolls_back_status_history_and_audit(self):
        from offer_acceptance import accept_offer_document
        from business_changes.schema import install
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.execute('BEGIN');install(self.connection)
        self.connection.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
        self.connection.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
        accept_offer_document(self.connection,'S001',102,self.actor)
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('sync failed')):
            with self.assertRaises(RuntimeError):self.withdraw()
        self.assertEqual('accepted',self.connection.execute('SELECT offer_status FROM offers WHERE id=102').fetchone()[0])
        self.assertEqual(0,self.connection.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
        self.assertEqual(1,self.connection.execute('SELECT count(*) FROM offer_acceptance_history').fetchone()[0])
        self.withdraw();self.withdraw()
        self.assertEqual(['offer.document_accepted','offer.document_withdrawn'],[r[0] for r in self.connection.execute('SELECT event_type FROM business_change_events ORDER BY seq')])


class InvalidPreviousFinalTests(OfferAcceptanceFixture):
    def old_final(self,status,receipt_void=False):
        from offer_acceptance import assign_offer_school,set_accepted_offer
        assign_offer_school(self.connection,101,11,self.actor)
        set_accepted_offer(self.connection,'S001',101,self.actor)
        self.connection.execute('UPDATE offers SET offer_status=? WHERE id=101',(status,))
        if receipt_void:
            self.connection.executescript('''
                CREATE TABLE offer_received_events(id INTEGER PRIMARY KEY,legacy_offer_id INTEGER);
                CREATE TABLE offer_received_event_state_events(id INTEGER PRIMARY KEY,offer_received_event_id INTEGER,state TEXT,occurred_at TEXT);
                INSERT INTO offer_received_events VALUES(1,101);
                INSERT INTO offer_received_event_state_events VALUES(1,1,'VOID','2026-11-20T12:00:00+11:00');
            ''')

    def check_invalid_previous(self,status,receipt_void=False,switch=False):
        from offer_acceptance import set_accepted_offer,withdraw_accepted_offer
        self.old_final(status,receipt_void)
        self.connection.execute("DELETE FROM offer_acceptance_history WHERE previous_offer_restored_status='__document_acceptance__'")
        if switch:
            set_accepted_offer(self.connection,'S001',102,self.actor)
            self.assertEqual(102,self.connection.execute('SELECT offer_id FROM student_offer_acceptances').fetchone()[0])
        else:
            withdraw_accepted_offer(self.connection,'S001',self.actor,reason='Invalid prior offer')
            self.assertEqual(0,self.connection.execute('SELECT count(*) FROM student_offer_acceptances').fetchone()[0])
        self.assertEqual(status,self.connection.execute('SELECT offer_status FROM offers WHERE id=101').fetchone()[0])

    def test_voided_previous_final_can_withdraw(self):self.check_invalid_previous('voided')
    def test_rejected_previous_final_can_withdraw(self):self.check_invalid_previous('rejected')
    def test_void_receipt_previous_final_can_withdraw(self):self.check_invalid_previous('accepted',True)
    def test_voided_previous_final_can_switch(self):self.check_invalid_previous('voided',switch=True)
    def test_rejected_previous_final_can_switch(self):self.check_invalid_previous('rejected',switch=True)
    def test_void_receipt_previous_final_can_switch(self):self.check_invalid_previous('accepted',True,True)

    def test_invalid_transfer_final_still_requires_dedicated_service(self):
        from offer_acceptance import set_accepted_offer,withdraw_accepted_offer,OfferAcceptanceError
        self.old_final('voided')
        self.connection.executescript('''
            CREATE TABLE offer_received_events(id INTEGER PRIMARY KEY,legacy_offer_id INTEGER,transfer_branch_id INTEGER);
            INSERT INTO offer_received_events VALUES(1,101,88);
        ''')
        with self.assertRaisesRegex(OfferAcceptanceError,'Transfer'):
            withdraw_accepted_offer(self.connection,'S001',self.actor,reason='Invalid prior')
        with self.assertRaisesRegex(OfferAcceptanceError,'Transfer'):
            set_accepted_offer(self.connection,'S001',102,self.actor)
        self.assertEqual(101,self.connection.execute('SELECT offer_id FROM student_offer_acceptances').fetchone()[0])
        self.assertEqual('voided',self.connection.execute('SELECT offer_status FROM offers WHERE id=101').fetchone()[0])
