"""Explicit Visa material/review/result service boundaries and ownership scopes."""
from functools import wraps
from inspect import signature
from dashboard_visa_metrics import is_student_visa_case
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .runtime import enabled

EVENTS={
    'register_document':'visa.document_registered',
    'mark_submitted':'visa.document_submitted',
    'set_document_archived':'visa.document_lifecycle_changed',
    'save_extraction':'visa.extraction_saved',
    'retire_letter_extraction':'visa.extraction_retired',
    'confirm_letter':'visa.letter_confirmed',
    'classify_document':'visa.document_classified',
    'edit_document_metadata':'visa.document_metadata_changed',
    'record_auto_result':'visa.result_detected',
    'record_manual_result':'visa.result_confirmed',
    'save_processing_extraction':'visa.processing_dates_extracted',
    'correct_processing_dates':'visa.processing_dates_corrected',
    'correct_grant_date':'visa.grant_date_corrected',
    'link_recorded_visa_grant':'visa.cycle_assigned',
    'save_ai_letter':'visa.ai_extraction_saved',
    'save_ai_analysis':'visa.ai_analysis_saved',
}
CASE_TABLES=(('visa_case_documents','visa_case_id'),('visa_immi_submissions','visa_case_id'),
    ('visa_letter_extractions','visa_case_id'),('visa_letter_reviews','visa_case_id'),
    ('visa_document_audit','visa_case_id'),('visa_result_status_current','visa_case_id'),
    ('visa_result_status_events','visa_case_id'),('visa_case_confirmed_decisions','visa_case_id'),
    ('visa_decision_events','visa_case_id'),('visa_processing_date_extractions','visa_case_id'),
    ('visa_processing_date_revisions','visa_case_id'))


def case_snapshot(conn,case_id):
    cursor=conn.execute('SELECT * FROM visa_cases WHERE id=?',(case_id,));row=cursor.fetchone()
    if row is None:raise ValueError('Visa Case not found.')
    case=dict(zip((x[0] for x in cursor.description),row));graph=[]
    present={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table,column in CASE_TABLES:
        if table not in present:continue
        # Explicit case-owned audit/authority rows; never read files or text via OCR.
        cursor=conn.execute('SELECT * FROM '+table+' WHERE '+column+'=?',(case_id,))
        columns=[i for i,x in enumerate(cursor.description) if x[0]!='updated_at']
        rows=tuple(sorted((tuple(row[i] for i in columns) for row in cursor.fetchall()),key=repr))
        graph.append((table,rows))
    if 'audit_logs' in present:
        columns={r[1] for r in conn.execute('PRAGMA table_info(audit_logs)')}
        if {'id','action_type','new_value'}<=columns:
            retired=conn.execute("""SELECT id,new_value FROM audit_logs
                WHERE action_type='visa_letter_extraction_retired' AND json_valid(new_value)
                  AND CAST(json_extract(new_value,'$.visa_case_id') AS INTEGER)=? ORDER BY id""",(case_id,)).fetchall()
            graph.append(('retired_extractions',tuple(tuple(r) for r in retired)))
    return (case,tuple(graph))


def case_scopes(conn,case):
    scopes=[Subject('module','visa_cases'),Subject('dashboard','visa_cases')]
    student_id=case.get('student_id')
    if student_id:scopes.append(Subject('student',str(student_id)))
    if student_id and is_student_visa_case(case):
        scopes.extend((Subject('dashboard','workflow'),Subject('dashboard','overview')))
        cycle_id=case.get('application_cycle_id')
        if cycle_id is not None:
            owner=conn.execute('SELECT student_id FROM application_cycles WHERE id=?',(cycle_id,)).fetchone()
            if owner and str(owner[0])==str(student_id):scopes.append(Subject('application_cycle',str(cycle_id)))
    return tuple(scopes)


def synchronized_visa_write(function):
    event_type=EVENTS[function.__name__];sig=signature(function)
    @wraps(function)
    def wrapped(conn,*args,**kwargs):
        if not enabled(conn):return function(conn,*args,**kwargs)
        params=sig.bind(conn,*args,**kwargs).arguments
        case_id=int(params['case_id']);actor=params.get('actor_user_id')
        if actor is None and params.get('actor') is not None:
            try:actor=params['actor']['id']
            except (KeyError,IndexError,TypeError):actor=getattr(params['actor'],'id',None)
        def describe(view,result,before,after):
            return Mutation(event_type,Subject('visa_case',str(case_id)),case_scopes(view,after[0]),case_scopes(view,before[0]))
        return run_domain_write(conn,event_type=event_type,actor_kind='user' if actor is not None else 'system',
            actor_ref=str(actor) if actor is not None else 'visa_domain_service',
            capture=lambda view:case_snapshot(view,case_id),operation=lambda view:function(view,*args,**kwargs),describe=describe)
    return wrapped
