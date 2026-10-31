"""Explicit, one-use data preparation for the approved November 1 cutover.

Never stops services or overwrites a target. Run final preparation only after
the old writers have been stopped. Historical snapshots/audit text stay intact.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from runtime_configuration import REQUIRED_DIRECTORIES, RuntimeConfiguration, preflight
from migration_runner import apply_migrations, FORMAL_CONFIRMATION

OLD = Path('/Users/faqiang/Downloads/AAIE_CRM_Test_5985902_20261030/STAGING_DATA')
RUNTIME = SOURCE.parent / 'runtime'
SEED = RUNTIME / 'staging/separated-smoke-20261101'
EXTERNAL_AGENT = dict(pid=10362, started='Fri Oct 30 06:10:07 2026',
    command='/Volumes/Zhang/AAIE CodeX/agent-reach/xhs-monitor-ui/venv/bin/python /Volumes/Zhang/AAIE CodeX/agent-reach/candidates/xhs-intelligence-phase4-20260906T080029+1000/app.py --host 127.0.0.1 --port 8790')
POINTERS = {
    'students': ['folder_path'],
    'student_documents': ['file_path', 'preview_path'],
    'contracts': ['contract_file_path'],
    'upload_review_items': ['staging_file_path', 'final_file_path'],
    'school_plan_reports': ['file_path'],
    'school_plan_report_confirmations': ['signature_image_path', 'confirmation_pdf_path'],
    'contract_signatures': ['signature_image_path', 'confirmation_pdf_path'],
}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def backup(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not dst.execute('PRAGMA foreign_key_check').fetchall()


def copy_tree(source, target, files):
    if not source.exists():
        return
    for path in source.rglob('*'):
        if any(part.startswith('._') for part in path.relative_to(source).parts):
            continue
        if path.is_symlink():
            raise RuntimeError('Source symlink rejected: '+str(path))
        dest = target / path.relative_to(source)
        if path.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
            expected = digest(path)
            assert digest(dest) == expected
            files.append(dict(source=str(path), target=str(dest), sha256=expected))


def prepare(target, production=False):
    target = target.absolute()
    if target.exists() or target.is_symlink():
        raise RuntimeError('Target already exists; archive explicitly first')
    if production:
        assert target == RUNTIME / 'production'
        from tools.runtime_control import observe
        assert observe(EXTERNAL_AGENT['pid'],'lstart') == EXTERNAL_AGENT['started'], 'External Agent birth changed'
        assert observe(EXTERNAL_AGENT['pid'],'command') == EXTERNAL_AGENT['command'], 'External Agent command changed'
    else:
        assert target.parent == RUNTIME / 'staging'
    target.mkdir(parents=True)
    for name in (*REQUIRED_DIRECTORIES, 'database', 'auxiliary'):
        (target/name).mkdir(exist_ok=True)
    report = dict(source=str(OLD), target=str(target), files=[], rebased={}, missing_before=[])
    for name in ('staging.db', 'content_extraction_service.db'):
        backup(OLD/'database'/name, target/'database'/name)
        backup(target/'database'/name, target/'backups'/('before-cutover-'+name))
    for name in ('storage', 'templates', 'secure_assets', 'upload_staging', 'reports',
                 'school_library_files', 'school_data_sources', 'revenue_documents',
                 'import_reports', 'contracts', 'cache/previews'):
        copy_tree(OLD/name, target/name, report['files'])
    key = OLD/'config/field_encryption.key'
    shutil.copyfile(key, target/'config/field_encryption.key')
    assert digest(key) == digest(target/'config/field_encryption.key')
    copy_tree(SEED/'auxiliary/ocr-models', target/'auxiliary/ocr-models', report['files'])
    copy_tree(SEED/'auxiliary/agent-source', target/'auxiliary/agent-source', report['files'])
    if not production:
        backup(RUNTIME/'imports/production-20261101-022012/agent-reach/database.sqlite3',
               target/'auxiliary/agent-reach/database.sqlite3')
    deps = json.loads((SEED/'config/auxiliary.json').read_text())
    deps['agent_source'] = str(target/'auxiliary/agent-source')
    if production:
        deps['agent_management'] = 'external'
        (target/'config/external-agent.json').write_text(json.dumps(EXTERNAL_AGENT,indent=2))
    (target/'config/auxiliary.json').write_text(json.dumps(deps,indent=2))
    for name in ('nas-readonly.json','school-library-readonly.json',
                 *(('nas-write.json','admin-lan.json') if production else ())):
        if (OLD/name).is_file():
            shutil.copyfile(OLD/name,target/name)
    config = dict(version=1,mode='production' if production else 'staging',root=str(target),
                  database='staging.db',admin_port=8780 if production else 30880,
                  parent_port=8781 if production else 30881,extraction_port=8792 if production else 30892,
                  namespace='aaie_production_' if production else 'aaie_staging_rehearsal_')
    (target/'config/runtime.json').write_text(json.dumps(config,indent=2))
    db = target/'database/staging.db'
    commit = subprocess.check_output(['git','rev-parse','HEAD'],cwd=SOURCE,text=True).strip()
    report['migration'] = apply_migrations(db, SOURCE/'migrations', applied_by='approved-cutover-20261101',
        source_commit=commit, formal_database_path=db if production else None,
        allow_formal_database=production, confirmation=FORMAL_CONFIRMATION if production else None,
        expected_database_sha256=digest(db) if production else None)
    with closing(sqlite3.connect(db)) as conn, conn:
        report['counts'] = {t:conn.execute('SELECT count(*) FROM '+t).fetchone()[0]
                            for t in ('students','parents','contracts','schools','users')}
        for table, columns in POINTERS.items():
            for column in columns:
                rows = conn.execute(f'SELECT rowid,"{column}" FROM "{table}" WHERE "{column}" LIKE ?',
                                    (str(OLD)+'/%',)).fetchall()
                for rowid, value in rows:
                    source = Path(value)
                    dest = target/source.relative_to(OLD)
                    if source.exists():
                        if source.is_file():
                            assert dest.is_file() and digest(source) == digest(dest), str(dest)
                        else:
                            assert dest.is_dir(), str(dest)
                    else:
                        report['missing_before'].append(dict(table=table,column=column,rowid=rowid))
                    conn.execute(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?',(str(dest),rowid))
                report['rebased'][table+'.'+column] = len(rows)
        settings = {'test_root':str(target/'storage'), 'mode':config['mode']}
        if production:
            settings.update(production_root=str(target/'storage'))
        else:
            settings.update(PARENT_FORM_PUBLIC_BASE_URL='http://127.0.0.1:30881',
                            CRM_ADMIN_LAN_URL='http://127.0.0.1:30880',
                            CRM_PUBLIC_BASE_URL='http://127.0.0.1:30880', CRM_ADMIN_BIND_HOST='127.0.0.1')
        for key,value in settings.items():
            conn.execute('UPDATE system_settings SET value=? WHERE key=?',(value,key))
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        # User credential/role rows must be byte-for-byte unchanged.
        with closing(sqlite3.connect((target/'backups/before-cutover-staging.db').as_uri()+'?mode=ro',uri=True)) as before:
            for table in ('users','roles','audit_logs'):
                assert conn.execute('SELECT * FROM '+table).fetchall() == before.execute('SELECT * FROM '+table).fetchall(), table
    if production:
        from business_write_gate import Gate
        gate=Gate(db,target/'config/business-write-gate')
        gate.initialize_open()
        report['gate']=gate.enable()
    report['preflight']=preflight(RuntimeConfiguration.load(target/'config/runtime.json',source_root=SOURCE))
    assert report['preflight']['ready'], report['preflight']['errors']
    (target/'PREPARATION.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
    print(json.dumps({k:v for k,v in report.items() if k not in ('files','migration')},indent=2))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--production',action='store_true')
    args=parser.parse_args()
    prepare(args.target,args.production)
