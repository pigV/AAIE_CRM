"""Shared atomic Upload Review lifecycle; file preparation is external."""
import json
import re

from .commands import Command, Mutation, Outcome, execute_command, _domain_owner, digest, command_request_hash
from .contracts import Subject
from .registry import Registry
from .runtime import enabled
from .student_records import student_impacts
from .versions import version_of
from .domain import run_domain_write


MODULES={
    'Applications':('Applications','applications'), 'Offer':('Offer','offers'),
    'COE + OSHC':('COE + OSHC','coe_oshc'), 'Visa':('VISA','visa_cases'),
    'Payments':('Payments','payments'), 'Forms':('Forms','contracts'),
    'Homestay + Airport Pickup':('Homestay + Airport Pickup','homestay_airport'),
}
BUSINESS_RECORDS={'Offer':('offers','offer'),'COE + OSHC':('coe_oshc','coe'),
                  'Visa':('visas','visa_record'),'Payments':('payments','payment'),'Forms':('contracts','contract')}


def start_review(conn, *, student_id,module_key,session_token,prepared_files,actor,occurred_at,
                 request_context,authorize,metadata_after):
    """Register already verified staging bytes; no material approval implied.

    Resource retry identity is the private upload token's digest. This does not
    deduplicate two separate HTTP uploads which each create a different token.
    The caller compensates its exclusively created files on any failure.
    """
    if not enabled(conn) or module_key not in MODULES:raise ValueError('材料审核模块未启用')
    if not isinstance(session_token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,200}',session_token):
        raise ValueError('上传审核身份无效')
    if not isinstance(prepared_files,(list,tuple)) or not prepared_files:raise ValueError('请选择文件')
    files=[]
    for item in prepared_files:
        if not isinstance(item,dict):raise ValueError('暂存文件校验信息无效')
        fields={key:item.get(key) for key in ('original_name','staging_path','content_type','file_size','sha256')}
        if (any(not isinstance(fields[key],str) or not fields[key] for key in ('original_name','staging_path','content_type'))
                or type(fields['file_size']) is not int or fields['file_size']<0
                or not isinstance(fields['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',fields['sha256'])):
            raise ValueError('暂存文件校验信息无效')
        files.append(fields)
    student_id=str(student_id);source=Subject('student',student_id)
    retained=conn.execute('SELECT student_id,module_key,uploaded_by FROM upload_sessions WHERE session_token=?',(session_token,)).fetchone()
    if retained and (str(retained[0])!=student_id or retained[1]!=module_key):raise ValueError('上传审核身份已被其他业务使用')
    command=Command('upload_review_started','user',str(retained[2] if retained else actor['id']),digest(session_token),
        {'student_id':student_id,'module_key':module_key,'files':files,'context':request_context})
    receipt=conn.execute('SELECT result_json FROM business_change_commands WHERE command_key=?',(command.key,)).fetchone()
    expected=version_of(conn,source)
    if receipt:
        version=json.loads(receipt[0]).get('version')
        if type(version) is not int or version<1:raise ValueError('上传审核凭据版本无效')
        expected=version-1
    def check(view):
        authorize(view)
        if view.execute('SELECT 1 FROM students WHERE student_id=? AND deleted_at IS NULL',(student_id,)).fetchone() is None:
            raise ValueError('学生不存在或已删除')
        if bool(retained)!=bool(receipt):raise ValueError('上传审核源记录与保存凭据不一致')
    def write(view):
        sid=int(view.execute('''INSERT INTO upload_sessions(session_token,student_id,module_key,uploaded_by,
            uploaded_at,status,total_files,notes) VALUES(?,?,?,?,?,'NeedsReview',?,'')''',
            (session_token,student_id,module_key,actor['id'],occurred_at,len(files))).lastrowid)
        items=[]
        def audit(action,record_id,path,details):
            view.execute('''INSERT INTO audit_logs(user_id,username,action_type,module_name,module_key,record_id,
                student_id,file_path,new_value,status,created_at) VALUES(?,?,?,'upload_review',?,?,?,?,?,'success',?)''',
                (actor['id'],actor.get('username',''),action,MODULES[module_key][1],str(record_id),student_id,path,
                 json.dumps(details,ensure_ascii=False),occurred_at))
        audit('upload_session_created',sid,None,{'student_id':student_id,'module':module_key,'total_files':len(files)})
        for index,item in enumerate(files):
            item_id=int(view.execute('''INSERT INTO upload_review_items(upload_session_id,student_id,module_key,
                original_file_name,staging_file_path,file_type,file_size,status,uploaded_by,uploaded_at)
                VALUES(?,?,?,?,?,?,?,'NeedsReview',?,?)''',
                (sid,student_id,module_key,item['original_name'],item['staging_path'],item['content_type'],
                 item['file_size'],actor['id'],occurred_at)).lastrowid)
            audit('file_uploaded_to_staging',item_id,item['staging_path'],{'session_id':sid,'original_file_name':item['original_name']})
            items.append(dict(item,index=index,item_id=item_id))
        # The reviewed caller writes only the small metadata manifest here.
        # It must not start another business command or modify source rows.
        changes=_domain_owner(conn).total_changes
        metadata_after(sid,items)
        if _domain_owner(conn).total_changes!=changes:raise ValueError('上传清单回调不得修改业务记录')
        impacts=(Subject('upload_session',str(sid)),Subject('module',MODULES[module_key][1]),
            *(Subject('upload_review_item',str(item['item_id'])) for item in items),*student_impacts(view,student_id))
        return Outcome({'id':sid,'student_id':student_id,'version':expected+1},
            (Mutation('materials.review_started',source,impacts),))
    return execute_command(conn,command,registry=Registry([]),authorize=check,operation=write,
        expected_versions={source:expected},occurred_at=occurred_at)


def verify_review_manifest(conn, *, metadata,prepared_files):
    """Verify new manifests against the original receipt without a write TX.

    Old pending uploads have no source receipt and remain explicitly legacy.
    Removing the marker from a new manifest cannot bypass its retained receipt.
    """
    def invalid():raise ValueError('暂存文件或审核清单已变化，请重新上传并核对。')
    if not isinstance(metadata,dict) or not isinstance(prepared_files,(list,tuple)):invalid()
    tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('upload_sessions','business_change_commands')")}
    sealed=metadata.get('business_sync_version') is not None
    if len(tables)!=2:
        if sealed:invalid()
        return False
    token=metadata.get('token')
    row=conn.execute('SELECT id,student_id,module_key,uploaded_by,uploaded_at FROM upload_sessions WHERE session_token=?',(token,)).fetchone()
    if row is None:
        if sealed:invalid()
        return False
    identity=Command('upload_review_started','user',str(row[3]),digest(token),{})
    receipt=conn.execute('SELECT request_hash,result_json FROM business_change_commands WHERE command_key=?',(identity.key,)).fetchone()
    if receipt is None:
        if sealed:invalid()
        return False
    result=json.loads(receipt[1]);version=result.get('version')
    if (metadata.get('business_sync_version')!=1 or metadata.get('session_id')!=row[0]
            or str(metadata.get('student_id'))!=str(row[1]) or metadata.get('module')!=row[2]
            or metadata.get('created_by')!=row[3] or metadata.get('created_at')!=row[4]
            or result.get('id')!=row[0] or type(version) is not int or version<1):invalid()
    items=conn.execute('SELECT id,original_file_name,staging_file_path,file_type,file_size FROM upload_review_items WHERE upload_session_id=? ORDER BY id',(row[0],)).fetchall()
    supplied=metadata.get('files')
    if not isinstance(supplied,list) or len(items)!=len(supplied) or len(items)!=len(prepared_files):invalid()
    for index,(item,meta,prepared) in enumerate(zip(items,supplied,prepared_files)):
        if not isinstance(meta,dict) or not isinstance(prepared,dict):invalid()
        if (type(meta.get('index')) is not int or meta['index']!=index
                or type(meta.get('item_id')) is not int or meta['item_id']!=item[0] or prepared.get('original_name')!=item[1]
                or prepared.get('staging_path')!=item[2] or prepared.get('content_type')!=item[3]
                or prepared.get('file_size')!=item[4]):invalid()
    intent=Command('upload_review_started','user',str(row[3]),digest(token),
        {'student_id':str(row[1]),'module_key':row[2],'files':prepared_files,'context':metadata.get('form') or {}})
    expected={Subject('student',str(row[1])):version-1}
    if command_request_hash(intent,expected)!=receipt[0]:invalid()
    return True


def _review(conn, item_id, student_id, module_key, *, allow_cancelled=False):
    cursor=conn.execute('''SELECT i.*,s.student_id AS session_student_id,s.module_key AS session_module
        FROM upload_review_items i JOIN upload_sessions s ON s.id=i.upload_session_id WHERE i.id=?''', (item_id,))
    row=cursor.fetchone()
    review=dict(zip((column[0] for column in cursor.description),row)) if row else {}
    if (str(review.get('student_id') or '') != student_id or review.get('module_key') != module_key
            or str(review.get('session_student_id') or '') != student_id or review.get('session_module') != module_key):
        raise ValueError('材料审核记录与学生或模块不匹配')
    if conn.execute('SELECT 1 FROM students WHERE student_id=? AND deleted_at IS NULL', (student_id,)).fetchone() is None:
        raise ValueError('学生不存在或已删除')
    if review['status'] not in ({'NeedsReview','Confirmed','Cancelled'} if allow_cancelled else {'NeedsReview','Confirmed'}):
        raise ValueError('材料审核记录已取消或状态已变化')
    return review


def _refresh_session(view,session_id):
    counts={r[0]:int(r[1]) for r in view.execute('SELECT status,count(*) FROM upload_review_items WHERE upload_session_id=? GROUP BY status',(session_id,))}
    total=sum(counts.values());confirmed=counts.get('Confirmed',0);cancelled=counts.get('Cancelled',0)
    status=('Confirmed' if confirmed and confirmed+cancelled==total
            else 'Cancelled' if total and cancelled==total else 'NeedsReview')
    view.execute('UPDATE upload_sessions SET status=?,total_files=?,confirmed_files=?,cancelled_files=? WHERE id=?',
        (status,total,confirmed,cancelled,session_id))


def update_review_state(conn, *, item_id, student_id, module_key, action, actor, occurred_at, authorize):
    """Failure is retryable; cancellation never revokes a confirmed document."""
    if not enabled(conn) or module_key not in MODULES or action not in {'failed','cancelled'}:
        raise ValueError('不支持的材料审核状态变更')
    student_id=str(student_id);item_id=int(item_id)
    current=_review(conn,item_id,student_id,module_key,allow_cancelled=True)
    owner=_domain_owner(conn)
    message='保存未完成，请检查文件与对应业务记录后重试。' if action=='failed' else None
    def operation(view):
        changes=owner.total_changes;authorize(view,current)
        if owner.total_changes!=changes:raise ValueError('authorization_must_be_readonly')
        if current['status']!='NeedsReview':return False
        if action=='failed' and current.get('error_message')==message:return False
        if action=='failed':
            view.execute("UPDATE upload_review_items SET error_message=? WHERE id=? AND status='NeedsReview'",(message,item_id))
        else:
            view.execute("UPDATE upload_review_items SET status='Cancelled',cancelled_by=?,cancelled_at=?,error_message=NULL WHERE id=? AND status='NeedsReview'",
                (actor['id'],occurred_at,item_id))
        _refresh_session(view,current['upload_session_id'])
        view.execute('''INSERT INTO audit_logs(user_id,username,action_type,module_name,module_key,record_id,student_id,status,created_at)
            VALUES(?,?,?,'upload_review',?,?,?,?,?)''',
            (actor['id'],actor.get('username',''),'upload_review_'+action,MODULES[module_key][1],str(item_id),student_id,
             'failed' if action=='failed' else 'success',occurred_at))
        return True
    return run_domain_write(conn,event_type='materials.review_'+action,actor_kind='user',actor_ref=str(actor['id']),
        capture=lambda view:tuple(view.execute('SELECT status,error_message FROM upload_review_items WHERE id=?',(item_id,)).fetchone()),
        operation=operation,describe=lambda view,result,before,after:Mutation('materials.review_'+action,
            Subject('upload_review_item',str(item_id)),(Subject('student',student_id),Subject('module',MODULES[module_key][1]),
            *student_impacts(view,student_id))))


def confirm_application_document(conn, *, item_id, student_id, document_type, target_name, notes,
        original_name, file_sha256, file_size, destination, file_version, actor, occurred_at,
        request_context, authorize, after_document, module_key='Applications'):
    """The caller owns BEGIN and has already verified the durable source bytes.

    A retained review item is the resource retry identity across staff members.
    Fresh permission checks still use the current staff actor on every replay;
    the retained command correctly keeps its original confirming actor.
    """
    if not enabled(conn):raise ValueError('业务同步模式未启用')
    if module_key not in MODULES:raise ValueError('未知材料审核模块')
    category,module_scope=MODULES[module_key]
    student_id=str(student_id);item_id=int(item_id)
    if not re.fullmatch(r'[0-9a-f]{64}',file_sha256) or type(file_size) is not int or file_size<0:
        raise ValueError('材料文件校验信息无效')
    review=_review(conn,item_id,student_id,module_key)
    if review['original_file_name']!=original_name or int(review['file_size'])!=file_size:
        raise ValueError('材料内容与原上传审核记录不匹配')
    confirming_actor=review['confirmed_by'] if review['status']=='Confirmed' else actor['id']
    if confirming_actor is None:raise ValueError('历史材料审核缺少确认人，请从文件列表核对原记录')
    source=Subject('upload_review_item',str(item_id))
    command=Command('application_upload_review' if module_key=='Applications' else 'material_upload_review',
        'user',str(confirming_actor),str(item_id),
        {'student_id':student_id,'document_type':document_type,'target_name':target_name,'notes':notes,
         'file_sha256':file_sha256,'file_size':file_size,'context':request_context})
    receipt=conn.execute('SELECT 1 FROM business_change_commands WHERE command_key=?',(command.key,)).fetchone()
    expected=version_of(conn,source)
    if receipt:
        original=conn.execute('''SELECT source_version FROM business_change_events WHERE command_key=?
            AND event_type='materials.document_confirmed' AND source_kind=? AND source_id=?''',
            (command.key,source.kind,source.identifier)).fetchall()
        if len(original)!=1:raise ValueError('材料确认凭据缺少唯一源版本，请核对原记录')
        expected=int(original[0][0])-1
    def check(view):
        current=_review(view,item_id,student_id,module_key)
        authorize(view,current)
        if bool(receipt)!=(current['status']=='Confirmed'):
            raise ValueError('审核凭据与源状态不一致，请核对原确认记录')
    def operation(view):
        if not destination or type(file_version) is not int or file_version<1:
            raise ValueError('材料目标文件或版本无效')
        document_id=int(view.execute('''INSERT INTO student_documents(student_id,folder_category,document_type,
            original_name,stored_name,file_path,version,file_size,uploaded_by,uploaded_at,is_active)
            VALUES(?,?,?,?,?,?,?,?,?,?,1)''',
            (student_id,category,document_type,original_name,str(destination).rsplit('/',1)[-1],str(destination),
             file_version,file_size,actor['id'],occurred_at)).lastrowid)
        changed=view.execute('''UPDATE upload_review_items SET final_file_name=?,final_file_path=?,
            selected_document_type=?,target_name=?,notes=?,status='Confirmed',confirmed_by=?,confirmed_at=?,error_message=NULL
            WHERE id=? AND status='NeedsReview' ''',
            (str(destination).rsplit('/',1)[-1],str(destination),document_type,target_name,notes,actor['id'],occurred_at,item_id))
        if changed.rowcount!=1:raise ValueError('材料审核状态已变化，请刷新后重试')
        # This includes optional Case services through their explicit transaction
        # ports. A failed association rolls back the review and document too.
        business_record_id=after_document(view,document_id)
        if business_record_id is not None and (type(business_record_id) is not int or business_record_id<1):
            raise ValueError('审核业务回调必须返回正式记录编号或空值')
        business_impacts=[]
        if business_record_id is not None:
            if module_key not in BUSINESS_RECORDS:raise ValueError('当前材料模块没有对应的正式业务记录')
            table,kind=BUSINESS_RECORDS[module_key]
            cursor=view.execute('SELECT * FROM '+table+' WHERE id=?',(business_record_id,))
            row=cursor.fetchone()
            record=dict(zip((column[0] for column in cursor.description),row)) if row else {}
            if str(record.get('student_id') or '')!=student_id:
                raise ValueError('审核业务记录不属于当前学生')
            business_impacts.append(Subject(kind,str(business_record_id)))
            bound_cycle=record.get('application_cycle_id')
            if bound_cycle is not None:
                cursor=view.execute('SELECT * FROM application_cycles WHERE id=? AND student_id=?',(bound_cycle,student_id))
                row=cursor.fetchone()
                if row is None:raise ValueError('审核业务记录与申请周期归属不匹配')
                cycle=dict(zip((column[0] for column in cursor.description),row))
                business_impacts.append(Subject('application_cycle',str(bound_cycle)))
                if cycle.get('source_student_case_id') is not None:
                    business_impacts.append(Subject('student_case',str(cycle['source_student_case_id'])))
        _refresh_session(view,review['upload_session_id'])
        actions=['document_type_selected','upload','file_saved_to_final_folder']
        if file_version>1:actions.append('duplicate_version_created')
        for action in actions:
            view.execute('''INSERT INTO audit_logs(user_id,username,action_type,module_name,module_key,record_id,
                student_id,file_path,new_value,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,'success',?)''',
                (actor['id'],actor.get('username',''),action,category if action=='upload' else 'upload_review',
                 module_scope,str(document_id if action=='upload' else item_id),student_id,str(destination),
                 json.dumps({'document_id':document_id,'review_item_id':item_id,'document_type':document_type,
                             'sha256':file_sha256},sort_keys=True),occurred_at))
        impacts=(Subject('student',student_id),Subject('document',str(document_id)),Subject('module',module_scope),
                 *business_impacts,*student_impacts(view,student_id))
        return Outcome({'document_id':document_id,'record_id':business_record_id},(Mutation('materials.document_confirmed',source,impacts),))
    return execute_command(conn,command,registry=Registry([]),authorize=check,operation=operation,
                           expected_versions={source:expected},occurred_at=occurred_at)
