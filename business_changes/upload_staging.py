"""Prepare isolated pending-upload bytes and atomically register their manifest."""
import json
import hashlib
import mimetypes
from pathlib import Path
import shutil
import tempfile
import zipfile

from .files import persist_verified_file
from .contracts import BusinessPermissionError
from .upload_review import start_review, verify_review_manifest


def reusable_pending_token(conn, g, *, student_id, module, actor_id, files, context):
    """Reuse only a complete, sealed batch with identical bytes and intent.

    Run under the caller's immediate transaction so two upload requests cannot
    both register a new batch. Legacy/unreadable manifests remain pending and
    are never inferred to be approved or deleted by this check.
    """
    signature=lambda items: [(i['original_name'],i['content_type'],i['file_size'],i['sha256']) for i in items]
    # CSRF changes with a new login; it is transport authorization, not the
    # material's business intent. Keep the original context in the sealed
    # manifest and its receipt, and use that unchanged context for validation.
    business_context=lambda form: {key:value for key,value in form.items() if key!='csrf'}
    expected=signature(files)
    candidates=conn.execute('''SELECT s.session_token FROM upload_sessions s
        WHERE s.student_id=? AND s.module_key=? AND s.uploaded_by=?
        AND EXISTS(SELECT 1 FROM upload_review_items i WHERE i.upload_session_id=s.id AND i.status='NeedsReview')
        ORDER BY s.id DESC''',(str(student_id),module,actor_id)).fetchall()
    for (token,) in candidates:
        try:
            folder=g['upload_review_path'](token)
            metadata=json.loads((folder/'review.json').read_text(encoding='utf-8'))
            if (metadata.get('business_sync_version')!=1
                    or not isinstance(metadata.get('form'),dict)
                    or business_context(metadata['form'])!=business_context(context)):continue
            prepared=[{'original_name':item['original_name'],
                'staging_path':str(g['ensure_within'](folder/item['blob'],folder)),
                'content_type':item['content_type'],'file_size':item['size'],'sha256':item['sha256']}
                for item in metadata['files']]
            if signature(prepared)!=expected:continue
            if not verify_review_manifest(conn,metadata=metadata,prepared_files=prepared):continue
            if any(not Path(item['staging_path']).is_file()
                   or hashlib.sha256(Path(item['staging_path']).read_bytes()).hexdigest()!=item['sha256']
                   for item in prepared):continue
            return token
        except (OSError,ValueError,KeyError,TypeError):
            continue
    return None


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
            retained_token=reusable_pending_token(conn,g,student_id=student_id,module=module,
                actor_id=actor['id'],files=files,context=context)
            if retained_token:
                from .commands import TransactionView
                changes=conn.total_changes
                authorize(TransactionView(conn,'pending_reuse_authorization'))
                if conn.total_changes!=changes:
                    raise ValueError('上传审核权限检查不得修改业务记录')
                # Only our brand-new scratch folder is removed. The retained
                # pending manifest, bytes and review records remain untouched.
                shutil.rmtree(folder)
                return retained_token
            start_review(conn,student_id=student_id,module_key=module,session_token=token,
                prepared_files=files,actor=actor,occurred_at=stamp,request_context=context,
                authorize=authorize,metadata_after=metadata_after)
        return token
    except BaseException as error:
        try:shutil.rmtree(folder)
        except OSError:raise RuntimeError('本次暂存上传清理未完成，请联系管理员核对。') from error
        raise
