"""Prepare complete contract PDFs, then publish one guarded archive fact."""
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from .commands import Command,Mutation,Outcome,execute_command,command_request_hash,digest,VersionConflict
from .contracts import Subject,BusinessPermissionError
from .contract_archive_files import prepare_pdf,insert_pdf,cleanup
from .files import persist_verified_file
from .registry import Registry
from .snapshots import read_scope
from .student_records import student_impacts
from .versions import version_of


def _one(conn,sql,params):
    cursor=conn.execute(sql,params);row=cursor.fetchone()
    return dict(zip((item[0] for item in cursor.description),row)) if row else None


def _bundle(conn,signature_id):
    signature=_one(conn,'SELECT * FROM contract_signatures WHERE id=?',(signature_id,))
    contract=_one(conn,'SELECT * FROM contracts WHERE id=?',(signature['contract_id'],)) if signature else None
    student=_one(conn,'SELECT * FROM students WHERE student_id=?',(signature['student_id'],)) if signature else None
    if (not signature or not contract or not student or signature['status']!='signed'
            or str(signature['student_id'])!=str(contract['student_id'])):
        raise ValueError('签署记录、合同和学生归属不匹配。')
    if contract['contract_status'] in {'Voided','Deleted','Revoked','Superseded'} or contract['deleted_at'] or contract['voided_at']:
        raise ValueError('合同已失效，不能继续归档。')
    paths=tuple(sorted({str(value) for value in (contract.get('contract_file_path'),contract.get('signed_file_path'),
        contract.get('final_signed_file_path'),signature.get('signed_pdf_path'),signature.get('confirmation_pdf_path')) if value}))
    documents=[]
    if paths:
        cursor=conn.execute('SELECT * FROM student_documents WHERE student_id=? AND file_path IN ('+','.join('?' for _ in paths)+') ORDER BY id',
            (student['student_id'],*paths))
        columns=[item[0] for item in cursor.description]
        documents=[{key:value for key,value in zip(columns,row) if key!='preview_path'} for row in cursor]
    return {'signature':signature,'contract':contract,'student':student,'documents':documents,
            'version':version_of(conn,Subject('contract',str(contract['id'])))}


def _actor(g,conn,user,contract):
    if user is None:return None
    from .authorization import current_staff
    try:current=current_staff(conn,user['id'])
    except PermissionError as exc:raise BusinessPermissionError(str(exc)) from exc
    if (not current or not g['can_create_contract_center'](current)
            or g['normalize_contract_type'](contract['contract_type'])=='TransferApplicationContract'
            and not g['can_manage_transfer_contract'](current)):
        raise BusinessPermissionError('合同归档权限已变化。')
    return current


def _command(manifest):
    return Command('contract_archive','system','contract_archive',manifest['resource_key'],
        {'input_hash':manifest['input_hash'],'manifest_hash':digest(manifest)})


def _cached(g,before,input_hash,user):
    signature=before['signature'];student=before['student'];contract=before['contract']
    with read_scope(g['db']) as conn:
        records=conn.execute("SELECT metadata_json FROM contract_signing_logs WHERE contract_id=? AND event_type='archive_snapshot_committed' ORDER BY id DESC",(contract['id'],)).fetchall()
    manifest=None
    for row in records:
        data=json.loads(row[0])
        if data.get('signature_id')==signature['id'] and data.get('input_hash')==input_hash:
            manifest=data;break
    if manifest is None:return None,False
    command=_command(manifest);scope=Subject('contract_archive',manifest['resource_key'])
    try:
        with read_scope(g['db']) as conn:
            _actor(g,conn,user,contract)
            receipt=conn.execute('SELECT request_hash FROM business_change_commands WHERE command_key=?',(command.key,)).fetchone()
            if not receipt or receipt[0]!=command_request_hash(command,{scope:0}):raise ValueError('归档回执或清单已变化。')
            if contract['contract_status']!='FullyExecuted' or signature['archive_status']!='archived':return None,True
            result=manifest['result']
            if contract['signed_file_path']!=result['signed_document_path'] or contract['final_signed_file_path']!=result['final_document_path']:
                return None,True
            for item in manifest['files']:
                document=_one(conn,'SELECT * FROM student_documents WHERE id=? AND student_id=?',(item['document_id'],student['student_id']))
                if (not document or not document['is_active'] or document['document_status']!='active' or document['voided_at']
                        or document['file_path']!=item['path'] or document['document_type']!=item['document_type']
                        or document['file_size']!=item['size']):raise ValueError('归档文件来源已变化。')
        for item in manifest['files']:
            path=g['nas'].require_student_file_readable(student,item['path'])
            if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=item['sha256']:raise ValueError('归档文件校验失败。')
        with read_scope(g['db']) as conn:
            current=_bundle(conn,signature['id']);_actor(g,conn,user,current['contract'])
            if digest(current)!=digest(before):raise VersionConflict('归档来源已变化，请刷新后重试。')
        return result,True
    except (OSError,ValueError):
        if user is None:raise
        # The explicit authorized staff rebuild creates a new immutable version;
        # it never restores or overwrites a revoked/missing old document.
        return None,True


