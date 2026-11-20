"""Read-only source/candidate consistency report; never repairs or migrates."""
from contextlib import contextmanager
from dataclasses import asdict
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import os
import math
import signal
import sqlite3
import subprocess
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


class AuditDeadlineExceeded(BaseException):
    """Cancellation must cross legacy ``except Exception`` fallbacks."""


def run_bounded_worker(command,seconds):
    """A blocked filesystem call must not turn a partial audit into success.

    Only the parent publishes reports. The child has no report destination and
    may be terminated without leaving an apparently complete output file.
    """
    seconds=float(seconds)
    if not math.isfinite(seconds) or not 0<seconds<=60:raise ValueError('Invalid audit timeout.')
    process=subprocess.Popen(command,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        output,diagnostic=process.communicate(timeout=seconds)
    except BaseException as exc:
        process.terminate()
        try:
            _,diagnostic=process.communicate(timeout=1)
            if diagnostic:sys.stderr.write(diagnostic)
        except subprocess.TimeoutExpired:
            process.kill()
            try:process.communicate(timeout=1)
            except subprocess.TimeoutExpired:pass
        if isinstance(exc,subprocess.TimeoutExpired):raise AuditDeadlineExceeded('readonly_audit_total_timeout') from None
        raise
    if diagnostic:sys.stderr.write(diagnostic)
    if process.returncode:raise RuntimeError('readonly_audit_worker_failed')
    return json.loads(output)


@contextmanager
def audit_deadline(seconds):
    seconds=float(seconds)
    if not math.isfinite(seconds) or not 0<seconds<=60:raise ValueError('Audit timeout must be between zero and sixty seconds.')
    if signal.getitimer(signal.ITIMER_REAL)!=(0.0,0.0):raise RuntimeError('An existing process timer must not be replaced.')
    previous=signal.getsignal(signal.SIGALRM)
    timed_out=False
    def expired(signum,frame):
        nonlocal timed_out
        timed_out=True
        raise AuditDeadlineExceeded('readonly_audit_total_timeout')
    signal.signal(signal.SIGALRM,expired)
    try:
        signal.setitimer(signal.ITIMER_REAL,seconds)
        yield
        if timed_out:raise AuditDeadlineExceeded('readonly_audit_total_timeout')
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        signal.signal(signal.SIGALRM,previous)


@contextmanager
def audit_presence_snapshot():
    """Use the same observed legacy attachment gate in both rule previews."""
    from student_lifecycle import transfer_state
    original=transfer_state._signed_artifact_exists
    observed={}
    def exists(value):
        key=str(value or '').strip()
        if key not in observed:observed[key]=original(value)
        return observed[key]
    with patch.object(transfer_state,'_signed_artifact_exists',exists):yield observed


@contextmanager
def readonly_database(path):
    path=Path(path)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError('An explicit existing regular database file is required.')
    connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    reads={'table_info','index_info','index_list','foreign_key_list','foreign_key_check',
           'integrity_check','query_only','data_version','schema_version'}
    allowed={sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE}
    def authorize(action,arg1,arg2,database,source):
        if action==sqlite3.SQLITE_PRAGMA:
            return sqlite3.SQLITE_OK if str(arg1).lower() in reads and (
                arg2 is None or str(arg1).lower() in {'table_info','index_info','index_list','foreign_key_list'}) else sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_FUNCTION and str(arg2).lower() in {'load_extension','writefile'}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
    connection.set_authorizer(authorize)
    try:yield connection
    finally:
        connection.set_authorizer(None)
        connection.rollback();connection.close()


def distribution_preview(connection,*,loader=None):
    if loader is None:
        from student_application_stage_distribution import load_application_stage_distribution
        loader=load_application_stage_distribution
    rows=tuple(row for row in loader(connection,cycles_only=True) if row.stage.key)
    identities=[('cycle',row.cycle_id) if row.cycle_id is not None else ('student',str(row.student_id)) for row in rows]
    if len(set(identities))!=len(identities):raise ValueError('duplicate_cycle_in_audit_distribution')
    groups={}
    for row in rows:
        groups.setdefault(row.stage.key,[]).append({'cycle_id':row.cycle_id,'student_id':row.student_id,
            'student_name':row.student_name,'entity_kind':getattr(row,'entity_kind','student'),
            'stage_label':row.stage.label})
    return {'total':len(rows),'counts':dict(Counter(row.stage.key for row in rows)),'members':groups}


def collect_source_audit(connection,*,progress=lambda phase:None):
    """Compare legacy/candidate rule evaluations on ONE unchanged source snapshot.

    The feature switch is patched only in this process. No setting, fact,
    relationship or historical outcome is changed. This is a candidate preview,
    not a claim about the HTML of the live production process.
    """
    from student_workflow_projection import load_student_workflow_projection_map
    from visa_business_state import formal_lodgement, resolve_case_state, visa_consistency_issues, grant_review_issue_code
    if 'app' in sys.modules:raise RuntimeError('Audit must run without importing the application runtime.')
    if connection.execute('PRAGMA query_only').fetchone()[0]!=1 or not connection.in_transaction:
        raise ValueError('A protected read-only source snapshot is required.')
    present={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'students','application_cycles','visa_cases'}<=present:raise ValueError('Full business schema is required.')
    students=[dict(row) for row in connection.execute('''SELECT student_id,passport_first_name,passport_last_name
        FROM students WHERE deleted_at IS NULL ORDER BY student_id''')]
    ids=tuple(str(row['student_id']) for row in students)
    with audit_presence_snapshot() as presence:
        progress('legacy_rule_preview')
        with patch('business_changes.runtime.enabled',return_value=False):
            legacy=load_student_workflow_projection_map(connection,ids)
            progress('legacy_distribution_preview')
            legacy_distribution=distribution_preview(connection)
        progress('candidate_rule_preview')
        with patch('business_changes.runtime.enabled',return_value=True):
            candidate=load_student_workflow_projection_map(connection,ids)
            progress('candidate_distribution_preview')
            candidate_distribution=distribution_preview(connection)
    names={str(row['student_id']):' '.join(str(row[key] or '') for key in ('passport_first_name','passport_last_name')).strip()
           for row in students}
    differences=[]
    for sid in ids:
        before=asdict(legacy[sid]) if sid in legacy else None
        after=asdict(candidate[sid]) if sid in candidate else None
        if before!=after:differences.append({'student_id':sid,'student_name':names[sid],'legacy_rule_preview':before,
                                             'candidate_rule_preview':after,'action':'review_evidence_no_automatic_repair'})
    progress('visa_case_authority')
    cases=[]
    for row in connection.execute('SELECT * FROM visa_cases WHERE archived_at IS NULL ORDER BY id').fetchall():
        case=dict(row);state=resolve_case_state(connection,case);issues=[]
        sid=str(case.get('student_id') or '')
        if sid and sid not in names:issues.append('case_student_missing_or_deleted')
        if sid and case.get('application_cycle_id') is None:issues.append('case_cycle_unbound')
        if case.get('application_cycle_id') is not None:
            cycle=connection.execute('SELECT student_id FROM application_cycles WHERE id=?',(case['application_cycle_id'],)).fetchone()
            if cycle is None or str(cycle[0] or '')!=sid:issues.append('case_cycle_owner_mismatch')
        if str(case.get('status') or '').lower() in {'lodged','submitted'} and not formal_lodgement(case):
            issues.append('lodged_label_without_formal_evidence')
        if str(case.get('result_status') or 'Pending')!=state['result']:issues.append('stored_result_differs_from_candidate_authority')
        if state['requires_review']:issues.append(grant_review_issue_code(connection,case,state['result']))
        if issues:cases.append({'case_id':case['id'],'student_id':sid or None,'student_name':names.get(sid),
            'cycle_id':case.get('application_cycle_id'),'visa_type':case.get('visa_type'),
            'stored_status':case.get('status'),'stored_result':case.get('result_status'),
            'candidate_result':state['result'],'candidate_student_status':state['status'],
            'issue_codes':issues,'action':'review_evidence_no_automatic_repair'})
    schema='\n'.join(str(row[0]) for row in connection.execute('SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name'))
    flag=connection.execute("SELECT value FROM system_settings WHERE key='business_change_sync_v1'").fetchone()
    return {'at':datetime.now(timezone.utc).isoformat(),'kind':'readonly_candidate_rule_preview',
        'source_feature_setting':flag[0] if flag else None,'students_checked':len(ids),
        'schema_sha256':hashlib.sha256(schema.encode()).hexdigest(),'database_total_changes':connection.total_changes,
        'attachment_presence_observations':sum(bool(path) for path in presence),
        'legacy_cycle_distribution':legacy_distribution,'candidate_cycle_distribution':candidate_distribution,
        'student_rule_differences':differences,'visa_case_issues':cases,
        'retained_grant_issues':list(visa_consistency_issues(connection)),
        'limits':['No original file content or physical hashes were checked.',
                  'Legacy rule preview uses candidate checkout with its feature switch disabled; it is not a capture of live HTML.',
                  'The two rule previews reuse the same per-audit legacy attachment-presence observations.',
                  'No fact, date, relationship or manual decision is repaired by this tool.']}


def save_report(path,report):
    path=Path(path)
    if not path.is_absolute() or not path.parent.is_dir() or not path.resolve().is_relative_to(ROOT/'reports'):
        raise ValueError('Report must be an explicit new file under this task reports directory.')
    with path.open('x',encoding='utf-8') as output:
        json.dump(report,output,ensure_ascii=False,indent=2);output.write('\n');output.flush();os.fsync(output.fileno())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True);parser.add_argument('--output')
    parser.add_argument('--timeout-seconds',type=float,default=45)
    parser.add_argument('--_collect-json',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if not args._collect_json and not args.output:parser.error('--output is required')
    sys.path.insert(0,str(ROOT))
    def progress(phase):print(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'phase':phase}),file=sys.stderr,flush=True)
    try:
        if args._collect_json:
            with audit_deadline(args.timeout_seconds),readonly_database(Path(args.database)) as connection:
                report=collect_source_audit(connection,progress=progress)
                if connection.total_changes!=0:raise RuntimeError('Audit changed source data.')
            print(json.dumps(report,ensure_ascii=False));return 0
        report=run_bounded_worker([sys.executable,'-B',str(Path(__file__).resolve()),'--database',args.database,
            '--timeout-seconds',str(args.timeout_seconds),'--_collect-json'],args.timeout_seconds)
    except AuditDeadlineExceeded:
        print(json.dumps({'ok':False,'code':'readonly_audit_total_timeout','report_created':False}),file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(json.dumps({'ok':False,'code':str(exc),'report_created':False}),file=sys.stderr)
        return 2
    report['source_database']=str(Path(args.database).resolve())
    save_report(Path(args.output),report)
    print(json.dumps({'students_checked':report['students_checked'],
        'student_rule_differences':len(report['student_rule_differences']),
        'visa_case_issues':len(report['visa_case_issues']),'database_total_changes':report['database_total_changes']}))


if __name__=='__main__':raise SystemExit(main())
