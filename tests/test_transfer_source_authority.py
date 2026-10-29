"""Explicit source FK evidence decisions on migration036 with foreign keys enabled."""
import sqlite3
import unittest
from pathlib import Path
from tests.test_transfer_source_authority_migration import fixture, MIGRATION
MODULE = Path(__file__).resolve().parents[1] / 'student_lifecycle/transfer_source_authority.py'

class SourceAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.exists(), 'Explicit evidence source authority service is required')
        from student_lifecycle import transfer_source_authority
        self.svc = transfer_source_authority
        self.db = fixture()
        self.addCleanup(self.db.close)
        self.db.executescript("""
          CREATE TABLE roles(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,permissions_json TEXT NOT NULL);
          INSERT INTO roles VALUES(1,'Source operator','["student_cases.view","student_cases.create"]'),(2,'Read only','["student_cases.view"]');
          ALTER TABLE users ADD COLUMN role_id INTEGER REFERENCES roles(id);
          ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;
          ALTER TABLE users ADD COLUMN deleted_at TEXT;
          UPDATE users SET role_id=1;
          INSERT INTO users(id,role_id,is_active) VALUES(2,2,1);
          CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_by INTEGER,updated_at TEXT NOT NULL);
          INSERT INTO system_settings VALUES('STUDENT_CASES_READ_ENABLED','true',1,'2026-09-07'),('TRANSFER_CASES_WRITE_ENABLED','true',1,'2026-09-07');
        """)
        self.db.executescript(MIGRATION.read_text())

    def save(self, student='new', contract=100, **evidence):
        if not evidence: evidence = dict(parent_form_token='new-intake')
        return self.svc.save_authority(self.db, student, contract, actor_user_id=1, occurred_at='2026-09-07', **evidence)

    def resolve(self, student='new', contract=100):
        return self.svc.resolve_for_creation(self.db, student, contract)

    def existing(self):
        return self.save('existing', 200, current_enrolment_id=21, ordinary_relationship_cycle_id=20, ordinary_relationship_contract_id=201)

    def bind(self, student='new', contract=100, source='new_transfer'):
        self.db.execute('INSERT INTO student_cases(id,student_id) VALUES(99,?)', (student,))
        self.db.execute("INSERT INTO application_cycles VALUES(99,?,'transfer','school_education','active',NULL,99)", (student,))
        self.db.execute("UPDATE contracts SET contract_status='FullyExecuted',application_cycle_id=99 WHERE id=?", (contract,))
        self.db.execute('INSERT INTO transfer_cases VALUES(99,?,?)', (contract, source))
        self.db.execute('INSERT INTO transfer_v2_origin_events(id,application_cycle_id,new_source_label) VALUES(99,99,?)', (source,))

    def test_explicit_new_intake_without_active_initial(self):
        row = self.save()
        self.assertEqual(row['lifecycle_state'], 'verified')
        self.assertEqual(row['source_classification'], 'new_transfer')
        self.assertEqual(row['verified_by'], 1)
        self.assertEqual(row['verified_at'], '2026-09-07')
        self.assertEqual(self.resolve()['source_classification'], 'new_transfer')
        self.assertEqual(self.db.execute('SELECT parent_form_token FROM application_cycles WHERE id=10').fetchone()[0], 'new-intake')
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())

    def test_new_intake_without_any_initial(self):
        self.db.execute('DELETE FROM application_cycles WHERE id=10')
        self.save()
        self.assertEqual(self.resolve()['source_classification'], 'new_transfer')

    def test_existing_requires_exact_dual_evidence(self):
        self.existing()
        self.assertEqual(self.resolve('existing', 200)['source_classification'], 'aaie_existing')

    def test_absence_does_not_become_existing(self):
        with self.assertRaises(self.svc.SourceAuthorityError): self.save('other', 300, parent_form_token=None)
        with self.assertRaises(self.svc.SourceAuthorityError): self.resolve('other', 300)
        self.assertEqual(self.db.execute('SELECT count(*) FROM transfer_source_authorities').fetchone()[0], 0)

    def test_each_existing_evidence_is_required(self):
        for evidence in (dict(current_enrolment_id=21), dict(ordinary_relationship_cycle_id=20, ordinary_relationship_contract_id=201)):
            with self.subTest(evidence=evidence), self.assertRaises(self.svc.SourceAuthorityError): self.save('existing', 200, **evidence)

    def test_new_intake_conflicts_with_sufficient_existing_evidence(self):
        self.db.execute("INSERT INTO student_school_enrolments VALUES(11,'new','current','active','2026-09-01')")
        self.db.execute("INSERT INTO contracts VALUES(101,'new','LocalStudentApplicationContract','FullyExecuted',NULL,NULL,10)")
        with self.assertRaises(self.svc.SourceAuthorityError): self.save()

    def test_existing_selection_holds_when_submitted_new_transfer_intake_also_exists(self):
        self.db.execute("""INSERT INTO parent_form_tokens(
            token,student_id,intake_type,submitted_at
        ) VALUES('conflicting-new-intake','existing','new_student_transfer','2026-09-07')""")
        with self.assertRaisesRegex(
                self.svc.SourceAuthorityError,
                'Parent intake conflicts with Existing dual evidence'):
            self.existing()

    def test_explicit_mixed_payload_holds(self):
        with self.assertRaises(self.svc.SourceAuthorityError):
            self.save(parent_form_token='new-intake', current_enrolment_id=21, ordinary_relationship_cycle_id=20, ordinary_relationship_contract_id=201)

    def test_cross_student_and_invalid_token_rejected(self):
        for token in ('other-intake', 'missing'):
            with self.subTest(token=token), self.assertRaises(self.svc.SourceAuthorityError): self.save(parent_form_token=token)
        self.db.execute("UPDATE parent_form_tokens SET invalidated_at='2026-09-07' WHERE token='new-intake'")
        with self.assertRaises(self.svc.SourceAuthorityError): self.save()

    def test_unsubmitted_intake_rejected(self):
        self.db.execute("UPDATE parent_form_tokens SET submitted_at=NULL WHERE token='new-intake'")
        with self.assertRaises(self.svc.SourceAuthorityError): self.save()

    def test_same_contract_same_evidence_is_idempotent(self):
        first = self.save()
        self.assertEqual(first['id'], self.save()['id'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM transfer_source_authorities').fetchone()[0], 1)
        self.db.execute("INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at) VALUES('new-intake-2','new','new_student_transfer','2026-09-07')")
        with self.assertRaises(self.svc.SourceAuthorityError): self.save(parent_form_token='new-intake-2')

    def test_reserve_unbound_draft_creates_or_reuses_one_safe_student_identity(self):
        self.db.executescript("""
          ALTER TABLE contracts ADD COLUMN consulting_and_visa_service_fee REAL;
          ALTER TABLE contracts ADD COLUMN contract_file_path TEXT;
          ALTER TABLE contracts ADD COLUMN contract_instance_json TEXT;
          ALTER TABLE contracts ADD COLUMN signing_link TEXT;
        """)
        first = self.svc.reserve_unbound_transfer_application_draft(
            self.db, 'new', actor_user_id=1, occurred_at='2026-09-07'
        )
        second = self.svc.reserve_unbound_transfer_application_draft(
            self.db, 'new', actor_user_id=1, occurred_at='2026-09-07'
        )
        self.assertEqual(first, second)
        rows = self.db.execute(
            """SELECT id,student_id,contract_type,contract_status,application_cycle_id,
                      consulting_and_visa_service_fee,contract_file_path,
                      contract_instance_json,signing_link
                 FROM contracts WHERE student_id='new'
                   AND contract_type='TransferApplicationContract' AND contract_status='Draft'"""
        ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual(
            (first, 'new', 'TransferApplicationContract', 'Draft', None,
             None, None, None, None),
            tuple(rows[0]),
        )

    def test_reserve_unbound_draft_holds_on_multiple_pending_or_wrong_student(self):
        self.db.execute(
            "INSERT INTO contracts VALUES(101,'new','TransferApplicationContract','Draft',NULL,NULL,NULL)"
        )
        with self.assertRaisesRegex(self.svc.SourceAuthorityError, 'multiple'):
            self.svc.reserve_unbound_transfer_application_draft(
                self.db, 'new', actor_user_id=1, occurred_at='2026-09-07'
            )
        with self.assertRaises(self.svc.SourceAuthorityError):
            self.svc.reserve_unbound_transfer_application_draft(
                self.db, 'missing', actor_user_id=1, occurred_at='2026-09-07'
            )

    def test_reserve_unbound_draft_rejects_every_signed_artifact_surface(self):
        self.db.executescript("""
          ALTER TABLE contracts ADD COLUMN signed_file_path TEXT;
          ALTER TABLE contracts ADD COLUMN final_signed_file_path TEXT;
          ALTER TABLE contracts ADD COLUMN signed_at TEXT;
          ALTER TABLE contracts ADD COLUMN fully_executed_at TEXT;
          CREATE TABLE contract_versions(id INTEGER PRIMARY KEY,contract_id INTEGER);
          CREATE TABLE contract_signing_links(id INTEGER PRIMARY KEY,contract_id INTEGER);
          CREATE TABLE contract_signing_logs(id INTEGER PRIMARY KEY,contract_id INTEGER);
          CREATE TABLE contract_signatures(id INTEGER PRIMARY KEY,contract_id INTEGER);
        """)
        for column, value in (
            ('signed_file_path', '/isolated/signed.pdf'),
            ('final_signed_file_path', '/isolated/final.pdf'),
            ('signed_at', '2026-09-17T10:00:00+10:00'),
            ('fully_executed_at', '2026-09-17T11:00:00+10:00'),
        ):
            with self.subTest(column=column):
                self.db.execute(f'UPDATE contracts SET {column}=? WHERE id=100', (value,))
                with self.assertRaisesRegex(self.svc.SourceAuthorityError, 'HOLD'):
                    self.svc.reserve_unbound_transfer_application_draft(
                        self.db, 'new', actor_user_id=1, occurred_at='2026-09-17'
                    )
                self.db.execute(f'UPDATE contracts SET {column}=NULL WHERE id=100')
        for table in (
            'contract_versions', 'contract_signing_links',
            'contract_signing_logs', 'contract_signatures',
        ):
            with self.subTest(table=table):
                self.db.execute(f'INSERT INTO {table}(id,contract_id) VALUES(1,100)')
                with self.assertRaisesRegex(self.svc.SourceAuthorityError, 'HOLD'):
                    self.svc.reserve_unbound_transfer_application_draft(
                        self.db, 'new', actor_user_id=1, occurred_at='2026-09-17'
                    )
                self.db.execute(f'DELETE FROM {table}')

    def test_failed_source_validation_rolls_back_new_draft_without_orphan(self):
        self.db.execute("DELETE FROM contracts WHERE id=300")
        self.db.commit()
        try:
            with self.db:
                contract_id = self.svc.reserve_unbound_transfer_application_draft(
                    self.db, 'other', actor_user_id=1, occurred_at='2026-09-07'
                )
                self.svc.save_authority(
                    self.db, 'other', contract_id, actor_user_id=1,
                    occurred_at='2026-09-07', parent_form_token='missing',
                )
        except self.svc.SourceAuthorityError:
            pass
        self.assertEqual(
            0,
            self.db.execute(
                """SELECT count(*) FROM contracts WHERE student_id='other'
                   AND contract_type='TransferApplicationContract' AND contract_status='Draft'"""
            ).fetchone()[0],
        )

    def test_submitted_unlinked_intake_and_existing_evidence_holds(self):
        self.db.execute("INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at) VALUES('old-existing-intake','existing','new_student_transfer','2020-01-01')")
        with self.assertRaisesRegex(
                self.svc.SourceAuthorityError,
                'Parent intake conflicts with Existing dual evidence'):
            self.existing()
        self.assertEqual(
            self.db.execute('SELECT count(*) FROM transfer_source_authorities').fetchone()[0],
            0,
        )

    def test_multiple_intakes_require_explicit_selection(self):
        self.db.execute("INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at) VALUES('new-intake-2','new','new_student_transfer','2026-09-07')")
        with self.assertRaises(self.svc.SourceAuthorityError): self.save(parent_form_token=None)
        self.save(parent_form_token='new-intake-2')
        self.assertEqual(self.resolve()['parent_form_token'], 'new-intake-2')

    def test_pending_revalidates_changed_evidence_before_creation(self):
        self.existing()
        self.db.execute("UPDATE student_school_enrolments SET enrolment_role='historical' WHERE id=21")
        with self.assertRaises(self.svc.SourceAuthorityError): self.resolve('existing', 200)

    def test_pending_legacy_row_is_not_contract_eligible_until_explicitly_reverified(self):
        self.db.execute("""INSERT INTO transfer_source_authorities(
            student_id,transfer_contract_id,parent_form_token,rule_version,created_by,created_at
        ) VALUES('new',100,'new-intake','transfer-source-v1',1,'2026-09-06')""")
        with self.assertRaisesRegex(self.svc.SourceAuthorityError, 'SOURCE VERIFICATION HOLD'):
            self.svc.resolve_for_contract(self.db, 'new', 100)
        row = self.save()
        self.assertEqual('verified', row['lifecycle_state'])
        self.assertEqual('new_transfer', row['source_classification'])

    def test_consume_requires_binding_then_preserves_history(self):
        row = self.existing()
        with self.assertRaises(self.svc.SourceAuthorityError): self.svc.consume_authority(self.db, row['id'], 99, actor_user_id=1, occurred_at='2026-09-07')
        self.bind('existing', 200, 'aaie_existing')
        result = self.svc.consume_authority(self.db, row['id'], 99, actor_user_id=1, occurred_at='2026-09-07')
        self.assertEqual(result['source_classification'], 'aaie_existing')
        self.assertEqual(result['lifecycle_state'], 'consumed')
        self.db.execute("UPDATE student_school_enrolments SET enrolment_role='historical' WHERE id=21")
        self.assertEqual(self.svc.load_for_contract(self.db, 'existing', 200)['source_classification'], 'aaie_existing')
        again = self.svc.consume_authority(self.db, row['id'], 99, actor_user_id=1, occurred_at='2026-09-08')
        self.assertEqual(again['verified_at'], '2026-09-07')
        with self.assertRaises(self.svc.SourceAuthorityError): self.resolve('existing', 200)

    def test_source_options_are_student_scoped_and_valid(self):
        options = self.svc.list_source_options(self.db, 'new')
        self.assertEqual([r['token'] for r in options['intakes']], ['new-intake'])
        self.assertEqual(options['existing'], [])
        existing = self.svc.list_source_options(self.db, 'existing')['existing']
        self.assertEqual(existing[0]['current_enrolment_id'], 21)
        self.assertEqual(existing[0]['ordinary_relationship_contract_id'], 201)

    def test_invalid_actor_contract_and_timestamp_hold(self):
        for overrides in (dict(actor_user_id=999), dict(actor_user_id=None), dict(occurred_at='')):
            args = dict(actor_user_id=1, occurred_at='2026-09-07', parent_form_token='new-intake')
            args.update(overrides)
            with self.subTest(args=args), self.assertRaises(self.svc.SourceAuthorityError): self.svc.save_authority(self.db, 'new', 100, **args)
        with self.assertRaises(self.svc.SourceAuthorityError): self.save('new', 300)

    def test_caller_controls_commit_and_rollback(self):
        self.db.commit()
        self.save()
        self.db.rollback()
        self.assertIsNone(self.svc.load_for_contract(self.db, 'new', 100))

    def test_unauthorized_role_cannot_save_authority(self):
        with self.assertRaises(self.svc.SourceAuthorityError):
            self.svc.save_authority(self.db, 'new', 100, parent_form_token='new-intake', actor_user_id=2, occurred_at='2026-09-07')
        self.assertEqual(self.db.execute('SELECT count(*) FROM transfer_source_authorities').fetchone()[0], 0)

    def test_permission_revoked_before_consume_holds(self):
        row = self.save()
        self.bind()
        self.db.execute("UPDATE roles SET permissions_json='[]' WHERE id=1")
        with self.assertRaises(self.svc.SourceAuthorityError):
            self.svc.consume_authority(self.db, row['id'], 99, actor_user_id=1, occurred_at='2026-09-07')
        self.assertEqual(self.db.execute('SELECT lifecycle_state FROM transfer_source_authorities').fetchone()[0], 'verified')

    def test_disabled_flags_cannot_save_authority(self):
        for key in ('STUDENT_CASES_READ_ENABLED', 'TRANSFER_CASES_WRITE_ENABLED'):
            with self.subTest(key=key):
                self.db.execute("UPDATE system_settings SET value='false' WHERE key=?", (key,))
                with self.assertRaises(self.svc.SourceAuthorityError): self.save()
                self.db.execute("UPDATE system_settings SET value='true' WHERE key=?", (key,))

    def test_missing_permission_schema_fails_closed(self):
        self.db.execute('ALTER TABLE roles RENAME TO unavailable_roles')
        with self.assertRaises(self.svc.SourceAuthorityError): self.save()

    def test_inactive_deleted_and_invalid_role_users_fail_closed(self):
        for sql in ("UPDATE users SET is_active=0 WHERE id=1", "UPDATE users SET deleted_at='2026-09-07' WHERE id=1", "UPDATE roles SET permissions_json='not JSON' WHERE id=1"):
            with self.subTest(sql=sql):
                self.db.execute('SAVEPOINT permission_test')
                self.db.execute(sql)
                with self.assertRaises(self.svc.SourceAuthorityError): self.save()
                self.db.execute('ROLLBACK TO permission_test')
                self.db.execute('RELEASE permission_test')

    def test_schema_unavailable_holds(self):
        empty = sqlite3.connect(':memory:')
        self.addCleanup(empty.close)
        self.assertFalse(self.svc.schema_ready(empty))
        with self.assertRaises(self.svc.SourceAuthorityError): self.svc.load_for_contract(empty, 'new', 100)

if __name__ == '__main__': unittest.main()
