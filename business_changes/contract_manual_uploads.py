"""Explicit signed-PDF command; prepare bytes before the guarded metadata save."""
import json
from datetime import datetime

from .files import persist_verified_file
from .record_uploads import _upload_seal
from .snapshots import read_scope


def save_signed_contract(handler,contract,upload,*,student_entry=False):
    g=handler.business_change_runtime();database=g['db'];cid=int(contract['id'])
    student_id=contract['student_id'] or ''
    policy='contracts_generate' if student_entry else None
    seal=_upload_seal(upload,'SignedStudyAbroadContract',{'contract_id':cid,'entry':'student' if student_entry else 'center'})
    def command(callback,conn=None):
        return handler.business_change_save_manual_record('Contracts',student_id,cid,callback,
            caller_connection=conn,payload_extra=seal,authorization_policy=policy)
    def forbidden(view):raise AssertionError('receipt lookup executed source write')
    with read_scope(database) as conn:
        receipt=command(forbidden,conn)
        if receipt is not None:return receipt
    uid=contract['contract_uid'] or g['contract_uid_for_today']()
    student=database.one('SELECT * FROM students WHERE student_id=?',(student_id,)) if student_id else None
    if student_id and student is None:raise ValueError('关联学生不存在。')
    if student is not None and student['nas_status']!='ready':student=g['ensure_student_storage'](student)
    folder=g['forms_folder_for_student'](student) if student is not None else g['standalone_contract_folder'](uid,'signed')
    date=datetime.now().astimezone().strftime('%Y%m%d')
    target,version=g['next_version_path'](folder,f'SignedStudyAbroadContract_{date}_V1.pdf')
    def save(path,digest):
        if digest!=seal['sha256']:raise ValueError('签署文件校验值已变化。')
        def persist(view):
            fresh=database.one('SELECT * FROM contracts WHERE id=?',(cid,))
            if fresh is None or dict(fresh)!=dict(contract):raise ValueError('合同已更新，请刷新后重试。')
            document_id='';now=g['now_iso']()
            if student_id:
                document_id=database.execute('''INSERT INTO student_documents(student_id,folder_category,document_type,
                    original_name,stored_name,file_path,version,file_size,uploaded_by,uploaded_at,preview_path,is_active)
                    VALUES(?,'Forms','SignedStudyAbroadContract',?,?,?,?,?,?,?,'',1)''',
                    (student_id,upload.filename,path.name,str(path),version,len(upload.data),handler.user['id'],now))
            database.execute("UPDATE contracts SET contract_uid=?,signed_file_path=?,contract_status='ParentSigned',signed_at=?,updated_at=? WHERE id=?",
                (uid,str(path),now,now,cid))
            g['notify_recorded_contract_signature'](contract,now)
            if student_entry:
                database.audit(handler.user,'upload_signed','contracts',cid,file_path=str(path),new_value=str(document_id))
            else:
                database.audit(handler.user,'parent_signed_contract_uploaded','contracts',cid,file_path=str(path),
                    old_value=g['normalize_contract_status'](contract['contract_status']),
                    new_value=json.dumps({'new_status':'ParentSigned','document_id':document_id},ensure_ascii=False),
                    ip=handler.client_address[0],device=handler.headers.get('User-Agent'))
            return cid
        return command(persist)
    result=persist_verified_file(target,upload.data,save)
    if result.replayed:target.unlink(missing_ok=True)
    return result
