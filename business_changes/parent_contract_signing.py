"""Persist a verified parent's completed step before fallible PDF archiving."""
import json
import re

from .commands import Command,Mutation,Outcome,execute_command
from .contracts import Subject
from .registry import Registry
from .runtime import enabled
from .student_records import student_impacts
from .versions import version_of
from .domain import run_domain_write


def _owned(conn,contract_id,link_id,student_id):
    contract=conn.execute('SELECT student_id,contract_status,deleted_at,voided_at FROM contracts WHERE id=?',(contract_id,)).fetchone()
    link=conn.execute('SELECT contract_id,student_id FROM contract_signing_links WHERE id=?',(link_id,)).fetchone()
    if (contract is None or link is None or int(link[0])!=contract_id
            or str(contract[0] or '')!=student_id or str(link[1] or '') not in {'',student_id}):
        raise ValueError('签署链接、合同与学生归属不匹配。')
    if contract[1] in {'Voided','Deleted','Revoked','Superseded'} or contract[2] or contract[3]:
        raise ValueError('合同已失效，不能保存签署。')
    from student_lifecycle.transfer_state import pending_transfer_was_withdrawn
    if pending_transfer_was_withdrawn(conn, contract_id):
        raise ValueError('该轮转学已停止办理，不能保存签署。')
    return contract


def record_parent_signature(conn,*,contract_id,link_id,student_id,actor_ref,image_sha256,
                            signature_path,occurred_at,authorize,operation):
    if not enabled(conn):raise ValueError('家长签署尚未启用共用命令。')
    if any(type(value) is not int or value<1 for value in (contract_id,link_id)):
        raise ValueError('合同或签署链接编号无效。')
    if any(not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{64}',value)
           for value in (actor_ref,image_sha256)):
        raise ValueError('签署身份或图片校验值无效。')
    student_id=str(student_id or '');source=Subject('contract',str(contract_id))
    command=Command('contract_parent_sign','parent',actor_ref,str(link_id),
        {'contract_id':contract_id,'link_id':link_id,'student_id':student_id,'image_sha256':image_sha256})
    receipt=conn.execute('SELECT 1 FROM business_change_commands WHERE command_key=?',(command.key,)).fetchone()
    if receipt:
        events=conn.execute("""SELECT source_version FROM business_change_events WHERE command_key=?
            AND event_type='contract.parent_signed' AND source_kind='contract' AND source_id=?""",
            (command.key,str(contract_id))).fetchall()
        if len(events)!=1:raise ValueError('原签署回执不完整，请联系顾问核对。')
        expected=int(events[0][0])-1
    else:expected=version_of(conn,source)
    def check(view):
        authorize(view)
        _owned(view,contract_id,link_id,student_id)
        if receipt and view.execute("""SELECT 1 FROM contract_signatures WHERE contract_id=? AND link_id=?
            AND coalesce(student_id,'')=? AND status='signed'""",(contract_id,link_id,student_id)).fetchone() is None:
            raise ValueError('原签署记录不存在，请联系顾问核对。')
    def persist(view):
        if view.execute("SELECT 1 FROM contract_signatures WHERE link_id=? AND status='signed'",(link_id,)).fetchone():
            raise ValueError('该链接已经签署，不能重复保存。')
        signature_id=operation(view)
        if type(signature_id) is not int or signature_id<1:raise ValueError('签署保存未返回对应记录。')
        contract=_owned(view,contract_id,link_id,student_id)
        row=view.execute('''SELECT student_id,signature_image_path,status,archive_status FROM contract_signatures
            WHERE id=? AND contract_id=? AND link_id=?''',(signature_id,contract_id,link_id)).fetchone()
        if (row is None or str(row[0] or '')!=student_id or str(row[1])!=str(signature_path)
                or row[2]!='signed' or row[3]!='pending' or contract[1]!='ParentSigned'):
            raise ValueError('家长签署来源或状态不匹配。')
        impacts=[Subject('contract_signature',str(signature_id)),Subject('contract_link',str(link_id)),Subject('module','contracts')]
        if student_id:impacts.extend((Subject('student',student_id),*student_impacts(view,student_id)))
        return Outcome({'record_id':signature_id,'contract_id':contract_id},
            (Mutation('contract.parent_signed',source,tuple(impacts)),))
    return execute_command(conn,command,registry=Registry([]),authorize=check,operation=persist,
        expected_versions={source:expected},occurred_at=occurred_at)


def record_archive_failure(conn,*,signature_id,error_message,actor_kind,actor_ref,occurred_at,authorize):
    row=conn.execute('''SELECT s.contract_id,s.student_id,s.link_id,c.student_id FROM contract_signatures s
        JOIN contracts c ON c.id=s.contract_id WHERE s.id=? AND s.status='signed' ''',(int(signature_id),)).fetchone()
    if not row or str(row[1] or '')!=str(row[3] or ''):raise ValueError('归档失败记录与签署归属不匹配。')
    contract_id,student_id,link_id=row[0],str(row[1] or ''),row[2]
    def capture(view):
        return (tuple(view.execute('SELECT archive_status,archive_error FROM contract_signatures WHERE id=?',(signature_id,)).fetchone()),
            tuple(tuple(record) for record in view.execute("SELECT id FROM contract_signing_logs WHERE contract_id=? AND event_type='archive_attempt_failed' ORDER BY id",(contract_id,))))
    def operation(view):
        authorize(view)
        view.execute("""UPDATE contract_signatures SET archive_status='failed',archive_error=?,updated_at=?
            WHERE id=? AND archive_status<>'archived'""",(str(error_message)[:2000],occurred_at,int(signature_id)))
        view.execute('''INSERT INTO contract_signing_logs(contract_id,link_id,event_at,event_type,metadata_json)
            VALUES(?,?,?,'archive_attempt_failed',?)''',(contract_id,link_id,occurred_at,
                json.dumps({'signature_id':int(signature_id),'issue_code':'contract_archive_failed','detail':str(error_message)[:2000]},ensure_ascii=False)))
    return run_domain_write(conn,event_type='contract.archive_failed',actor_kind=actor_kind,actor_ref=actor_ref,
        capture=capture,operation=operation,describe=lambda *args:Mutation('contract.archive_failed',
            Subject('contract_signature',str(signature_id)),
            (Subject('contract',str(contract_id)),Subject('module','contracts'),
             *((Subject('student',student_id),*student_impacts(conn,student_id)) if student_id else ()))))
