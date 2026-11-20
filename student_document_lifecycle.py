"""Reversible withdrawal for unused application source files, never business facts.

The caller owns the transaction and supplies a fresh module-permission check.
No file operations, schema changes, global database, or commits occur here.
"""
from datetime import datetime
import json
from business_changes.commands import Mutation
from business_changes.contracts import Subject
from business_changes.domain import run_domain_write
from business_changes.runtime import enabled

SOURCE_WITHDRAWAL_MARKER = '[source-material-withdrawal] '
SOURCE_DOCUMENT_TYPES = frozenset({'Passport','StudentPassport','FatherPassport','MotherPassport',
    'GuardianPassport','BirthCertificate','Transcripts','Vaccination','AEAS','IDAT',
    'IELTS','TOEFL','Award','Others'})
_PROVENANCE = {'student_documents','upload_review_items','audit_logs','file_action_markers'}


def _quote(identifier):
    return '"' + identifier.replace('"','""') + '"'


def _columns(conn, table):
    return {str(r[1]) for r in conn.execute('PRAGMA table_info('+_quote(table)+')')}


def _owned(conn, student_id, document_id):
    cursor=conn.execute('SELECT * FROM student_documents WHERE id=? AND student_id=?',
                        (document_id,student_id))
    row=cursor.fetchone()
    if row is None:raise ValueError('材料不存在或不属于当前学生。')
    return dict(zip((c[0] for c in cursor.description),row))


def _validate_unused(conn, document):
    if document['folder_category'] != 'Applications' or document['document_type'] not in SOURCE_DOCUMENT_TYPES:
        raise ValueError('该材料属于正式业务证据，请到对应 Offer、CoE、付款、签证或合同专门页面处理。')
    if 'materials_checked' in _columns(conn,'workflow_status'):
        if conn.execute('SELECT 1 FROM workflow_status WHERE student_id=? AND materials_checked=1',
                        (document['student_id'],)).fetchone():
            raise ValueError('申请材料已完成正式审核，请先在申请材料专门页面复核，不能仅隐藏源材料。')
    # Detect actual adoption, including legacy file-path links without foreign keys.
    # Audit/import provenance is retained and does not make the file business evidence.
    tables=[str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        if table in _PROVENANCE:continue
        columns=_columns(conn,table)
        references={str(r[3]) for r in conn.execute('PRAGMA foreign_key_list('+_quote(table)+')')
                    if r[2]=='student_documents'}
        references |= columns & {'student_document_id'}
        for column in references:
            if conn.execute('SELECT 1 FROM '+_quote(table)+' WHERE '+_quote(column)+'=? LIMIT 1',
                            (document['id'],)).fetchone():
                raise ValueError('该材料已被正式业务采用，请到对应专门页面撤销或恢复业务记录。')
        if document.get('file_path'):
            for column in columns:
                if column=='file_path' or column.endswith('_file_path'):
                    if conn.execute('SELECT 1 FROM '+_quote(table)+' WHERE '+_quote(column)+'=? LIMIT 1',
                                    (document['file_path'],)).fetchone():
                        raise ValueError('该材料文件已关联正式业务，请到对应专门页面处理。')


def set_document_withdrawn(conn, *, student_id, document_id, withdrawn, reason,
                           actor_user_id, occurred_at, authorize):
    """Withdraw/restore an unused Applications source; authorization is mandatory.

    ``authorize(view, document)`` must recheck current user module authority.
    Only withdrawals created here may be restored here. A linked business fact
    always requires its own domain service, including when restoring a source.
    """
    if type(withdrawn) is not bool:raise ValueError('材料撤销状态无效。')
    if not callable(authorize):raise PermissionError('必须重新检查材料管理权限。')
    if not conn.in_transaction:raise ValueError('材料操作需要调用方事务。')
    if not enabled(conn):raise ValueError('材料变更同步未启用，不能执行撤销或恢复。')
    if datetime.fromisoformat(occurred_at).tzinfo is None:raise ValueError('操作时间必须包含时区。')
    student_id=str(student_id);document_id=int(document_id)
    actor_user_id=int(actor_user_id)
    reason=str(reason or '').strip()
    if not reason:raise ValueError('请填写撤销或恢复原因。')
    event='document.source_withdrawn' if withdrawn else 'document.source_restored'
    def capture(view):return _owned(view,student_id,document_id)
    def operation(view):
        document=_owned(view,student_id,document_id)
        actor=view.execute('SELECT 1 FROM users WHERE id=? AND is_active=1',(actor_user_id,)).fetchone()
        if actor is None:raise PermissionError('当前账号不存在或已停用。')
        if authorize(view,document) is False:raise PermissionError('没有材料管理权限。')
        required={'is_active','document_status','voided_at','voided_by','void_reason'}
        if not required <= document.keys():raise ValueError('材料生命周期字段缺失，当前不能操作。')
        _validate_unused(view,document)
        ours=(document['is_active']==0 and document['document_status']=='voided'
              and str(document.get('void_reason') or '').startswith(SOURCE_WITHDRAWAL_MARKER))
        active=(document['is_active']==1 and document['document_status']=='active' and not document['voided_at'])
        if withdrawn and ours or not withdrawn and active:return False
        if withdrawn and not active:raise ValueError('该材料已由其他业务作废，请到对应专门页面处理。')
        if not withdrawn and not ours:raise ValueError('仅可恢复通过材料撤销入口撤销的文件。')
        view.execute('''UPDATE student_documents SET is_active=?,document_status=?,
            voided_at=?,voided_by=?,void_reason=? WHERE id=? AND student_id=?''',
            (0 if withdrawn else 1,'voided' if withdrawn else 'active',
             occurred_at if withdrawn else None,actor_user_id if withdrawn else None,
             SOURCE_WITHDRAWAL_MARKER+reason if withdrawn else None,document_id,student_id))
        view.execute("""INSERT INTO audit_logs(user_id,action_type,module_name,module_key,
            record_id,student_id,old_value,new_value,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (actor_user_id,event,'Applications','applications',str(document_id),student_id,
             json.dumps({k:document[k] for k in ('is_active','document_status','voided_at','voided_by','void_reason')},ensure_ascii=False),
             json.dumps({'withdrawn':withdrawn,'reason':reason},ensure_ascii=False),occurred_at))
        return True
    def describe(view,result,before,after):
        scopes=[Subject('student',student_id),Subject('module','applications'),
                Subject('dashboard','workflow'),Subject('dashboard','overview')]
        cid=after.get('application_cycle_id')
        cycle_columns=_columns(view,'application_cycles')
        if cycle_columns:
            if cid is not None:
                owner=view.execute('SELECT student_id FROM application_cycles WHERE id=?',(cid,)).fetchone()
                if not owner or str(owner[0])!=student_id:raise ValueError('材料申请周期归属异常。')
            cursor=view.execute("SELECT * FROM application_cycles WHERE student_id=? AND (status='active' OR id=?)",
                                (student_id,cid))
            names=[c[0] for c in cursor.description]
            for row in cursor:
                cycle=dict(zip(names,row))
                scopes.append(Subject('application_cycle',str(cycle['id'])))
                if cycle.get('source_student_case_id') is not None:
                    scopes.append(Subject('student_case',str(cycle['source_student_case_id'])))
        return Mutation(event,Subject('document',str(document_id)),tuple(scopes))
    return run_domain_write(conn,event_type=event,actor_kind='user',actor_ref=str(actor_user_id),
        capture=capture,operation=operation,describe=describe)
