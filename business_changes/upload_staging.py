"""Prepare isolated pending-upload bytes and atomically register their manifest."""
import json
import mimetypes
from pathlib import Path
import shutil
import tempfile
import zipfile

from .files import persist_verified_file
from .contracts import BusinessPermissionError
from .upload_review import start_review


def create_pending_review(g,student_id,module,uploads,user,form,*,permission_policy):
    database=g['db']
    state=getattr(database,'_request_state',lambda:None)()
    current=(state.get('business_connection') or state.get('connection')) if state else None
    if current is not None and current.in_transaction:
        raise ValueError('请在原业务事务结束后准备新的上传审核。')
    policies={'upload','workflow_save','visa_record','payment_create','payment_upload','file_manager_upload'}
    if (module not in g['MODULE_REVIEW_CONFIG'] or permission_policy not in policies
            or permission_policy=='workflow_save' and module not in {'Offer','COE + OSHC'}
            or permission_policy=='visa_record' and module!='Visa'
            or permission_policy=='payment_create' and module!='Payments'
            or permission_policy=='payment_upload' and module not in {'Payments','Homestay + Airport Pickup'}):
        raise ValueError('未知上传模块或上传授权方式')
    token=g['new_token'](18).replace('-','_');folder=g['upload_review_path'](token)
    folder.mkdir(parents=False,exist_ok=False)
    try:
        expanded=[]
        for upload in uploads:
            if not upload.filename:continue
            if module=='Applications' and upload.filename.lower().endswith('.zip'):
                # Keep even ZIP scratch bytes in this exclusively owned folder.
                with tempfile.NamedTemporaryFile(suffix='.zip',dir=folder) as temporary:
                    temporary.write(upload.data);temporary.flush()
                    members=g['nas'].safe_zip_members(temporary.name)
                    with zipfile.ZipFile(temporary.name) as archive:
                        for member in members:
                            filename=Path(member.filename).name
                            if filename.startswith('._') or not filename:continue
                            expanded.append(g['Upload']('files',filename,
                                mimetypes.guess_type(filename)[0] or 'application/octet-stream',archive.read(member)))
            else:expanded.append(upload)
        if not expanded:raise ValueError('请选择文件')
        files=[]
        for index,upload in enumerate(expanded):
            path=g['ensure_within'](folder/f'{index}.bin',folder)
            digest=persist_verified_file(path,upload.data,lambda path,digest:digest)
            files.append({'original_name':upload.filename,'staging_path':str(path),
                'content_type':upload.content_type or mimetypes.guess_type(upload.filename)[0] or 'application/octet-stream',
                'file_size':len(upload.data),'sha256':digest})
        stamp=g['now_iso']();context=dict(form or {})
        def fresh_actor(view):
            from .authorization import current_staff
            try:return current_staff(view,user['id'])
            except PermissionError as exc:raise BusinessPermissionError(str(exc)) from exc
        def authorize(view):
            actor=fresh_actor(view)
            with database.business_scope(view):
                if permission_policy=='workflow_save':allowed=g['can_workflow_module_save'](actor,module)
                elif permission_policy=='visa_record':
                    allowed=g['can_save_visa_record'](actor) and (g['can_module_action'](actor,'visa','upload') or g['allowed'](actor,'visas.manage'))
                elif permission_policy=='payment_create':
                    allowed=g['can_payment_action'](actor,'create') or g['can_payment_action'](actor,'upload')
                elif permission_policy=='payment_upload':allowed=g['can_payment_action'](actor,'upload')
                elif permission_policy=='file_manager_upload':allowed=g['can_file_manager_action'](actor,'upload')
                else:allowed=g['can_review_module_action'](actor,module,'upload')
            if not allowed:raise BusinessPermissionError('员工上传权限已变化，请重新核对。')
        def metadata_after(session_id,items):
            metadata={'token':token,'session_id':session_id,'student_id':str(student_id),'module':module,
                'created_by':user['id'],'created_at':stamp,'form':context,'business_sync_version':1,
                'files':[{'index':item['index'],'item_id':item['item_id'],
                    'blob':Path(item['staging_path']).name,'original_name':item['original_name'],
                    'content_type':item['content_type'],'size':item['file_size'],'sha256':item['sha256']} for item in items]}
            payload=json.dumps(metadata,ensure_ascii=False).encode('utf-8')
            persist_verified_file(g['ensure_within'](folder/'review.json',folder),payload,lambda path,digest:None)
        with database.transaction() as conn:
            actor=fresh_actor(conn)
            if module=='Visa':
                from .visa_review_context import freeze_context
                context=freeze_context(conn,student_id,context)
            start_review(conn,student_id=student_id,module_key=module,session_token=token,
                prepared_files=files,actor=actor,occurred_at=stamp,request_context=context,
                authorize=authorize,metadata_after=metadata_after)
        return token
    except BaseException as error:
        try:shutil.rmtree(folder)
        except OSError:raise RuntimeError('本次暂存上传清理未完成，请联系管理员核对。') from error
        raise
