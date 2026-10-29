"""Independent 037 runner and preservation checks. No formal database access."""
import importlib.util
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

from tests.visa_test_schema import PRODUCTION_VISA_DDL, PRODUCTION_VISA_INDEXES

class VisaMigration037Tests(unittest.TestCase):
    def test_production_shape_retains_all_legacy_columns_and_values(self):
        import sqlite3
        paths=list((ROOT/'migrations').glob('*_visa_management_authorities.sql'))
        sql=paths[0].read_text()
        # Execute the actual migration through the copy/row-count guard before DROP.
        prefix=sql.split('DROP TRIGGER visa_decision_events_ownership_insert;')[0]
        with sqlite3.connect(':memory:') as c:
            c.execute(PRODUCTION_VISA_DDL)
            for sql in PRODUCTION_VISA_INDEXES: c.execute(sql)
            c.execute("INSERT INTO visa_cases(id,student_id,visa_case_name,created_at,updated_at,trn,grant_number,internal_notes) VALUES(1,'SYNTHETIC','Synthetic','now','now','TRN-preserve','GRANT-preserve','legacy note')")
            before=c.execute('SELECT * FROM visa_cases').fetchall()
            columns=[x[1] for x in c.execute('PRAGMA table_info(visa_cases)')]
            c.executescript(prefix)
            self.assertEqual(c.execute('SELECT '+','.join(columns)+' FROM visa_cases_rebuild_037').fetchall(),before)
            after={x[1]:x for x in c.execute('PRAGMA table_info(visa_cases_rebuild_037)')}
            self.assertEqual(len(after),60)
            self.assertEqual(after['student_id'][3],0)
            self.assertEqual(after['visa_case_name'][3],1)
            self.assertEqual(after['visa_status'][4],"'准备中'")

    def test_same_count_changed_constraint_and_extra_index_fail_closed(self):
        import sqlite3
        from contextlib import closing
        prefix=(ROOT/'migrations/20260907_035_visa_management_authorities.sql').read_text().split('DROP TRIGGER visa_decision_events_ownership_insert;')[0]
        for changed in ('constraint','index','column'):
            with self.subTest(changed=changed), closing(sqlite3.connect(':memory:')) as c:
                ddl=PRODUCTION_VISA_DDL.replace('visa_case_name TEXT NOT NULL','visa_case_name TEXT') if changed=='constraint' else PRODUCTION_VISA_DDL
                c.execute(ddl)
                for sql in PRODUCTION_VISA_INDEXES:c.execute(sql)
                if changed=='index':c.execute('CREATE INDEX unreviewed_visa_index ON visa_cases(trn)')
                if changed=='column':c.execute('ALTER TABLE visa_cases ADD COLUMN unreviewed_column TEXT')
                with self.assertRaises(sqlite3.IntegrityError):c.executescript(prefix)
                self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name='visa_applicants'").fetchone()[0],0)

    def test_full_rebuild_preserves_production_indexes_and_foreign_keys(self):
        from tests.test_visa_applicant_candidate import legacy_db, MIGRATION
        from contextlib import closing
        with closing(legacy_db()) as c:
            before_fk=c.execute('PRAGMA foreign_key_list(visa_cases)').fetchall()
            before_columns={x['name']:dict(x) for x in c.execute('PRAGMA table_info(visa_cases)')}
            c.executescript(MIGRATION.read_text())
            for sql in PRODUCTION_VISA_INDEXES:
                self.assertIsNotNone(c.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql=?",(sql,)).fetchone())
            after_columns={x['name']:dict(x) for x in c.execute('PRAGMA table_info(visa_cases)')}
            for name,definition in before_columns.items():
                expected=dict(definition)
                if name=='student_id':expected['notnull']=0
                self.assertEqual(after_columns[name],expected)
            signature=lambda rows:{tuple(row)[1:] for row in rows}
            after_fk=signature(c.execute('PRAGMA foreign_key_list(visa_cases)').fetchall())
            self.assertTrue(signature(before_fk).issubset(after_fk))
            self.assertEqual(len(after_fk),len(before_fk)+1)

    def test_global_fullversion_order_and_future_revenue_prefix(self):
        import migration_runner as runner
        migrations=runner.discover_migrations(ROOT/'migrations')
        versions=[m.version for m in migrations]
        self.assertEqual(versions[-4:], ['20260908_036','20260909_037','20260909_038','20260914_039'])
        self.assertEqual(len(versions),len(set(versions)))
        baseline=[m for m in migrations if m.version <= '20260908_036']
        self.assertEqual([m.version for m in migrations[len(baseline):]], ['20260909_037','20260909_038','20260914_039'])
        runner._validate_applied_state(migrations,baseline)
        runner._validate_applied_state(migrations,migrations)
        # Omitting already-applied036 or required037 remains a real prefix error.
        with self.assertRaises(runner.MigrationOrderError):
            runner._validate_applied_state(migrations,baseline[:-1]+migrations[-2:])
        with self.assertRaises(runner.MigrationOrderError):
            runner._validate_applied_state(migrations,baseline+[migrations[-1]])

    def test_guarded_runner_discovers_candidate_package(self):
        import migration_runner
        versions = [m.version for m in migration_runner.discover_migrations(ROOT / 'migrations')]
        self.assertIn('20260907_035', versions)

    def test_full_baseline_runner_rebuild_restore_and_drift(self):
        import uuid
        spec = importlib.util.spec_from_file_location('visa_rehearsal', ROOT / 'tools/visa_candidate_rehearsal.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        result = module.run(ROOT / 'SAFEBOX/visa-only-release-20260907/migration-qa' / ('test-' + uuid.uuid4().hex))
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['visa_migration']['applied'], ['20260907_035'])
        self.assertTrue(result['legacy_row_parity'])
        self.assertTrue(result['restore_parity'])
        self.assertTrue(result['unknown_column_rollback_parity'])
        self.assertEqual(result['foreign_key_check'], [])

    def test_rehearsal_has_explicit_isolated_destination(self):
        path = ROOT / 'tools/visa_candidate_rehearsal.py'
        self.assertTrue(path.exists(), 'Independent migration rehearsal is missing')
        spec = importlib.util.spec_from_file_location('visa_rehearsal', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with self.assertRaises(ValueError): module.validate_output(ROOT / 'database')
        self.assertEqual(module.validate_output(ROOT / 'SAFEBOX/visa-only-release-20260907/migration-qa/new'), ROOT / 'SAFEBOX/visa-only-release-20260907/migration-qa/new')

if __name__ == '__main__': unittest.main()
