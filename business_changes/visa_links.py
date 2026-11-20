"""Explicit existing-document link boundary; no file IO or inferred SQL hooks."""
import json

from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .visa_domains import case_scopes


def _one(view,sql,params):
    cursor=view.execute(sql,params);row=cursor.fetchone()
    return dict(zip((item[0] for item in cursor.description),row)) if row is not None else None


def link_existing_document(conn,*,database,case_before,document_before,actor,role,notes,occurred_at,
                           observed_sha256,observed_size,authorize,ip=None,device=None):
    cid=int(case_before['id']);did=int(document_before['id'])
    def owned(view):
        current_actor=authorize(view)
        case=_one(view,'SELECT * FROM visa_cases WHERE id=?',(cid,))
        document=_one(view,'SELECT * FROM student_documents WHERE id=?',(did,))
        if (not case or not document or case['archived_at'] or not case['student_id']
                or document['student_id']!=case['student_id'] or not document['is_active']
                or str(document.get('document_status') or '').lower() in {'deleted','archived','voided','inactive'}
                or document.get('voided_at')):raise ValueError('案件或材料的归属、有效状态已变化，请刷新后重试。')
        if not view.execute('SELECT 1 FROM students WHERE student_id=? AND deleted_at IS NULL',(case['student_id'],)).fetchone():
            raise ValueError('学生已不存在或已删除。')
        if case.get('application_cycle_id') is not None:
            cycle=view.execute('SELECT student_id FROM application_cycles WHERE id=?',(case['application_cycle_id'],)).fetchone()
            if not cycle or str(cycle[0])!=str(case['student_id']):raise ValueError('案件与申请周期的学生归属不匹配。')
        if case!=case_before or document!=document_before:raise ValueError('案件或材料已更新，请刷新后重试。')
        return case,document,current_actor
    def capture(view):
        case,document,current_actor=owned(view)
        columns={row[1] for row in view.execute('PRAGMA table_info(visa_case_document_links)')}
        document_column='coalesce(student_document_id,document_id)' if {'student_document_id','document_id'}<=columns else (
            'student_document_id' if 'student_document_id' in columns else 'document_id')
        rows=tuple(tuple(row) for row in view.execute('SELECT * FROM visa_case_document_links WHERE visa_case_id=? AND '
            +document_column+'=? ORDER BY id',(cid,did)))
        return case,document,rows
    def write(view):
        case,document,current_actor=owned(view)
        columns={row[1] for row in view.execute('PRAGMA table_info(visa_case_document_links)')}
        # Two existing schemas use different required keys. Populate both
        # retained aliases where present; never omit a required document_id.
        values={'visa_case_id':cid,'student_document_id':did,'document_id':did,'document_role':role,
            'link_purpose':role,'used_in_this_case':1,'status':'Linked','notes':notes,
            'created_by':current_actor['id'],'created_at':occurred_at,'updated_at':occurred_at}
        values={key:value for key,value in values.items() if key in columns}
        identifier=int(view.execute('INSERT INTO visa_case_document_links('+','.join(values)+') VALUES('
            +','.join('?' for _ in values)+')',tuple(values.values())).lastrowid)
        with database.business_scope(view):
            database.audit(current_actor,'visa_existing_document_linked','visa_management',identifier,
                new_value=json.dumps({'visa_case_id':cid,'student_document_id':did,'document_role':role,
                    'source_sha256':observed_sha256,'source_size':observed_size},ensure_ascii=False),
                student_id=case['student_id'],ip=ip,device=device)
        return identifier
    def describe(view,result,before,after):
        return Mutation('visa.document_linked',Subject('visa_case',str(cid)),
            (*case_scopes(view,after[0]),Subject('document',str(did))),case_scopes(view,before[0]))
    return run_domain_write(conn,event_type='visa.document_linked',actor_kind='user',actor_ref=str(actor['id']),
        capture=capture,operation=write,describe=describe)