def archive_contract(handler,signature_id,user,silent_archive,finalize):
    handler._business_archive_replayed=False
    g=handler.business_change_runtime();database=g['db']
    state=database._request_state();active=(state.get('business_connection') or state.get('connection')) if state else None
    if active is not None and active.in_transaction:raise ValueError('请在原业务事务结束后生成归档PDF。')
    with read_scope(database) as conn:
        before=_bundle(conn,int(signature_id));_actor(g,conn,user,before['contract'])
    contract=before['contract'];signature=before['signature'];student=before['student']
    g['require_transfer_contract_signable'](contract)
    if contract['contract_file_path']:g['nas'].require_student_file_readable(student,contract['contract_file_path'])
    source=handler.contract_signing_source_pdf(contract);g['require_transfer_pdf_file'](contract,source)
    g['ensure_contract_uid'](contract)
    with read_scope(database) as conn:
        before=_bundle(conn,int(signature_id));_actor(g,conn,user,before['contract'])
    contract=before['contract'];signature=before['signature'];student=before['student']
    source_bytes=Path(source).read_bytes();image_bytes=Path(signature['signature_image_path']).read_bytes()
    assets=tuple(Path(path) for path in g['active_contract_asset_paths']());asset_bytes=tuple(path.read_bytes() for path in assets)
    company=g['contract_company_payload']();placement=g['contract_stamp_placement']()
    contract_input={key:value for key,value in contract.items() if key not in
        {'contract_status','updated_at','signed_file_path','final_signed_file_path','signed_at','fully_executed_at'}}
    signature_input={key:value for key,value in signature.items() if key not in
        {'signed_pdf_path','confirmation_pdf_path','archive_status','archive_error','updated_at'}}
    student_input={key:value for key,value in student.items() if key not in {'current_status','updated_at'}}
    input_hash=digest([contract_input,signature_input,student_input,company,placement,
        *[hashlib.sha256(data).hexdigest() for data in (source_bytes,image_bytes,*asset_bytes)]])
    cached,previous=_cached(g,before,input_hash,user)
    if cached is not None:
        handler._business_archive_replayed=True
        return cached
    resource_key=uuid.uuid4().hex if previous else input_hash
    attempt=g['CONTRACT_SIGNING_DIR']/str(contract['id'])/'archive'/('attempt_'+uuid.uuid4().hex)
    attempt.mkdir(parents=True,exist_ok=False);owned=[];committed=False
    try:
        copies=[]
        for name,data in zip(('source.pdf','parent.png','company.png','stamp.png'),(source_bytes,image_bytes,*asset_bytes)):
            path=attempt/name;persist_verified_file(path,data,lambda path,digest:None);copies.append(path)
        snapshot=g['snapshot_payload'](contract['standalone_data_json'])
        relationship=(g['contract_display_binding_value'](snapshot.get('display_bindings'),'guardian_relationship')
            or (snapshot.get('signer') or {}).get('relationship') or snapshot.get('parent_signer_relationship')
            or (snapshot.get('contract_instance') or {}).get('parent_signer_relationship') or '父母/监护人')
        confirmation=attempt/'confirmation.pdf'
        g['render_contract_signature_confirmation_pdf'](confirmation,company=company,
            parent_signer_name=contract['parent_signer_name'] or '',parent_relationship=relationship,
            parent_contact=' / '.join(str(part) for part in (contract['parent_phone'] or '',contract['parent_email'] or '') if str(part).strip()),
            student_name=' '.join(part for part in (student['passport_first_name'],student['passport_last_name']) if part) or signature['student_id'],
            contract_title=contract['contract_title'] or '',contract_uid=contract['contract_uid'],
            wechat_identity=f"{signature['wechat_nickname'] or '已验证用户'} / {handler.school_plan_share_masked_openid(signature['wechat_openid'])}",
            signed_at_display=g['display_datetime'](signature['signed_at']),signed_ip=signature['signed_ip'] or '',
            user_agent=signature['user_agent'] or '',signature_image_path=copies[1])
        parent_pdf=attempt/'parent_signed.pdf'
        g['apply_parent_signature_to_contract_signature_section'](copies[0],copies[1],parent_pdf,
            parent_name=contract['parent_signer_name'] or signature['wechat_nickname'] or '已验证签署人',signed_at=signature['signed_at'])
        merged=attempt/'signed.pdf';g['merge_pdf_files']([parent_pdf,confirmation],merged);g['require_transfer_pdf_file'](contract,merged)
        final=attempt/'final.pdf';company_date=g['parent_signature_date_text'](signature['signed_at']) or g['local_company_signing_date']()
        g['apply_aaie_signature_and_stamp'](merged,final,company_date,asset_paths=(copies[2],copies[3]),placement_snapshot=placement)
        g['require_transfer_pdf_file'](contract,final)
        signed_dt=g['parse_local_datetime'](signature['signed_at'])
        if signed_dt is None:raise ValueError('原签署日期无效，不能生成归档。')
        full_name=' '.join(str(part).strip() for part in (student['passport_first_name'],student['passport_last_name']) if str(part or '').strip()) or student['student_id']
        specs=((merged,'SignedContract',f"{full_name}_signed_contract_{signed_dt.strftime('%d%m%Y')}.pdf",'contract_signed_pdf_archived'),
            (confirmation,'ContractSignatureConfirmationPage',f"{g['school_plan_student_archive_prefix'](student)}_Contract_Signature_Confirmation_Page_{signed_dt.strftime('%Y%m%d')}_v1.pdf",'contract_signature_confirmation_page_archived'),
            (final,'FinalSignedStudyAbroadContract',g['final_contract_base_filename'](contract,student),'final_signed_contract_archived'))
        artifacts=[prepare_pdf(g,student,path.read_bytes(),kind,name,action,owned) for path,kind,name,action in specs]
        with database.transaction() as conn:
            current=_bundle(conn,int(signature_id));actor=_actor(g,conn,user,current['contract'])
            if digest(current)!=digest(before):raise VersionConflict('归档期间来源已变化，请刷新后重试。')
            # All source rows and original effects use the protected command boundary.
            from .commands import TransactionView
            guard=TransactionView(conn,'bc_archive_'+uuid.uuid4().hex)
            with database.business_scope(guard):
                stamp=g['now_iso']();docs=[insert_pdf(g,guard,student['student_id'],item,actor,stamp) for item in artifacts]
                result=finalize(guard,signature,contract,student,*docs,stamp,actor)
                if signature['link_id']:
                    link=_one(guard,'SELECT * FROM contract_signing_links WHERE id=?',(signature['link_id'],))
                    if (not link or link['contract_id']!=contract['id']
                            or str(link['student_id'] or '') not in {'',str(student['student_id'])}):
                        raise ValueError('签署链接与归档合同归属不匹配。')
                    if (not link['invalidated_at'] and link['status'] in {'active','signed'}
                            and (link['status']=='signed' or not g['is_expired'](link['expires_at']))):
                        handler.contract_signing_mark_link_signed(link['id'],signature['signed_at'],int(signature_id))
                after=_bundle(guard,int(signature_id))
                if (after['contract']['contract_status']!='FullyExecuted' or after['signature']['archive_status']!='archived'
                        or after['contract']['signed_file_path']!=docs[0]['path'] or after['contract']['final_signed_file_path']!=docs[2]['path']):
                    raise ValueError('归档结果与合同状态不匹配。')
                manifest={'resource_key':resource_key,'input_hash':input_hash,'signature_id':int(signature_id),
                    'contract_id':contract['id'],'student_id':student['student_id'],'result':result,
                    'files':[{'document_id':doc['document_id'],'path':doc['path'],'document_type':item['document_type'],
                        'sha256':item['sha256'],'size':item['size']} for doc,item in zip(docs,artifacts)]}
                guard.execute('''INSERT INTO contract_signing_logs(contract_id,link_id,event_at,event_type,metadata_json)
                    VALUES(?,?,?,'archive_snapshot_committed',?)''',(contract['id'],signature['link_id'],stamp,json.dumps(manifest,ensure_ascii=False)))
                impacts=[Subject('contract',str(contract['id'])),Subject('contract_signature',str(signature_id)),
                    Subject('student',str(student['student_id'])),Subject('module','contracts'),*student_impacts(guard,student['student_id'])]
                impacts.extend(Subject('document',str(doc['document_id'])) for doc in docs)
                if signature['link_id']:impacts.append(Subject('contract_link',str(signature['link_id'])))
                old=tuple(Subject('document',str(doc['id'])) for doc in before['documents'])
                scope=Subject('contract_archive',resource_key)
                execute_command(conn,_command(manifest),registry=Registry([]),authorize=lambda view:None,
                    expected_versions={scope:0},occurred_at=stamp,operation=lambda view:Outcome(
                        {'record_id':int(signature_id),'contract_id':contract['id'],'document_id':docs[2]['document_id'],'state':'archived'},
                        (Mutation('contract.archive_completed',scope,tuple(impacts),previous_subjects=old),)))
        committed=True
        return result
    finally:
        try:
            try:cleanup(owned,committed=committed)
            except Exception as error:
                if not committed:raise
                try:g['append_service_log']('parent','archive_cache_cleanup_failed',str(error),contract_id=contract['id'])
                except Exception:pass
        finally:
            try:shutil.rmtree(attempt)
            except OSError as error:
                if not committed:raise
                try:g['append_service_log']('parent','archive_scratch_cleanup_failed',str(error),contract_id=contract['id'])
                except Exception:pass
