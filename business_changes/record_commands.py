"""Explicit owned manual-record commands; no SQL interception or IO callbacks.

Adapters validate business fields before entering this command. Source writes,
retained audit and business effects must use its guarded caller transaction.
"""
import json
import re
import uuid

from .commands import (Command,Mutation,Outcome,execute_command,TransactionView,
    CommandResult,CommandConflict,VersionConflict,command_request_hash)
from .contracts import Subject
from .registry import Registry
from .runtime import enabled
from .student_records import student_impacts
from .versions import version_of
from .schema import require_schema


RECORDS={'Offer':('offers','offer','offers'),
         'COE + OSHC':('coe_oshc','coe','coe_oshc'),
         'Payments':('payments','payment','payments'),
         'Visa':('visas','visa_record','visa_cases'),
         'Contracts':('contracts','contract','contracts')}


def _record(view,table,record_id,student_id):
    cursor=view.execute('SELECT * FROM '+table+" WHERE id=? AND coalesce(student_id,'')=?",(record_id,student_id))
    row=cursor.fetchone()
    if row is None:raise ValueError('业务记录不属于当前学生或已不存在')
    return dict(zip((column[0] for column in cursor.description),row))


def _cycle_scope(view,row,student_id):
    cycle=row.get('application_cycle_id')
    if cycle is None:return ()
    cursor=view.execute('SELECT * FROM application_cycles WHERE id=? AND student_id=?',(cycle,student_id))
    found=cursor.fetchone()
    if found is None:raise ValueError('业务记录与申请周期的学生归属不匹配')
    cycle_row=dict(zip((column[0] for column in cursor.description),found))
    result=[Subject('application_cycle',str(cycle))]
    if cycle_row.get('source_student_case_id') is not None:
        result.append(Subject('student_case',str(cycle_row['source_student_case_id'])))
    return tuple(result)


def _document_scopes(view,row,student_id):
    paths=tuple(sorted({str(row.get(key)) for key in ('file_path','offer_file_path','signed_offer_file_path',
        'contract_file_path','signed_file_path','final_signed_file_path') if row.get(key)}))
    if not paths or not view.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='student_documents'").fetchone():return ()
    return tuple(Subject('document',str(record[0])) for record in view.execute(
        'SELECT id FROM student_documents WHERE student_id=? AND file_path IN ('+','.join('?' for _ in paths)+') ORDER BY id',
        (student_id,*paths)))


def save_record(conn, *, module_key, student_id, record_id, actor, nonce,expected_version,
                payload, occurred_at, authorize, operation,lookup_only=False):
    if not enabled(conn) or module_key not in RECORDS:raise ValueError('当前业务写入尚未启用共用命令')
    if not isinstance(nonce,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',nonce):
        raise ValueError('缺少有效保存凭据，请刷新后重试')
    if type(expected_version) is not int or expected_version<0:raise ValueError('业务版本无效，请刷新后重试')
    if record_id is not None and (type(record_id) is not int or record_id<1):raise ValueError('业务记录编号无效')
    if module_key=='Contracts' and record_id is None:raise ValueError('合同创建保留原有生成流程。')
    table,kind,module=RECORDS[module_key];student_id=str(student_id or '')
    source=Subject(kind,str(record_id)) if record_id is not None else Subject('student',student_id)
    command=Command(kind+'_manual_save','user',str(actor['id']),nonce,
        {'student_id':student_id,'record_id':record_id,'fields':payload})
    def check(view):
        authorize(view)
        if (student_id or module_key!='Contracts') and view.execute('SELECT 1 FROM students WHERE student_id=? AND deleted_at IS NULL',(student_id,)).fetchone() is None:
            raise ValueError('学生不存在或已删除')
        if record_id is not None:_record(view,table,record_id,student_id)
    if lookup_only:
        if not conn.in_transaction or conn.execute('PRAGMA query_only').fetchone()[0]!=1:
            raise ValueError('receipt_lookup_requires_readonly_snapshot')
        require_schema(conn)
        check(TransactionView(conn,'bc_lookup_'+uuid.uuid4().hex))
        row=conn.execute('SELECT request_hash,result_json FROM business_change_commands WHERE command_key=?',(command.key,)).fetchone()
        if row:
            if row[0]!=command_request_hash(command,{source:expected_version}):raise CommandConflict('idempotency_key_content_conflict')
            sequences=tuple(r[0] for r in conn.execute('SELECT seq FROM business_change_events WHERE command_key=? ORDER BY seq',(command.key,)))
            return CommandResult(json.loads(row[1]),sequences,True)
        if version_of(conn,source)!=expected_version:raise VersionConflict('source_version_changed')
        return None
    def write(view):
        previous=_record(view,table,record_id,student_id) if record_id is not None else None
        old_scopes=(*_cycle_scope(view,previous,student_id),*_document_scopes(view,previous,student_id)) if previous else ()
        result=operation(view)
        if type(result) is not int or result<1 or record_id is not None and result!=record_id:
            raise ValueError('业务写入必须返回对应的正式记录编号')
        current=_record(view,table,result,student_id)
        impacts=(*( (Subject('student',student_id),) if student_id else ()),Subject(kind,str(result)),Subject('module',module),
            *_cycle_scope(view,current,student_id),*_document_scopes(view,current,student_id),
            *(student_impacts(view,student_id) if student_id else ()))
        # Creation belongs to the owning Student command until the new record
        # exists. Nested canonical classification can already revise that record
        # and Student; retain the actual next source revision in the receipt.
        return Outcome({'record_id':result,'version':version_of(view,source)+1},
            (Mutation(kind+'.created' if record_id is None else kind+'.updated',source,
                impacts,previous_subjects=old_scopes),))
    return execute_command(conn,command,registry=Registry([]),authorize=check,operation=write,
        expected_versions={source:expected_version},occurred_at=occurred_at)
