"""Atomic Transfer creation uses saved source facts rather than form labels."""
from dataclasses import asdict, replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from student_lifecycle import repository as lifecycle_repository
from student_lifecycle.transfer_v2_models import TransferV2CreateInput, TransferV2TargetInput
from student_lifecycle.transfer_v2_service import TransferV2Service, TransferV2ValidationError
from tests.transfer_v2_test_support import create_transfer_v2_database


class TransferSourceCreationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'source-creation.sqlite3'
        create_transfer_v2_database(self.path)
        self.service = TransferV2Service(self.path, clock=lambda: '2026-09-07T10:00:00+10:00')
        self.request = TransferV2CreateInput(student_id='S2',source_contract_id=102,
            source_label='aaie_existing',current_school_id=10,current_school_name='Current College',
            current_year_level='Year 7',application_year_level='Year 8',transfer_year=2027,
            transfer_term='Term 1',targets=(TransferV2TargetInput(20,'Target Grammar','VIC','Melbourne','Independent'),))

    def create(self, request=None, key='source-create'):
        return self.service.create_cycle(request or self.request, actor_user_id=1, idempotency_key=key)

    def query(self, sql, args=()):
        with sqlite3.connect(self.path) as db: return db.execute(sql, args).fetchall()

    def test_explicit_new_intake_overrides_form_label_without_initial(self):
        result = self.create()
        self.assertEqual(result.source_label, 'new_transfer')
        self.assertEqual(self.query('SELECT lifecycle_state,source_classification,application_cycle_id FROM transfer_source_authorities WHERE transfer_contract_id=102'), [('consumed','new_transfer',result.cycle_id)])
        self.assertEqual(self.query('SELECT transfer_source_label FROM transfer_cases WHERE case_id=?',(result.case_id,)), [('new_transfer',)])
        self.assertEqual(self.query('SELECT new_source_label FROM transfer_v2_origin_events WHERE application_cycle_id=?',(result.cycle_id,)), [('new_transfer',)])

    def test_ui_does_not_need_to_submit_any_source_label(self):
        result = self.create(replace(self.request, source_label=''))
        self.assertEqual(result.source_label, 'new_transfer')

    def test_existing_dual_evidence_overrides_form_label(self):
        result = self.create(replace(self.request,student_id='S1',source_contract_id=101,source_label='new_transfer'))
        self.assertEqual(result.source_label, 'aaie_existing')

    def test_missing_exact_contract_holds_without_new_case(self):
        before = self.query('SELECT count(*) FROM student_cases')
        with self.assertRaises(TransferV2ValidationError): self.create(replace(self.request,source_contract_id=None))
        self.assertEqual(before, self.query('SELECT count(*) FROM student_cases'))

    def test_source_schema_missing_holds_without_new_case(self):
        with sqlite3.connect(self.path) as db:
            db.execute('ALTER TABLE transfer_source_authorities RENAME TO unavailable_source_authorities')
        before = self.query('SELECT count(*) FROM student_cases')
        with self.assertRaises(TransferV2ValidationError): self.create()
        self.assertEqual(before, self.query('SELECT count(*) FROM student_cases'))

    def test_pending_invalidated_intake_holds_without_business_writes(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE parent_form_tokens SET invalidated_at='2026-09-07' WHERE token='S2-transfer-intake'")
        before = self.query('SELECT count(*) FROM student_cases')
        with self.assertRaises(TransferV2ValidationError): self.create()
        self.assertEqual(before, self.query('SELECT count(*) FROM student_cases'))
        self.assertEqual(self.query('SELECT application_cycle_id FROM contracts WHERE id=102'), [(None,)])
        self.assertEqual(self.query('SELECT count(*) FROM student_lifecycle_idempotency_receipts'), [(0,)])

    def test_closed_initial_keeps_intake_ownership(self):
        with sqlite3.connect(self.path) as db:
            db.execute("""INSERT INTO application_cycles(cycle_uid,student_id,parent_form_token,cycle_type,sequence_no,status,opened_at,created_at,updated_at,education_stream)
                VALUES('closed-S2','S2','S2-transfer-intake','initial',1,'closed','2026-08-01','2026-08-01','2026-08-01','school_education')""")
        result = self.create()
        self.assertEqual(result.source_label, 'new_transfer')
        self.assertEqual(self.query("SELECT parent_form_token,status FROM application_cycles WHERE cycle_uid='closed-S2'"), [('S2-transfer-intake','closed')])
        self.assertEqual(self.query('PRAGMA foreign_key_check'), [])

    def test_conflict_after_pending_save_holds_atomically(self):
        with sqlite3.connect(self.path) as db:
            db.execute("""INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at,education_stream)
                VALUES(450,'ordinary-S2','S2','initial',1,'closed','2026-08-01','2026-08-01','2026-08-01','school_education')""")
            db.execute("INSERT INTO contracts(id,student_id,contract_type,contract_status,updated_at,application_cycle_id) VALUES(450,'S2','LocalStudentApplicationContract','FullyExecuted','2026-08-01',450)")
            db.execute("""INSERT INTO student_school_enrolments(student_id,school_id,school_name_snapshot,enrolment_role,status,created_by,created_at,updated_by,updated_at,confirmed_at)
                VALUES('S2',10,'Current College','current','active',1,'2026-08-01',1,'2026-08-01','2026-08-01')""")
        with self.assertRaises(TransferV2ValidationError): self.create()
        self.assertEqual(self.query("SELECT count(*) FROM application_cycles WHERE student_id='S2' AND cycle_type='transfer'"), [(0,)])
        self.assertEqual(self.query('SELECT lifecycle_state FROM transfer_source_authorities WHERE transfer_contract_id=102'), [('verified',)])

    def test_replay_preserves_consumed_source_and_no_duplicate_origin(self):
        first = self.create()
        replay = self.create()
        self.assertEqual(first.cycle_id, replay.cycle_id)
        self.assertEqual(replay.source_label, 'new_transfer')
        self.assertEqual(self.query('SELECT count(*) FROM transfer_v2_origin_events WHERE application_cycle_id=?',(first.cycle_id,)), [(1,)])
        changed_label = self.create(replace(self.request,source_label='new_transfer'))
        self.assertEqual(first.cycle_id, changed_label.cycle_id)
        self.assertEqual(changed_label.source_label, 'new_transfer')
        with self.assertRaises(TransferV2ValidationError): self.create(replace(self.request,transfer_year=2028))

    def test_exact_preexisting_full_dto_receipt_remains_replayable(self):
        result = self.create()
        legacy_hash = lifecycle_repository.payload_hash(asdict(self.request))
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE student_lifecycle_idempotency_receipts SET payload_sha256=?', (legacy_hash,))
        before = self.query('SELECT * FROM transfer_source_authorities')
        replay = self.create()
        self.assertEqual(result.cycle_id, replay.cycle_id)
        self.assertEqual(before, self.query('SELECT * FROM transfer_source_authorities'))
        with self.assertRaises(TransferV2ValidationError):
            self.create(replace(self.request, current_school_name='Different school'))

    def test_creation_display_requires_exact_saved_context(self):
        self.assertIsNone(self.service.creation_source_label('S2'))
        self.assertEqual(self.service.creation_source_label('S2', source_contract_id=102), 'new_transfer')
        result = self.create()
        self.assertEqual(self.service.creation_source_label('S2', source_contract_id=102), 'new_transfer')
        with self.assertRaises(TransferV2ValidationError):
            self.service.change_origin(result.cycle_id,new_source_label='aaie_existing',actor_user_id=1,note='tamper',idempotency_key='tamper')

if __name__ == '__main__': unittest.main()
