"""Verified manual-record file replacement using the original source command."""
import hashlib
import os
from pathlib import Path
import uuid

from .files import persist_verified_file
from .snapshots import read_scope


def _upload_seal(upload,document_type,context):
    return {'sha256':hashlib.sha256(upload.data).hexdigest(),'size':len(upload.data),
          'original_name':upload.filename,'content_type':upload.content_type,
          'document_type':document_type,'context':context}


def lookup_visa_upload(handler,student_id,record_id,upload,document_type,*,context):
    def forbidden(view):raise AssertionError('receipt lookup executed a business callback')
    with read_scope(handler.business_change_runtime()['db']) as conn:
        return handler.business_change_save_manual_record('Visa',str(student_id),
            None if record_id is None else int(record_id),forbidden,caller_connection=conn,
            payload_extra=_upload_seal(upload,document_type,context))


def save_visa_upload(handler,student,record_id,upload,document_type,*,context,prepare,operation):
    g=handler.business_change_runtime();database=g['db'];student_id=str(student['student_id'])
    seal=_upload_seal(upload,document_type,context)
    def command(callback,conn=None):
        return handler.business_change_save_manual_record('Visa',student_id,None if record_id is None else int(record_id),callback,
            caller_connection=conn,payload_extra=seal)
    def unprepared(view):raise AssertionError('receipt lookup executed a business callback')
    with read_scope(database) as conn:
        receipt=command(unprepared,conn)
        if receipt is not None:return receipt
    # No attachment/preview/recognition work is repeated for an existing receipt.
    if student['nas_status']!='ready':student=g['ensure_student_storage'](student)
    prepared=prepare()
    destination,version=g['next_version_path'](g['nas'].category_folder(student,'VISA'),upload.filename)
    scratch=[];promoted=[]
    def cleanup(paths):
        failed=[]
        for path in reversed(paths):
            try:path.unlink(missing_ok=True)
            except OSError:failed.append(str(path))
        if failed:raise RuntimeError('材料缓存清理失败：'+'；'.join(failed))
    def save(path,digest):
        if digest!=seal['sha256']:raise ValueError('替换文件校验值已变化，请重新上传。')
        key='prepared_'+uuid.uuid4().hex
        for size in ('thumb','large'):
            target=g['preview_target'](key,size);scratch.append(target)
            rendered=Path(g['make_preview'](key,path.name,upload.data,upload.content_type,size=size))
            if rendered.resolve()!=target.resolve() or not target.is_file():raise ValueError('材料预览未保存到预期位置。')
        def persist(view):
            did=int(view.execute('''INSERT INTO student_documents(student_id,folder_category,document_type,
                original_name,stored_name,file_path,version,file_size,uploaded_by,uploaded_at,is_active,document_status)
                VALUES(?,'VISA',?,?,?,?,?,?,?,?,1,'active')''',
                (student_id,document_type,upload.filename,path.name,str(path),version,len(upload.data),
                 handler.user['id'],g['now_iso']())).lastrowid)
            for size,source in zip(('thumb','large'),scratch):
                target=g['preview_target'](did,size);os.link(source,target);promoted.append(target)
            view.execute('UPDATE student_documents SET preview_path=? WHERE id=?',
                (str(g['preview_target'](did,'thumb')),did))
            g['db'].audit(handler.user,'upload','VISA',did,str(path),new_value=document_type)
            return operation(view,did,path,prepared)
        return command(persist)
    try:result=persist_verified_file(destination,upload.data,save)
    except BaseException:
        cleanup([*scratch,*promoted]);raise
    # A cleanup error after commit must never remove the referenced original.
    cleanup([*scratch,*([destination] if result.replayed else [])])
    return result
