#!/usr/bin/env python3
"""Build synthetic full-chain Visa migration evidence; never read a formal DB.

All databases are newly created under this candidate's dedicated SAFEBOX.
Rollback is an online backup restored into a separate file, never in place.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import gc
import json
import re
from pathlib import Path
import shutil
import sqlite3
import sys
import subprocess
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))


def validate_output(path):
    path = Path(path).resolve()
    base = ROOT / 'SAFEBOX/visa-only-release-20260907/migration-qa'
    if not path.is_relative_to(base) or path == base:
        raise ValueError('Output must be a new run directory under candidate migration-qa SAFEBOX')
    return path


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
    return digest.hexdigest()


def backup(source, destination):
    from tools.system_snapshot_manifest import sqlite_online_backup
    source, destination = Path(source).resolve(), Path(destination).resolve()
    safe = ROOT / 'SAFEBOX/visa-only-release-20260907'
    if not source.is_relative_to(safe) or not destination.is_relative_to(safe):
        raise ValueError('Backup may only read/write this task SAFEBOX; never Production')
    if destination.exists(): raise FileExistsError(destination)
    sqlite_online_backup(source, destination)
    # The existing primitive uses connection context managers, which do not close
    # sqlite handles. Collect its now-unreachable handles before pinning WAL hashes.
    gc.collect()


def metadata(database, directory, label):
    backup_path = directory / (label + '-backup.sqlite3')
    manifest_path = directory / (label + '-backup.json')
    backup(database, backup_path)
    manifest = {'database_path':str(database), 'backup_path':str(backup_path), 'backup_method':'sqlite_online_backup', 'created_at':datetime.now(timezone.utc).isoformat(), 'source_database_sha256':sha(database), 'backup_sha256':sha(backup_path), 'quick_check':'ok', 'integrity_check':'ok'}
    with closing(sqlite3.connect(backup_path)) as conn:
        assert conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {'backup_path':str(backup_path), 'manifest_path':str(manifest_path)}


def snapshot(path, columns=None):
    columns = dict(columns or {})
    result = {}
    with closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)) as conn:
        tables = list(columns) if columns else [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations' ORDER BY name")]
        for table in tables:
            names = columns.get(table) or [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            columns[table] = names
            projection = ','.join('"' + name + '"' for name in names)
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0] or ''
            order = ','.join('"' + row[1] + '"' for row in sorted(conn.execute(f'PRAGMA table_info("{table}")'), key=lambda row:row[5]) if row[5]) if 'WITHOUT ROWID' in sql.upper() else 'rowid'
            cursor = conn.execute(f'SELECT {projection} FROM "{table}" ORDER BY {order}')
            digest = hashlib.sha256(); count = 0
            while True:
                batch = cursor.fetchmany(32)
                if not batch: break
                for row in batch:
                    digest.update(json.dumps(list(row), ensure_ascii=False, default=lambda value:{'bytes_hex':value.hex()}, separators=(',', ':')).encode())
                    digest.update(b'\n'); count += 1
            result[table] = {'rows':count, 'sha256':digest.hexdigest()}
    return result, columns


def run(output, source_backup=None):
    import migration_runner
    from database import Database
    source_commit = subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip()
    output = validate_output(output)
    output.mkdir(parents=True, exist_ok=False)
    database = output / 'candidate.sqlite3'
    report = {'status':'RUNNING', 'source':'New synthetic Database.initialize + all explicit migrations; no Production source', 'database':str(database), 'stages':[], 'source_commit':source_commit, 'visa_migration_version':'20260907_035', 'migration_sha256':sha(ROOT/'migrations/20260907_035_visa_management_authorities.sql')}
    try:
        if source_backup is not None:
            source_backup = Path(source_backup).resolve()
            allowed = ROOT/'SAFEBOX/visa-only-release-20260907/backup-01/database_backup.sqlite3'
            if source_backup != allowed or not source_backup.is_file():
                raise ValueError('Only the explicit verified backup-01 source is accepted')
            manifest = json.loads((source_backup.parent/'manifest.json').read_text())
            if Path(manifest['backup_path']).resolve() != source_backup or manifest['backup_method'] != 'tools.system_snapshot_manifest.sqlite_online_backup':
                raise ValueError('Unrecognized formal backup provenance')
            if manifest['quick_check'] != [['ok']] or manifest['integrity_check'] != [['ok']] or manifest['foreign_key_violations']:
                raise ValueError('Backup verification was not clean')
            source_hash = sha(source_backup)
            if source_hash != manifest['backup_sha256']: raise ValueError('Backup hash mismatch')
            backup(source_backup, database)
            report.update(source='Verified Production backup copied to isolated target with existing online-backup authority; no Production DB access', source_backup=str(source_backup), source_backup_sha256=source_hash)
            packages = output/'packages'; (packages/'checks').mkdir(parents=True)
            for sql in sorted(p for p in (ROOT/'migrations').glob('*.sql') if not p.name.startswith('._')):
                if sql.name.startswith('20260907_035_visa_management_authorities'): break
                shutil.copy2(sql, packages/sql.name)
                check = ROOT/'migrations/checks'/(sql.stem+'_forward_check.sql')
                shutil.copy2(check, packages/'checks'/check.name)
            report['baseline_verify'] = migration_runner.verify_migrations(database, packages)
        else:
            Database(database).initialize()
            packages = output / 'packages'; (packages/'checks').mkdir(parents=True)
            for sql in sorted(p for p in (ROOT/'migrations').glob('*.sql') if not p.name.startswith('._')):
                if sql.name.startswith('20260907_035_visa_management_authorities'): break
                shutil.copy2(sql, packages/sql.name)
                check = ROOT/'migrations/checks'/(sql.stem+'_forward_check.sql')
                shutil.copy2(check, packages/'checks'/check.name)
                migration = migration_runner.discover_migrations(packages)[-1]
                embedded = False
                with closing(sqlite3.connect(database)) as conn:
                    try:
                        migration_runner._run_forward_check(conn, migration, read_only=False)
                    except migration_runner.MigrationForwardCheckError:
                        pass
                    else:
                        embedded = True
                        migration_runner._ensure_registry(conn)
                        migration_runner._insert_applied_migration(conn, migration, execution_ms=0, applied_by='synthetic-fixture-existing-schema-forward-verified', source_commit=source_commit)
                        conn.commit()
                if embedded:
                    result = {'version':migration.version, 'mode':'existing Database.SCHEMA verified then baseline registered; not migration execution'}
                else:
                    meta = metadata(database, output, sql.stem)
                    result = migration_runner.apply_migrations(database, packages, applied_by='visa-037-synthetic-chain', source_commit=source_commit, formal_database_path=None, required_backup_metadata=meta)
                    assert result['applied'] == [sql.name[:12]], result
                report['stages'].append(result)
            from tests.visa_test_schema import prepare_production_visa_schema
            with closing(sqlite3.connect(database)) as conn:
                prepare_production_visa_schema(conn)
            # Seed a real legacy Visa case and dependent checklist link before rebuild.
            with closing(sqlite3.connect(database)) as conn:
                conn.execute('PRAGMA foreign_keys=ON')
                role = conn.execute("SELECT id FROM roles WHERE name='Admin'").fetchone()[0]
                conn.execute("INSERT INTO users(id,username,display_name,password_hash,role_id,created_at) VALUES(97037,'visa037','Visa QA','not-a-login-secret',?,'2026-09-07')",(role,))
                conn.execute("INSERT INTO students(student_id,passport_last_name,passport_first_name,created_date,updated_at,form_data_json) VALUES('VISA037','Fixture','Synthetic','2026-09-07','2026-09-07','{}')")
                conn.execute("INSERT INTO visa_cases(id,student_id,visa_case_name,visa_type,visa_subclass,created_at,updated_at,guardian_applicant) VALUES(97037,'VISA037','Synthetic legacy case','500','500','2026-09-07','2026-09-07','Parent 590 evidence retained')")
                conn.commit()
        before, columns = snapshot(database)
        with closing(sqlite3.connect(database)) as conn:
            original_schema_objects = {row[0]:re.sub(r'\s+', ' ', row[1]).strip() for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger','view') AND sql IS NOT NULL")}
        report['original_schema_objects'] = original_schema_objects
        with closing(sqlite3.connect(database)) as conn:
            original_tables = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            registry_before = conn.execute('SELECT * FROM schema_migrations ORDER BY rowid').fetchall()
        sql = ROOT/'migrations/20260907_035_visa_management_authorities.sql'
        check = ROOT/'migrations/checks'/(sql.stem+'_forward_check.sql')
        shutil.copy2(sql, packages/sql.name); shutil.copy2(check, packages/'checks'/check.name)
        meta = metadata(database, output, '037')
        report['visa_migration'] = migration_runner.apply_migrations(database, packages, applied_by='visa-037-independent-review', source_commit=source_commit, formal_database_path=None, required_backup_metadata=meta)
        after, _ = snapshot(database, columns)
        with closing(sqlite3.connect(database)) as conn:
            current_schema_objects = {row[0]:re.sub(r'\s+', ' ', row[1]).strip() for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger','view') AND sql IS NOT NULL")}
        with closing(sqlite3.connect(database)) as conn:
            current_tables = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            registry_after = conn.execute('SELECT * FROM schema_migrations ORDER BY rowid').fetchall()
        report['unchanged_other_table_definitions'] = all(current_tables.get(name) == sql for name, sql in original_tables.items() if name not in ('visa_cases','visa_evidence_profiles'))
        report['new_tables'] = sorted(set(current_tables)-set(original_tables))
        report['revenue_schema_delta'] = [name for name in set(original_tables)|set(current_tables)|set(original_schema_objects)|set(current_schema_objects) if name.startswith('revenue_') and (original_tables.get(name)!=current_tables.get(name) or original_schema_objects.get(name)!=current_schema_objects.get(name))]
        report['historical_registry_preserved'] = registry_after[:len(registry_before)] == registry_before
        assert report['unchanged_other_table_definitions'] and not report['revenue_schema_delta'] and report['historical_registry_preserved']
        changed_objects = [name for name, sql in original_schema_objects.items() if current_schema_objects.get(name) != sql]
        report['changed_original_schema_objects'] = changed_objects
        assert not changed_objects, '037 lost or changed existing indexes/triggers/views: ' + str(changed_objects)
        report['table_counts_and_hashes_before'] = before
        report['table_counts_and_hashes_after_original_columns'] = after
        report['legacy_row_parity'] = before == after
        assert before == after, '037 changed baseline rows'
        report['verify'] = migration_runner.verify_migrations(database, packages)
        report['idempotent'] = migration_runner.apply_migrations(database, packages, applied_by='visa-037-repeat', source_commit=source_commit, formal_database_path=None)
        with closing(sqlite3.connect(database)) as conn:
            report['foreign_key_check'] = conn.execute('PRAGMA foreign_key_check').fetchall()
            report['integrity_check'] = conn.execute('PRAGMA integrity_check').fetchone()[0]
        assert report['foreign_key_check'] == []
        assert report['integrity_check'] == 'ok'
        restore = output/'restored-pre037.sqlite3'; backup(Path(meta['backup_path']), restore)
        restored, _ = snapshot(restore, columns)
        report['restore_parity'] = before == restored
        assert before == restored
        with closing(sqlite3.connect(restore)) as conn:
            restored_tables = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            restored_objects = {row[0]:re.sub(r'\s+', ' ', row[1]).strip() for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger','view') AND sql IS NOT NULL")}
            report['restore_integrity_check'] = conn.execute('PRAGMA integrity_check').fetchone()[0]
            report['restore_foreign_key_check'] = conn.execute('PRAGMA foreign_key_check').fetchall()
            report['restore_registry_parity'] = conn.execute('SELECT * FROM schema_migrations ORDER BY rowid').fetchall() == registry_before
        report['restore_schema_parity'] = restored_tables == original_tables and restored_objects == original_schema_objects
        assert report['restore_integrity_check'] == 'ok' and not report['restore_foreign_key_check']
        assert report['restore_registry_parity'] and report['restore_schema_parity']
        # Unknown columns must abort with transaction and registry unchanged.
        drift = output/'unknown-column.sqlite3'; backup(restore, drift)
        with closing(sqlite3.connect(drift)) as conn:
            conn.execute('ALTER TABLE visa_cases ADD COLUMN unrecognized_business_value TEXT'); conn.commit()
        drift_before, drift_columns = snapshot(drift)
        drift_meta = metadata(drift, output, 'drift037')
        try:
            migration_runner.apply_migrations(drift, packages, applied_by='visa-037-drift', source_commit=source_commit, formal_database_path=None, required_backup_metadata=drift_meta)
        except migration_runner.MigrationError as exc:
            report['unknown_column_rejected'] = str(exc)
        else:
            raise AssertionError('Unknown legacy column was silently lost')
        assert snapshot(drift, drift_columns)[0] == drift_before
        with closing(sqlite3.connect(drift)) as conn:
            assert conn.execute("SELECT count(*) FROM schema_migrations WHERE version='20260907_035'").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('visa_applicants','visa_cases_rebuild_037','visa_document_audit')").fetchone()[0] == 0
        report['unknown_column_registry_and_schema_unchanged'] = True
        report['unknown_column_rollback_parity'] = True
        if source_backup is not None:
            report['source_backup_unchanged'] = sha(source_backup) == source_hash
            assert report['source_backup_unchanged']
        report['status'] = 'PASS'
    except Exception as exc:
        report['status'] = 'FAIL'; report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        (output/'report.json').write_text(json.dumps(report, indent=2, default=str))
    return report

if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', required=True, type=Path); parser.add_argument('--source-backup', type=Path)
    args=parser.parse_args(); result=run(args.output,args.source_backup)
    print(json.dumps({'status':result['status'],'database':result['database'],'report':str(args.output/'report.json')}, indent=2))
