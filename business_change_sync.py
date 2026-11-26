"""Opt-in HTTP bridge; shared infrastructure with explicit domain adapters.

No app imports or database effects at import time. Existing authentication,
CSRF and module authorization remain the owning handler's responsibility.
"""
from collections import Counter
import re
import uuid

from business_changes.contracts import Subject
from business_changes.registry import Registry
from business_changes.runtime import enabled
from business_changes.schema import SchemaUnavailable
from business_changes.snapshots import read_snapshot,MinimumVersionUnavailable
from business_changes.versions import version_of
from business_changes.read_admission import limit_snapshot_reads


class BusinessChangeSyncMixin:
    BUSINESS_SYNC_STUDENT_TABS=frozenset({'applications','contracts','school_plans','offer','payments','coe','visa','homestay','history','reminders','transfer_cases','progression_cases'})

    def business_change_runtime(self):
        raise NotImplementedError('the owning app must supply its runtime explicitly')

    BUSINESS_SYNC_STUDENT_LIST_FILTERS=frozenset({'q','selected_student_id','sort','stage','lifecycle_type','consultant_id',
        'year_level','alert','contract_status','created_from','created_to','updated_from','updated_to'})

    def business_change_student_list_packet(self,*,minimum=0):
        from urllib.parse import urlencode
        g=self.business_change_runtime();scope=Subject('dashboard','workflow')
        filters={key:values[0] for key,values in self.query.items()
            if key in self.BUSINESS_SYNC_STUDENT_LIST_FILTERS and values}
        endpoint='/api/business-changes/snapshot?'+urlencode({'surface':'student_list',**filters})
        parent=super().students
        def load(connection,allowed):
            if scope not in allowed:raise PermissionError('student_list_scope_forbidden')
            body=parent('GET',fragment_only=True)
            if not isinstance(body,str):raise ValueError('student_list_fragment_unavailable')
            html=('<div style="display:contents" data-business-sync-fragment="student-list" '
                f'data-business-sync-endpoint="{g["esc"](endpoint)}">'
                '<div class="business-sync-toolbar"><button type="button" class="secondary compact" '
                'data-business-sync-refresh>刷新状态</button> '
                '<button type="button" class="ghost compact" data-business-sync-discard-current hidden>取消编辑并更新</button> '
                '<small role="status" data-business-sync-status></small></div>'+body+'</div>')
            return {'fragments':{'student-list':html}}
        return read_snapshot(g['db'],(scope,),can_read=lambda subject:subject==scope
            and g['can_access_module'](self.user,'students') and g['allowed'](self.user,'students.view'),
            load=load,minimum_versions={scope:minimum})

    def business_change_student_list_snapshot(self):
        g=self.business_change_runtime()
        if not g['can_access_module'](self.user,'students') or not g['allowed'](self.user,'students.view'):
            return self.json_response({'ok':False,'code':'forbidden'},403)
        try:
            minimum=int((self.query.get('minimum_version') or ['0'])[0])
            if minimum<0:raise ValueError('invalid_revision')
        except (ValueError,TypeError):return self.json_response({'ok':False,'code':'invalid_revision'},400)
        if not self.business_changes_enabled():return self.json_response({'ok':False,'code':'sync_not_enabled'},503)
        try:packet=self.business_change_student_list_packet(minimum=minimum)
        except MinimumVersionUnavailable:return self.json_response({'ok':False,'code':'revision_not_visible'},409)
        except SchemaUnavailable:return self.json_response({'ok':False,'code':'sync_schema_unavailable'},503)
        return self.json_response(packet)

    def students(self,method,*,fragment_only=False):
        if method!='GET' or fragment_only or not self.business_changes_enabled():
            return super().students(method,fragment_only=fragment_only)
        g=self.business_change_runtime()
        if not g['can_access_module'](self.user,'students') or not g['allowed'](self.user,'students.view'):
            return self.deny_module_access('students')
        import json
        packet=self.business_change_student_list_packet()
        initial=g['esc'](json.dumps({key:packet[key] for key in ('etag','versions','as_of')},separators=(',',':')))
        body=packet['data']['fragments']['student-list'].replace('data-business-sync-fragment=',
            f'data-business-sync-initial-snapshot="{initial}" data-business-sync-fragment=',1)
        message,kind=self.message()
        return self.send_html(g['page']('学生管理',body,self.user,self.csrf,'students',g['nas'].status(),message,kind),
            headers=[('Cache-Control','no-store')])

    def business_change_higher_education_packet(self,*,minimum=0):
        g=self.business_change_runtime();scope=Subject('module','higher_education')
        parent=super().higher_education_workspace
        def load(connection,allowed):
            if scope not in allowed:raise PermissionError('higher_education_scope_forbidden')
            body=parent('GET',fragment_only=True)
            if not isinstance(body,str):raise ValueError('higher_education_fragment_unavailable')
            endpoint='/api/business-changes/snapshot?surface=higher_education_workspace'
            html=('<div style="display:contents" data-business-sync-fragment="higher-education-workspace" '
                f'data-business-sync-endpoint="{endpoint}"><div data-business-sync-controls hidden>'
                '<button type="button" class="secondary" data-business-sync-refresh>刷新数据</button> '
                '<button type="button" class="ghost" data-business-sync-discard-current hidden>取消编辑并更新</button> '
                '<span role="status" data-business-sync-status></span></div>'+body+'</div>')
            return {'fragments':{'higher-education-workspace':html}}
        return read_snapshot(g['db'],(scope,),can_read=lambda subject:subject==scope
            and g['can_access_module'](self.user,'higher_education'),load=load,minimum_versions={scope:minimum})

    def business_change_higher_education_snapshot(self):
        g=self.business_change_runtime()
        if not g['can_access_module'](self.user,'higher_education'):return self.json_response({'ok':False,'code':'forbidden'},403)
        try:
            minimum=int((self.query.get('minimum_version') or ['0'])[0])
            if minimum<0:raise ValueError('invalid_revision')
        except (ValueError,TypeError):return self.json_response({'ok':False,'code':'invalid_revision'},400)
        if not self.business_changes_enabled():return self.json_response({'ok':False,'code':'sync_not_enabled'},503)
        try:packet=self.business_change_higher_education_packet(minimum=minimum)
        except MinimumVersionUnavailable:return self.json_response({'ok':False,'code':'revision_not_visible'},409)
        except SchemaUnavailable:return self.json_response({'ok':False,'code':'sync_schema_unavailable'},503)
        return self.json_response(packet)

    def higher_education_workspace(self,method,*,fragment_only=False):
        if method!='GET' or fragment_only or not self.business_changes_enabled():
            return super().higher_education_workspace(method,fragment_only=fragment_only)
        import json
        g=self.business_change_runtime()
        if not g['can_access_module'](self.user,'higher_education'):return self.deny_module_access('higher_education')
        packet=self.business_change_higher_education_packet()
        initial=g['esc'](json.dumps({key:packet[key] for key in ('etag','versions','as_of')},separators=(',',':')))
        body=packet['data']['fragments']['higher-education-workspace'].replace('data-business-sync-fragment=',
            f'data-business-sync-initial-snapshot="{initial}" data-business-sync-fragment=',1)
        message,kind=self.message()
        return self.send_html(g['page']('高等教育申请',body,self.user,self.csrf,'higher_education',g['nas'].status(),message,kind),
            headers=[('Cache-Control','no-store')])

    def business_change_higher_education_master_action(self,method,parent,*args,modules=('school_library',)):
        if method!='POST':return parent(method,*args)
        from business_changes.authorization import current_staff
        from business_changes.staff_boundary import run_staff_transaction
        from business_changes.snapshots import read_scope
        g=self.business_change_runtime()
        def authorize(connection):
            actor=current_staff(connection,self.user['id'])
            if actor['role_name'] not in {'Admin','Manager'} or not any(g['can_access_module'](actor,module) for module in modules):
                raise PermissionError('当前账号没有维护大学、路径和课程的权限。')
            return actor
        try:
            if self.business_changes_enabled():
                return run_staff_transaction(self,g['db'],authorize=authorize,operation=lambda:parent(method,*args))
            with read_scope(g['db']) as connection:actor=authorize(connection)
            previous=self.user
            try:
                self.user={**previous,**actor}
                return parent(method,*args)
            finally:self.user=previous
        except PermissionError as exc:return self.error_page(str(exc),403)

    def higher_education_provider_create(self,method):
        return self.business_change_higher_education_master_action(method,super().higher_education_provider_create,modules=('higher_education','school_library'))

    def higher_education_provider_review(self,method,provider_id):
        return self.business_change_higher_education_master_action(method,super().higher_education_provider_review,provider_id,modules=('higher_education','school_library'))

    def higher_education_relationship_create(self,method):
        return self.business_change_higher_education_master_action(method,super().higher_education_relationship_create,modules=('higher_education','school_library'))

    def australian_university_update(self,method,university_id):
        return self.business_change_higher_education_master_action(method,super().australian_university_update,university_id)

    def australian_university_pathway_create(self,method,university_id):
        return self.business_change_higher_education_master_action(method,super().australian_university_pathway_create,university_id)

    def australian_university_pathway_segment_create(self,method,university_id):
        return self.business_change_higher_education_master_action(method,super().australian_university_pathway_segment_create,university_id)

    def australian_university_course_create(self,method,university_id):
        return self.business_change_higher_education_master_action(method,super().australian_university_course_create,university_id)

    def australian_university_course_status(self,method,university_id,course_id):
        return self.business_change_higher_education_master_action(method,super().australian_university_course_status,university_id,course_id)

    def australian_university_course_merge(self,method,university_id,course_id):
        return self.business_change_higher_education_master_action(method,super().australian_university_course_merge,university_id,course_id)

    def business_change_higher_education_form_fields(self,action,identifier=None,*,cycles=()):
        if not self.business_changes_enabled():return {}
        import json
        from business_changes.commands import digest
        from business_changes.higher_education_commands import ACTIONS
        from business_changes.snapshots import read_scope
        kind=ACTIONS[action][0]
        ids=tuple(int(value) for value in cycles) if action=='create_plan' else (int(identifier),)
        with read_scope(self.business_change_runtime()['db']) as connection:
            revisions={str(value):version_of(connection,Subject(kind,str(value))) for value in ids}
        nonce=digest(['he-staff-form',action,revisions,self.user['id'],self.csrf])
        return {'business_command_key':nonce,'business_expected_versions':json.dumps(revisions,sort_keys=True,separators=(',',':'))}

    def business_change_higher_education_hidden_fields(self,action,identifier=None,*,cycles=()):
        esc=self.business_change_runtime()['esc']
        values=self.business_change_higher_education_form_fields(action,identifier,cycles=cycles)
        if not values:return ''
        return '<span data-he-command-fields hidden>'+''.join(
            f'<input type="hidden" name="{key}" value="{esc(value)}">' for key,value in values.items())+'</span>'

    def business_change_higher_education_action(self,method,parent,action,*args):
        if method!='POST':return parent(method,*args)
        from business_changes.authorization import current_staff
        from business_changes.staff_boundary import run_staff_transaction,DeferredReply,RejectedReply
        from business_changes.commands import CommandConflict,VersionConflict
        from business_changes.higher_education_commands import save_action
        from higher_education_repository import HigherEducationError
        import json
        g=self.business_change_runtime()
        def authorize(view):
            actor=current_staff(view,self.user['id'])
            if not g['can_access_module'](actor,'higher_education') or actor['role_name']=='View Only':
                raise PermissionError('你没有修改高等教育申请的权限。')
            return actor
        try:
            if self.business_changes_enabled():
                def write():
                    # Authorization has already run under the owning writer.
                    try:
                        identifier=int(self.form.get('application_cycle_id')) if action=='create_plan' else int(args[0])
                        revisions=json.loads(self.form.get('business_expected_versions') or '{}')
                        expected=revisions[str(identifier)]
                        if type(expected) is not int or expected<0:raise ValueError('invalid_version')
                    except (TypeError,ValueError,KeyError):
                        raise VersionConflict('缺少有效保存凭据，请刷新数据后重试；当前输入已保留。')
                    reply=[]
                    def operation(view):
                        with g['db'].business_scope(view):result=parent(method,*args)
                        if not isinstance(result,DeferredReply):raise RuntimeError('staff_route_reply_must_wait_for_commit')
                        if result.rejected:raise RejectedReply(result)
                        reply.append(result)
                    connection=g['db'].connect()
                    result=save_action(connection,action=action,identifier=identifier,actor=self.user,
                        nonce=self.form.get('business_command_key'),expected_version=expected,
                        payload={key:value for key,value in self.form.items() if key not in {'csrf','business_command_key','business_expected_versions'}},
                        occurred_at=g['now_iso'](),authorize=authorize,operation=operation)
                    return reply[0] if reply else DeferredReply('flash_redirect',('/higher-education','已核对：该操作已成功保存。'),{})
                def respond(reply):
                    if 'application/json' not in str(getattr(self,'headers',{}).get('Accept','')):
                        return getattr(self,reply.method)(*reply.args,**reply.kwargs)
                    if reply.rejected:
                        message=str(reply.args[0] if reply.method=='error_page' else reply.args[1])
                        status=int(reply.args[1] if len(reply.args)>1 else reply.kwargs.get('status',400)) if reply.method=='error_page' else 400
                        return self.json_response({'ok':False,'message':message},status)
                    return self.json_response({'ok':True,'message':'已保存，正在更新相关数据。'})
                return run_staff_transaction(self,g['db'],authorize=authorize,operation=write,reply_handler=respond)
            # The rollout switch controls synchronization, never authorization.
            from business_changes.snapshots import read_scope
            with read_scope(g['db']) as connection:actor=authorize(connection)
            previous=self.user
            try:
                self.user={**previous,**actor}
                return parent(method,*args)
            finally:self.user=previous
        except PermissionError as exc:return self.error_page(str(exc),403)
        except (VersionConflict,CommandConflict) as exc:
            message='数据或保存凭据已改变，请刷新数据后核对；当前输入已保留。'
            if 'application/json' in str(getattr(self,'headers',{}).get('Accept','')):return self.json_response({'ok':False,'message':message},409)
            return self.error_page(message,409)
        except (ValueError,HigherEducationError) as exc:
            if 'application/json' in str(getattr(self,'headers',{}).get('Accept','')):return self.json_response({'ok':False,'message':str(exc)},400)
            return self.error_page(str(exc),400)

    def higher_education_plan_create(self,method):
        return self.business_change_higher_education_action(method,super().higher_education_plan_create,'create_plan')

    def higher_education_plan_item_create(self,method,plan_id):
        return self.business_change_higher_education_action(method,super().higher_education_plan_item_create,'add_item',plan_id)

    def higher_education_plan_confirm(self,method,plan_id):
        return self.business_change_higher_education_action(method,super().higher_education_plan_confirm,'confirm_plan',plan_id)

    def higher_education_plan_item_remove(self,method,item_id):
        return self.business_change_higher_education_action(method,super().higher_education_plan_item_remove,'remove_item',item_id)

    def higher_education_plan_item_submit(self,method,item_id):
        return self.business_change_higher_education_action(method,super().higher_education_plan_item_submit,'submit_item',item_id)

    def higher_education_offer_record(self,method,application_id):
        return self.business_change_higher_education_action(method,super().higher_education_offer_record,'record_offer',application_id)

    def higher_education_offer_accept(self,method,offer_id):
        return self.business_change_higher_education_action(method,super().higher_education_offer_accept,'accept_offer',offer_id)

    def higher_education_coe_link_create(self,method,application_id):
        return self.business_change_higher_education_action(method,super().higher_education_coe_link_create,'link_coe',application_id)

    def business_change_record_form_values(self,module_key,student_id,record_id=None,*,version=None):
        if not self.business_changes_enabled():return {}
        from business_changes.record_commands import RECORDS
        from business_changes.commands import digest
        student_id=str(student_id or '')
        if module_key not in RECORDS:raise ValueError('当前记录尚未接入保存命令')
        scope=Subject(RECORDS[module_key][1],str(record_id)) if record_id is not None else Subject('student',str(student_id))
        database=self.business_change_runtime()['db']
        if version is None:
            conn=database.connect()
            try:version=version_of(conn,scope)
            finally:database._release_connection(conn)
        # Stable for this actor/session/source version: polling itself must not
        # change the snapshot etag. A successful save supplies the next revision.
        nonce=digest(['manual-record',module_key,str(student_id),record_id,version,self.user['id'],self.csrf])
        return {'business_command_key':nonce,'business_expected_version':str(version)}

    def business_change_record_hidden_fields(self,module_key,student_id,record_id=None):
        esc=self.business_change_runtime()['esc']
        return ''.join(f'<input type="hidden" name="{key}" value="{esc(value)}">'
            for key,value in self.business_change_record_form_values(module_key,student_id,record_id).items())

    def business_change_save_manual_record(self,module_key,student_id,record_id,operation,*,
            caller_connection=None,payload_extra=None,authorization_policy=None):
        from contextlib import nullcontext
        from business_changes.record_commands import save_record
        g=self.business_change_runtime();database=g['db']
        try:expected=int(self.form.get('business_expected_version',''))
        except (TypeError,ValueError):raise ValueError('缺少业务版本，请刷新后重试') from None
        def actor(view):
            from business_changes.authorization import current_staff
            return current_staff(view,self.user['id'])
        def authorize(view):
            current=actor(view)
            if module_key=='Visa':
                allowed=g['can_save_visa_record'](current)
                if payload_extra is not None:
                    allowed=allowed and (g['can_module_action'](current,'visa','upload') or g['allowed'](current,'visas.manage'))
            elif module_key=='Payments':allowed=g['can_payment_action'](current,'create') or g['can_payment_action'](current,'upload')
            elif module_key=='Contracts':
                if authorization_policy not in {None,'contracts_manage','contracts_generate','contracts_stamp'}:raise ValueError('未知合同保存授权方式。')
                allowed=(g['allowed'](current,'contracts.manage') if authorization_policy=='contracts_manage' else
                    g['allowed'](current,'contracts.manage') and g['can_apply_aaie_stamp'](current) if authorization_policy=='contracts_stamp' else
                    g['can_generate_contract'](current) if authorization_policy=='contracts_generate' else g['can_create_contract_center'](current))
                row=view.execute('SELECT contract_type FROM contracts WHERE id=?',(int(record_id),)).fetchone()
                if row is not None and g['normalize_contract_type'](row[0])=='TransferApplicationContract':
                    allowed=allowed and g['can_manage_transfer_contract'](current)
            else:allowed=g['can_workflow_module_save'](current,module_key)
            if not allowed:raise PermissionError('员工业务保存权限已变化')
        def write(view):
            previous=self.user;self.user=actor(view)
            try:
                with database.business_scope(view):return operation(view)
            finally:self.user=previous
        payload={key:value for key,value in self.form.items() if key not in
            {'csrf','business_command_key','business_expected_version'}}
        if payload_extra is not None:payload={'fields':payload,'prepared':payload_extra}
        with (database.transaction() if caller_connection is None else nullcontext(caller_connection)) as conn:
            lookup_only=bool(caller_connection is not None and conn.execute('PRAGMA query_only').fetchone()[0])
            return save_record(conn,module_key=module_key,student_id=student_id,record_id=record_id,actor=self.user,
                nonce=self.form.get('business_command_key'),expected_version=expected,payload=payload,
                occurred_at=g['now_iso'](),authorize=authorize,operation=write,lookup_only=lookup_only)

    def business_changes_enabled(self):
        db=self.business_change_runtime()['db'];conn=db.connect()
        try:return enabled(conn)
        finally:db._release_connection(conn)

    def business_change_checklist_form_values(self,case_id,*,connection=None):
        from business_changes.commands import digest
        database=self.business_change_runtime()['db'];owned=connection is None
        conn=database.connect() if owned else connection
        try:
            version=version_of(conn,Subject('visa_case',str(int(case_id))))
            previous=conn.execute('''SELECT command_key FROM business_change_commands
                WHERE command_type='visa_checklist_save' AND actor_ref=?
                AND json_extract(result_json,'$.case_id')=? ORDER BY rowid DESC LIMIT 1''',
                (str(self.user['id']),int(case_id))).fetchone()
            nonce=digest(['visa-checklist',int(case_id),version,self.user['id'],self.csrf,previous[0] if previous else None])
            return {'business_command_key':nonce,'business_expected_version':str(version)}
        finally:
            if owned:database._release_connection(conn)

    def business_change_checklist_hidden_fields(self,case_id):
        if not self.business_changes_enabled():return ''
        esc=self.business_change_runtime()['esc']
        return ''.join(f'<input type="hidden" name="{key}" value="{esc(value)}">'
            for key,value in self.business_change_checklist_form_values(case_id).items())

    def business_change_save_checklist(self,student_id,case_id,updates,stamp):
        from business_changes.materials import save_checklist_command
        g=self.business_change_runtime();database=g['db']
        try:expected=int(self.form.get('business_expected_version',''))
        except (ValueError,TypeError):raise ValueError('缺少材料版本，请刷新后重试。') from None
        nonce=str(self.form.get('business_command_key') or '')
        if not re.fullmatch(r'[a-f0-9]{64}',nonce):raise ValueError('缺少保存凭据，请刷新后重试。')
        def authorize(view):
            from business_changes.authorization import current_staff
            actor=current_staff(view,self.user['id'])
            if not actor or not g['can_access_module'](actor,'visa') or not g['can_workflow_module_save'](actor,'Visa'):
                raise PermissionError('材料保存权限已变化，请重新登录。')
            if student_id and not view.execute('SELECT 1 FROM students WHERE student_id=? AND deleted_at IS NULL',
                                               (student_id,)).fetchone():raise ValueError('学生已不存在或已删除。')
        with database.transaction() as conn:
            result=save_checklist_command(conn,student_id=student_id,case_id=int(case_id),updates=updates,
                actor_user_id=self.user['id'],stamp=stamp,command_key=nonce,expected_version=expected,authorize=authorize)
            fields=self.business_change_checklist_form_values(case_id,connection=conn)
        return {'ok':True,'business_receipt':fields,'replayed':result.replayed,'message':'Material Checklist 已保存。'}

    def business_change_link_existing_visa_document(self,result,stamp):
        import hashlib
        from pathlib import Path
        from business_changes.visa_links import link_existing_document
        from business_changes.snapshots import read_scope
        g=self.business_change_runtime();database=g['db'];cid=int(result['case']['id']);did=int(result['document']['id'])
        def authorize(view):
            from business_changes.authorization import current_staff
            current=current_staff(view,self.user['id'])
            if not current or not g['can_access_module'](current,'visa') or not g['can_save_visa_record'](current):
                raise PermissionError('签证材料关联权限已变化。')
            return current
        with read_scope(database) as conn:
            authorize(conn)
            case=conn.execute('SELECT * FROM visa_cases WHERE id=?',(cid,)).fetchone()
            document=conn.execute('SELECT * FROM student_documents WHERE id=?',(did,)).fetchone()
            if not case or not document or not case['student_id'] or case['student_id']!=document['student_id']:
                raise ValueError('案件或材料归属已变化。')
            case=dict(case);document=dict(document)
            student=conn.execute('SELECT * FROM students WHERE student_id=? AND deleted_at IS NULL',(case['student_id'],)).fetchone()
            if student is None:raise ValueError('学生已不存在或已删除。')
        try:
            path=g['nas'].require_student_file_readable(student,document['file_path'])
            hasher=hashlib.sha256();size=0
            with Path(path).open('rb') as source:
                for block in iter(lambda:source.read(1024*1024),b''):
                    hasher.update(block);size+=len(block)
        except (OSError,ValueError) as error:raise ValueError('材料原文件当前不可读取，不能确认为可用关联。') from error
        if document['file_size'] and int(document['file_size'])!=size:
            raise ValueError('材料原文件大小与保存记录不一致，请先核对来源。')
        with database.transaction() as conn:
            return link_existing_document(conn,database=database,case_before=case,document_before=document,actor=self.user,
                role=result['document_role'],notes=result['notes'],occurred_at=stamp,
                observed_sha256=hasher.hexdigest(),observed_size=size,authorize=authorize,
                ip=self.client_address[0],device=self.headers.get('User-Agent'))

    def business_change_discard_signature_image(self,path,contract_id):
        from pathlib import Path
        g=self.business_change_runtime();path=Path(path)
        if not path.name.startswith('signature_sync_'):return
        g['ensure_within'](path,g['CONTRACT_SIGNING_DIR']/str(contract_id))
        if not g['db'].one('SELECT 1 FROM contract_signatures WHERE signature_image_path=?',(str(path),)):
            path.unlink(missing_ok=True)

    def business_change_save_parent_signature(self,link,token,signature_path,stamp,operation):
        import hashlib
        from pathlib import Path
        from business_changes.commands import digest
        from business_changes.contracts import BusinessPermissionError
        from business_changes.parent_contract_signing import record_parent_signature
        g=self.business_change_runtime();database=g['db']
        actor_ref=digest(['contract_parent_sign',link['token_hash'],link['bound_wechat_openid'] or ''])
        def fresh_link():return self.contract_signing_row_by_token(token)
        def authorize(view):
            with database.business_scope(view):current=fresh_link()
            if current is None or current['id']!=link['id'] or current['contract_id']!=link['contract_id']:
                raise BusinessPermissionError('合同签署权限已变化。')
            status=str(current['status'] or '').lower()
            if (g['normalize_contract_status'](current['contract_status']) in {'Deleted','Voided','Revoked','Superseded'}
                    or current['deleted_at'] or current['voided_at'] or current['invalidated_at']
                    or status not in {'active','signed'} or status=='active' and g['is_expired'](current['expires_at'])
                    or current['bound_wechat_openid']!=link['bound_wechat_openid']
                    or not self.contract_signing_cookie_valid(current)):
                raise BusinessPermissionError('合同签署权限已变化。')
        def write(view):
            with database.business_scope(view):
                current=fresh_link()
                signature_id=operation(current)
                self.contract_signing_mark_link_signed(current['id'],stamp,signature_id)
                self.contract_signing_record_log(self.contract_signing_row_by_id(current['id']),
                    'signature_saved',{'signature_id':signature_id,'archive_status':'pending'})
                return signature_id
        g['ensure_within'](Path(signature_path),g['CONTRACT_SIGNING_DIR']/str(link['contract_id']))
        image_sha256=hashlib.sha256(Path(signature_path).read_bytes()).hexdigest()
        with database.transaction() as conn:
            return record_parent_signature(conn,contract_id=int(link['contract_id']),link_id=int(link['id']),
                student_id=link['student_id'] or link['contract_student_id'] or '',actor_ref=actor_ref,
                image_sha256=image_sha256,signature_path=str(signature_path),
                occurred_at=stamp,authorize=authorize,operation=write)

    def contract_signing_archive_documents(self,signature_id,user=None,*,silent_archive=False):
        if not self.business_changes_enabled():
            return super().contract_signing_archive_documents(signature_id,user=user,silent_archive=silent_archive)
        from business_changes.contract_archives import archive_contract
        def finalize(view,signature,contract,student,signed_doc,confirmation_doc,final_doc,stamp,actor):
            return self._contract_signing_finalize_archive(signature,contract,student,signed_doc,confirmation_doc,
                final_doc,stamp,user=actor,silent_archive=silent_archive)
        return archive_contract(self,signature_id,user,silent_archive,finalize)

    def business_change_contract_archive_failed(self,signature_id,error,user=None):
        from business_changes.parent_contract_signing import record_archive_failure
        from business_changes.contract_archives import _actor
        g=self.business_change_runtime();database=g['db']
        def authorize(view):
            if user is not None:
                cursor=view.execute('SELECT c.* FROM contracts c JOIN contract_signatures s ON s.contract_id=c.id WHERE s.id=?',(int(signature_id),))
                row=cursor.fetchone()
                if row is None:raise ValueError('签署记录不存在。')
                contract=dict(zip((item[0] for item in cursor.description),row));_actor(g,view,user,contract)
        with database.transaction() as conn:
            return record_archive_failure(conn,signature_id=int(signature_id),error_message=str(error),
                actor_kind='user' if user else 'system',actor_ref=str(user['id']) if user else 'contract_archive',
                occurred_at=g['now_iso'](),authorize=authorize)

    def materials_complete(self,method,student_id):
        if not self.business_changes_enabled():
            return super().materials_complete(method,student_id)
        if method!='POST':return
        g=self.business_change_runtime()
        if not g['can_review_module_action'](self.user,'Applications','confirm'):
            return self.deny_module_access('applications')
        from business_changes.application_review import review_materials,MaterialReviewBlocked
        def after_write(view):
            with g['db'].business_scope(view):
                g['advance_workflow_stage_to'](student_id,'申请材料审核')
                g['db'].audit(self.user,'update','workflow',student_id,new_value='materials_checked=true')
        try:
            with g['db'].transaction() as conn:
                if not enabled(conn):raise ValueError('业务同步模式已变化，请刷新后重试。')
                review_materials(conn,student_id=student_id,actor_user_id=self.user['id'],
                    occurred_at=g['now_iso'](),after_write=after_write)
        except MaterialReviewBlocked as exc:
            message={'no_initial_cycle':'当前没有可审核的首次申请周期。',
                     'parent_form_not_submitted':'家长在线表单尚未提交。'}.get(exc.code)
            if message is None:
                message='缺少：'+'、'.join(g['application_material_label'](kind) for kind in exc.missing_document_types)
            return self.flash_redirect(f'/students/{student_id}?tab=applications',message,'error')
        except ValueError as exc:
            if str(exc)=='学生不存在':return self.error_page('学生不存在',404)
            return self.flash_redirect(f'/students/{student_id}?tab=applications',str(exc),'error')
        return self.flash_redirect(f'/students/{student_id}?tab=applications','材料审核已完成')

    def contract_soft_delete_or_void(self,contract,reason_code='',reason_text=''):
        parent=super().contract_soft_delete_or_void
        if not self.business_changes_enabled():return parent(contract,reason_code,reason_text)
        from business_changes.contract_records import invalidate_contract
        g=self.business_change_runtime()
        with g['db'].transaction() as conn:
            return invalidate_contract(conn,database=g['db'],contract_id=contract['id'],actor_user_id=self.user['id'],
                operation=lambda current:parent(current,reason_code,reason_text))

    def send_html(self,content,status=200,headers=None,**kwargs):
        owned_shell=('<div class="app-shell">' in content or
            '<body class="mobile-page-body">' in content and 'data-business-sync-endpoint=' in content)
        if (getattr(self,'user',None) and owned_shell and '</body>' in content
                and not re.search(r'''<script\b[^>]*\bsrc=["']/assets/business-change-refresh\.js(?:\?[^"']*)?["']''',content)
                and self.business_changes_enabled()):
            content=content.replace('</body>','<script defer src="/assets/business-change-refresh.js?v=9"></script></body>',1)
        return super().send_html(content,status,headers,**kwargs)

    def business_change_versions(self,method):
        """Only revisions for a known page scope; never render/OCR/read files."""
        if method!='GET':return self.json_response({'ok':False,'code':'method_not_allowed'},405)
        if not self.user:return self.json_response({'ok':False,'code':'login_required'},401)
        g=self.business_change_runtime();surface=(self.query.get('surface') or [''])[0]
        scopes={'dashboard_workflow':('dashboard','workflow','dashboard'),
            'dashboard_overview':('dashboard','overview','dashboard'),
            'student_list':('dashboard','workflow','students'),
            'mobile_dashboard_overview':('dashboard','overview','dashboard'),
            'visa_overview':('dashboard','visa_cases','visa'),
            'higher_education_workspace':('module','higher_education','higher_education')}
        student_surface=surface in {'student_summary','student_tab'}
        case_surface=surface in {'student_visa_case','visa_case_workspace'}
        if surface not in scopes and not student_surface and not case_surface:
            return self.json_response({'ok':False,'code':'unknown_surface'},400)
        module='students' if student_surface else 'visa' if case_surface else scopes[surface][2]
        if not g['can_access_module'](self.user,module) or (student_surface or surface=='student_list') and not g['allowed'](self.user,'students.view'):
            return self.json_response({'ok':False,'code':'forbidden'},403)
        try:
            minimum=int((self.query.get('minimum_version') or ['0'])[0])
            if minimum<0:raise ValueError('invalid_revision')
            if student_surface:
                if surface=='student_tab' and (self.query.get('tab') or [''])[0] not in self.BUSINESS_SYNC_STUDENT_TABS:
                    raise ValueError('unknown_student_tab')
                student_id=str((self.query.get('student_id') or [''])[0]);scope=Subject('student',student_id)
            elif case_surface:
                value=str((self.query.get('case_id') or [''])[0])
                if not value.isascii() or not value.isdigit() or len(value)>18 or int(value)<1:raise ValueError('invalid_case')
                scope=Subject('visa_case',str(int(value)))
            else:scope=Subject(*scopes[surface][:2])
        except (ValueError,TypeError):return self.json_response({'ok':False,'code':'invalid_scope_or_revision'},400)
        def load(connection,allowed):
            if scope not in allowed:raise PermissionError('scope_forbidden')
            if student_surface:
                if not connection.execute('SELECT 1 FROM students WHERE student_id=? AND deleted_at IS NULL',(student_id,)).fetchone():
                    raise LookupError('student_not_found')
                cycle=(self.query.get('application_cycle_id') or [None])[0]
                if cycle is not None and (not str(cycle).isascii() or not str(cycle).isdigit() or len(str(cycle))>18 or not connection.execute(
                    'SELECT 1 FROM application_cycles WHERE id=? AND student_id=?',(int(cycle),student_id)).fetchone()):
                    raise LookupError('cycle_not_found')
            elif case_surface:
                row=connection.execute('SELECT student_id FROM visa_cases WHERE id=? AND archived_at IS NULL',(int(scope.identifier),)).fetchone()
                if row is None or surface=='student_visa_case' and str(row[0] or '')!=str((self.query.get('student_id') or [''])[0]):
                    raise LookupError('case_not_found')
            if surface=='dashboard_overview':
                from business_changes.snapshots import read_authorization_context
                return {'authorization_context':read_authorization_context(connection,self.user)}
            return {}
        try:
            if not self.business_changes_enabled():return self.json_response({'ok':False,'code':'sync_not_enabled'},503)
            result=read_snapshot(g['db'],(scope,),can_read=lambda item:item==scope,
                load=load,minimum_versions={scope:minimum})
        except LookupError:return self.json_response({'ok':False,'code':'subject_not_found'},404)
        except MinimumVersionUnavailable:return self.json_response({'ok':False,'code':'revision_not_visible'},409)
        except SchemaUnavailable:return self.json_response({'ok':False,'code':'sync_schema_unavailable'},503)
        packet={'versions':result['versions'],'as_of':result['as_of']}
        if surface=='dashboard_overview':packet['authorization_context']=result['data']['authorization_context']
        return self.json_response(packet)

    @limit_snapshot_reads
    def business_change_snapshot(self,method):
        return self._business_change_snapshot(method)

    def _business_change_snapshot(self,method):
        if method!='GET':return self.json_response({'ok':False,'code':'method_not_allowed'},405)
        if not self.user:return self.json_response({'ok':False,'code':'login_required'},401)
        g=self.business_change_runtime()
        surface=(self.query.get('surface') or [''])[0]
        if surface=='higher_education_workspace':return self.business_change_higher_education_snapshot()
        if surface=='mobile_dashboard_overview':return self.business_change_mobile_snapshot()
        if surface in {'student_visa_case','visa_case_workspace'}:
            return self.business_change_case_snapshot(surface)
        if surface=='student_list':return self.business_change_student_list_snapshot()
        if surface=='student_summary':return self.business_change_student_summary_snapshot()
        if surface=='student_tab':
            tab=(self.query.get('tab') or [''])[0]
            if tab not in self.BUSINESS_SYNC_STUDENT_TABS:
                return self.json_response({'ok':False,'code':'unknown_student_tab'},400)
            return self.business_change_student_summary_snapshot(tab=tab)
        module='visa' if surface=='visa_overview' else 'dashboard'
        if not g['can_access_module'](self.user,module):
            return self.json_response({'ok':False,'code':'forbidden'},403)
        if surface not in {'dashboard_workflow','dashboard_overview','visa_overview'}:
            return self.json_response({'ok':False,'code':'unknown_surface'},400)
        chart_only = surface == 'dashboard_workflow' and (self.query.get('view') or [''])[0] == 'chart'
        try:
            minimum=int((self.query.get('minimum_version') or ['0'])[0])
            if minimum<0:raise ValueError('invalid_revision')
        except (ValueError,TypeError):return self.json_response({'ok':False,'code':'invalid_revision'},400)
        scope=Subject('dashboard',{'dashboard_overview':'overview','visa_overview':'visa_cases'}.get(surface,'workflow'))
        try:
            if not self.business_changes_enabled():return self.json_response({'ok':False,'code':'sync_not_enabled'},503)
            def load(conn,allowed):
                if scope not in allowed:raise PermissionError('dashboard_scope_forbidden')
                if surface=='visa_overview':
                    return {'fragments':{'visa-overview':self.visa_management('GET',fragment_only=True)}}
                from dashboard_read_context import DashboardProjectionContext
                context=DashboardProjectionContext(conn)
                rows=tuple(g['load_application_stage_distribution'](conn,cycles_only=True,read_context=context))
                rows=tuple(x for x in rows if x.stage.key)
                identities=[('cycle',x.cycle_id) if x.cycle_id is not None else ('student',str(getattr(x,'student_id',''))) for x in rows]
                if len(set(identities))!=len(rows):raise ValueError('duplicate_cycle_in_distribution')
                from dashboard_stage_presentation import distribution_stage_key
                canonical=g['is_workflow_v2_active'](conn)
                counts=dict(Counter(distribution_stage_key(x.stage.key,canonical=canonical) for x in rows))
                if surface=='dashboard_overview':
                    html=self._dashboard_read_snapshot('GET',context,fragment_only=True,stage_students=rows)
                    fragments={'dashboard-overview':html}
                else:
                    html=g['dashboard_workflow_distribution_chart_html'](rows,canonical=canonical)
                    fragments={'dashboard-workflow':html}
                data={'total':len(rows),'counts':counts,'fragments':fragments}
                if not chart_only:
                    data['review_issues']=self.business_change_visa_consistency_issues()
                if surface=='dashboard_overview':
                    from business_changes.snapshots import read_authorization_context
                    data['authorization_context']=read_authorization_context(conn,self.user)
                return data
            result=read_snapshot(g['db'],(scope,),can_read=lambda subject:subject==scope and g['can_access_module'](self.user,module),
                                 load=load,minimum_versions={scope:minimum})
        except MinimumVersionUnavailable:return self.json_response({'ok':False,'code':'revision_not_visible'},409)
        except SchemaUnavailable:return self.json_response({'ok':False,'code':'sync_schema_unavailable'},503)
        if surface=='dashboard_overview':result['authorization_context']=result['data']['authorization_context']
        return self.json_response(result)

    def business_change_mobile_packet(self,*,minimum=0):
        g=self.business_change_runtime();scope=Subject('dashboard','overview')
        def load(connection,allowed):
            if scope not in allowed:raise PermissionError('dashboard_scope_forbidden')
            data=g['load_mobile_home_data'](g['db'],self.user,g['can_access_module'],g['load_application_stage_distribution'],
                connection=connection,cycles_only=True)
            body=g['render_mobile_home'](data,self.user,g['can_access_module'])
            endpoint='/api/business-changes/snapshot?surface=mobile_dashboard_overview'
            html=('<div data-business-sync-fragment="mobile-dashboard-overview" '
                f'data-business-sync-endpoint="{endpoint}"><div data-business-sync-controls hidden>'
                '<button type="button" class="secondary" data-business-sync-refresh>刷新数据</button> '
                '<span role="status" data-business-sync-status></span></div>'+body+'</div>')
            counts={stage.key:stage.count for stage in data.stages}
            return {'counts':counts,'total':sum(counts.values()),'fragments':{'mobile-dashboard-overview':html}}
        return read_snapshot(g['db'],(scope,),can_read=lambda subject:subject==scope
            and g['can_access_module'](self.user,'dashboard'),load=load,minimum_versions={scope:minimum})

    def business_change_mobile_snapshot(self):
        g=self.business_change_runtime()
        if not g['can_access_module'](self.user,'dashboard'):return self.json_response({'ok':False,'code':'forbidden'},403)
        try:
            minimum=int((self.query.get('minimum_version') or ['0'])[0])
            if minimum<0:raise ValueError('invalid_revision')
        except (ValueError,TypeError):return self.json_response({'ok':False,'code':'invalid_revision'},400)
        if not self.business_changes_enabled():return self.json_response({'ok':False,'code':'sync_not_enabled'},503)
        try:packet=self.business_change_mobile_packet(minimum=minimum)
        except MinimumVersionUnavailable:return self.json_response({'ok':False,'code':'revision_not_visible'},409)
        except SchemaUnavailable:return self.json_response({'ok':False,'code':'sync_schema_unavailable'},503)
        return self.json_response(packet)

    def mobile_dashboard(self,method):
        if method!='GET' or not self.business_changes_enabled():return super().mobile_dashboard(method)
        import json
        g=self.business_change_runtime()
        if not g['can_access_module'](self.user,'dashboard'):return self.deny_module_access('dashboard')
        packet=self.business_change_mobile_packet()
        initial=g['esc'](json.dumps({key:packet[key] for key in ('etag','versions','as_of')},separators=(',',':')))
        body=packet['data']['fragments']['mobile-dashboard-overview'].replace('data-business-sync-fragment=',
            f'data-business-sync-initial-snapshot="{initial}" data-business-sync-fragment=',1)
        return self.send_html(self.mobile_shell('首页',body,'home'),headers=[('Cache-Control','no-store')])

    def business_change_case_packet(self,surface,case_id,*,student_id=None,minimum=0):
        from urllib.parse import urlencode
        g=self.business_change_runtime();scope=Subject('visa_case',str(int(case_id)))
        name=('student-visa-case-' if surface=='student_visa_case' else 'visa-case-workspace-')+scope.identifier
        def load(conn,allowed):
            if scope not in allowed:raise PermissionError('case_scope_forbidden')
            row=conn.execute('SELECT * FROM visa_cases WHERE id=? AND archived_at IS NULL',(int(case_id),)).fetchone()
            if row is None or surface=='student_visa_case' and str(row['student_id'] or '')!=str(student_id or ''):
                raise LookupError('case_not_found')
            body=(self.student_visa_case_detail('GET',str(student_id),str(int(case_id)),fragment_only=True)
                if surface=='student_visa_case' else self.visa_case_workspace('GET',int(case_id),fragment_only=True))
            if not isinstance(body,str):raise ValueError('case_fragment_unavailable')
            if row['student_id'] and row['application_cycle_id'] is None:
                body=self.business_change_unassigned_visa_html(student_id=row['student_id'])+body
            # The existing preview controller owns a long-lived dialog. Keep
            # that widget outside the replaced fragment so its listeners live.
            body=body.replace(g['preview_dialog_html'](),'')
            active_tab=(self.visa_case_active_tab() if surface=='student_visa_case' else
                self.visa_case_normalized_tab((self.query.get('visa_tab') or [''])[0],'material_checklist'))
            params={'surface':surface,'case_id':scope.identifier,'visa_tab':active_tab}
            if surface=='student_visa_case':params['student_id']=str(student_id)
            endpoint='/api/business-changes/snapshot?'+urlencode(params)
            html=(f'<div style="display:contents" data-business-sync-fragment="{name}" data-business-sync-endpoint="{g["esc"](endpoint)}">'
                '<span data-business-sync-controls hidden><button type="button" class="secondary" data-business-sync-refresh>刷新数据</button> '
                '<button type="button" class="ghost" data-business-sync-discard-current hidden>取消编辑并更新</button> '
                '<span role="status" data-business-sync-status></span></span>'+body+'</div>')
            return {'fragments':{name:html}}
        return read_snapshot(g['db'],(scope,),can_read=lambda subject:subject==scope and g['can_access_module'](self.user,'visa'),
            load=load,minimum_versions={scope:minimum})

    def business_change_case_snapshot(self,surface):
        g=self.business_change_runtime()
        if not g['can_access_module'](self.user,'visa'):return self.json_response({'ok':False,'code':'forbidden'},403)
        try:
            value=str((self.query.get('case_id') or [''])[0])
            if not value.isascii() or not value.isdigit() or len(value)>18 or int(value)<1:raise ValueError('invalid_case')
            minimum=int((self.query.get('minimum_version') or ['0'])[0])
            if minimum<0:raise ValueError('invalid_revision')
        except (ValueError,TypeError):return self.json_response({'ok':False,'code':'invalid_case_or_revision'},400)
        if not self.business_changes_enabled():return self.json_response({'ok':False,'code':'sync_not_enabled'},503)
        try:
            packet=self.business_change_case_packet(surface,int(value),
                student_id=(self.query.get('student_id') or [None])[0],minimum=minimum)
        except PermissionError:return self.json_response({'ok':False,'code':'forbidden'},403)
        except LookupError:return self.json_response({'ok':False,'code':'case_not_found'},404)
        except MinimumVersionUnavailable:return self.json_response({'ok':False,'code':'revision_not_visible'},409)
        except SchemaUnavailable:return self.json_response({'ok':False,'code':'sync_schema_unavailable'},503)
        return self.json_response(packet)

    def business_change_case_page(self,surface,case_id,*,student_id=None):
        g=self.business_change_runtime()
        if not g['can_access_module'](self.user,'visa'):return self.deny_module_access('visa')
        try:packet=self.business_change_case_packet(surface,int(case_id),student_id=student_id)
        except LookupError:return self.error_page('Visa Case 不存在或已归档。',404)
        body=''.join(packet['data']['fragments'].values())
        import json
        initial=g['esc'](json.dumps({key:packet[key] for key in ('etag','versions','as_of')},separators=(',',':')))
        body=body.replace('data-business-sync-fragment=',f'data-business-sync-initial-snapshot="{initial}" data-business-sync-fragment=',1)
        body+=g['preview_dialog_html']()
        for script in ('visa-material-workspace','visa-result-workspace'):
            if 'src="/assets/'+script+'.js' not in body:body+=f'<script defer src="/assets/{script}.js?v=2"></script>'
        return self.send_html(g['page']('Visa Case',body,self.user,self.csrf,'visa_management',g['nas'].status()),
            headers=[('Cache-Control','no-store')])

    def student_visa_case_detail(self,method,student_id,case_id,*,fragment_only=False):
        if method=='GET' and not fragment_only and self.business_changes_enabled():
            return self.business_change_case_page('student_visa_case',case_id,student_id=student_id)
        return super().student_visa_case_detail(method,student_id,case_id,fragment_only=fragment_only)

    def student_detail_tab(self,method,student_id,tab,*,fragment_only=False):
        if method!='GET' or tab not in self.BUSINESS_SYNC_STUDENT_TABS or not self.business_changes_enabled():
            return super().student_detail_tab(method,student_id,tab,fragment_only=fragment_only)
        from business_changes.snapshots import read_scope
        try:
            with read_scope(self.business_change_runtime()['db']):
                html=super().student_detail_tab(method,student_id,tab,fragment_only=True)
        except PermissionError:
            if fragment_only:raise
            return self.deny_module_access('students')
        except LookupError:
            if fragment_only:raise
            return self.error_page('学生不存在',404)
        return html if fragment_only else self.send_html(html,headers=[('Cache-Control','no-store')])

    def student_detail(self,method,student_id,*,fragment_only=False):
        if method!='GET' or not self.business_changes_enabled():
            return super().student_detail(method,student_id,fragment_only=fragment_only)
        from business_changes.snapshots import read_scope
        with read_scope(self.business_change_runtime()['db']):
            return super().student_detail(method,student_id,fragment_only=fragment_only)

    def business_change_pending_reviews_html(self,student_id):
        if not self.business_changes_enabled():return ''
        from urllib.parse import quote
        from business_changes.pending_reviews import pending_reviews
        from business_changes.snapshots import read_scope
        g=self.business_change_runtime();esc=g['esc']
        with read_scope(g['db']) as conn:
            result=pending_reviews(conn,student_id,
                can_view=lambda module:g['can_review_module_action'](self.user,module,'view'))
        consistency=(self.business_change_visa_consistency_html(student_id=student_id)
            +self.business_change_unassigned_visa_html(student_id=student_id))
        if not result['pending']:return consistency
        links=[]
        for row in result['rows']:
            url=f"/students/{quote(str(student_id),safe='')}/upload-review/{quote(str(row['token']),safe='')}"
            failure=f" · {row['failed']} 份保存未完成" if row['failed'] else ''
            links.append(f'<li><a href="{esc(url)}">{esc(row["module"])} · {row["pending"]} 份待审核{failure}</a>'
                         f' <small>{esc(g["display_datetime"](row["uploaded_at"]))}</small></li>')
        limit='<p>显示最近 10 个批次。</p>' if result['batches']>10 else ''
        return (consistency+'<div data-student-summary-fact data-business-pending-reviews>'
            '<span>待审核材料</span>'
            f'<strong>{result["pending"]} 份 · {result["batches"]} 个批次</strong>'
            '<p>上传尚未审核生效，工作流不会因此自动完成。</p>'
            f'<ul>{"".join(links)}</ul>{limit}</div>')

    def business_change_visa_consistency_issues(self,*,student_id=None,case_id=None):
        g=self.business_change_runtime()
        if (not self.business_changes_enabled() or not g['can_access_module'](self.user,'visa')
                or not g['allowed'](self.user,'students.view')):return ()
        from visa_business_state import visa_consistency_issues
        from business_changes.snapshots import read_scope
        with read_scope(g['db']) as conn:
            return visa_consistency_issues(conn,student_id=student_id,case_id=case_id)

    def business_change_unassigned_visa_html(self,*,student_id):
        g=self.business_change_runtime()
        if (not self.business_changes_enabled() or not g['can_access_module'](self.user,'visa')
                or not g['allowed'](self.user,'students.view')):return ''
        from visa_business_state import unassigned_student_case_ids
        from business_changes.snapshots import read_scope
        from urllib.parse import quote
        with read_scope(g['db']) as conn:identifiers=unassigned_student_case_ids(conn,student_id)
        if not identifiers:return ''
        esc=g['esc'];root=f'/students/{quote(str(student_id),safe="")}/visa-cases'
        links=''.join(f'<li><a href="{esc(root+"/"+str(int(identifier))+"?visa_tab=visa_result")}">Visa Case #{int(identifier)}</a></li>'
            for identifier in identifiers[:5])
        more=f'<p><a href="{esc(root)}">查看全部 {len(identifiers)} 个案件</a></p>' if len(identifiers)>5 else ''
        return (f'<section class="panel" role="status" data-business-unassigned-visa><h3>签证案件待关联申请周期 · {len(identifiers)} 个</h3>'
            '<p>先核实案件与申请周期归属，再通过正式流程关联；系统不会自动归入当前申请。</p>'
            f'<ul>{links}</ul>{more}</section>')

    def business_change_visa_consistency_html(self,*,student_id=None,case_id=None,compact=False):
        from urllib.parse import quote
        g=self.business_change_runtime();esc=g['esc'];issues=self.business_change_visa_consistency_issues(student_id=student_id,case_id=case_id)
        if not issues:return ''
        rows=[]
        for issue in issues:
            url=f'/students/{quote(str(issue["student_id"]),safe="")}/visa-cases/{issue["case_id"]}?visa_tab=visa_result'
            rows.append(f'<li data-business-visa-review-case="{issue["case_id"]}"><a href="{esc(url)}">{esc(issue["student_name"])} · {esc(issue["student_id"])} · Case #{issue["case_id"]}</a>：{esc(issue["message"])}</li>')
        content = (f'<section class="panel" data-business-visa-review role="status"><h3>签证材料需核实 · {len(issues)} 个案件</h3>'
            '<p>原人工确认和周期关闭记录已保留；请核对有效材料或使用正式更正流程。</p>'
            f'<ul>{"".join(rows)}</ul></section>')
        if compact:
            return (f'<details class="visa-management-review-details"><summary>签证材料需核实 · {len(issues)} 个案件 · 展开查看</summary>'
                    + content + '</details>')
        return content

    def business_change_student_summary_snapshot(self,*,tab=None):
        g=self.business_change_runtime()
        if not g['can_access_module'](self.user,'students') or not g['allowed'](self.user,'students.view'):
            return self.json_response({'ok':False,'code':'forbidden'},403)
        student_id=str((self.query.get('student_id') or [''])[0])
        try:
            scope=Subject('student',student_id)
            minimum=int((self.query.get('minimum_version') or ['0'])[0])
            if minimum<0:raise ValueError('invalid_revision')
        except (ValueError,TypeError):return self.json_response({'ok':False,'code':'invalid_scope_or_revision'},400)
        cycle=(self.query.get('application_cycle_id') or [None])[0]
        def load(conn,allowed):
            if scope not in allowed:raise PermissionError('student_scope_forbidden')
            if conn.execute('SELECT 1 FROM students WHERE student_id=? AND deleted_at IS NULL',(student_id,)).fetchone() is None:
                raise LookupError('student_not_found')
            if cycle is not None:
                if not str(cycle).isdigit() or len(str(cycle))>18 or not conn.execute(
                    'SELECT 1 FROM application_cycles WHERE id=? AND student_id=?',(int(cycle),student_id)).fetchone():
                    raise LookupError('cycle_not_found')
            html=(self.student_detail_tab('GET',student_id,tab,fragment_only=True) if tab else
                  self.student_detail('GET',student_id,fragment_only=True))
            if not isinstance(html,str):raise ValueError('student_summary_unavailable')
            return {'fragments':{('student-tab-'+tab if tab else 'student-summary'):html}}
        try:
            if not self.business_changes_enabled():return self.json_response({'ok':False,'code':'sync_not_enabled'},503)
            result=read_snapshot(g['db'],(scope,),can_read=lambda subject:subject==scope
                and g['can_access_module'](self.user,'students') and g['allowed'](self.user,'students.view'),
                load=load,minimum_versions={scope:minimum})
        except PermissionError:return self.json_response({'ok':False,'code':'forbidden'},403)
        except LookupError:return self.json_response({'ok':False,'code':'student_or_cycle_not_found'},404)
        except MinimumVersionUnavailable:return self.json_response({'ok':False,'code':'revision_not_visible'},409)
        except SchemaUnavailable:return self.json_response({'ok':False,'code':'sync_schema_unavailable'},503)
        return self.json_response(result)

    def business_change_visa_hidden_fields(self,case_id):
        if not self.business_changes_enabled():return ''
        g=self.business_change_runtime()
        if not g['can_workflow_module_save'](self.user,'Visa'):return ''
        conn=g['db'].connect()
        try:version=version_of(conn,Subject('visa_case',str(int(case_id))))
        finally:g['db']._release_connection(conn)
        from business_changes.commands import digest
        nonce=digest(['visa-lodgement-form',str(int(case_id)),version,self.user['id'],self.csrf])[:32]
        return (f'<input type="hidden" name="business_command_key" value="{nonce}">'
                f'<input type="hidden" name="business_expected_version" value="{version}">')

    def business_change_visa_overview_page(self):
        g=self.business_change_runtime();scope=Subject('dashboard','visa_cases')
        if not g['can_access_module'](self.user,'visa'):
            return self.deny_module_access('visa')
        def load(conn,allowed):
            if scope not in allowed:raise PermissionError('visa_scope_forbidden')
            return {'fragments':{'visa-overview':self.visa_management('GET',fragment_only=True)}}
        try:
            packet=read_snapshot(g['db'],(scope,),can_read=lambda subject:subject==scope and g['can_access_module'](self.user,'visa'),load=load)
        except PermissionError:return self.deny_module_access('visa')
        return self.send_html(g['page']('Visa Management',packet['data']['fragments']['visa-overview'],
            self.user,self.csrf,'visa_management',g['nas'].status()))

    def business_change_visa_lodgement(self,student_id,case_id,return_path):
        g=self.business_change_runtime()
        if not g['can_access_module'](self.user,'visa') or not g['can_workflow_module_save'](self.user,'Visa'):
            return self.deny_module_access('visa')
        nonce=str(self.form.get('business_command_key') or '')
        try:
            if not re.fullmatch(r'[0-9a-f]{32}',nonce):raise ValueError('页面已更新，请刷新后再确认递交。')
            expected=int(self.form.get('business_expected_version',''))
            if expected<0:raise ValueError('页面版本无效，请刷新后重试。')
            from business_changes.visa import submit_lodgement
            def dates(conn,cid,value,actor,stamp):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='visa_processing_date_revisions'").fetchone():
                    from visa_processing_dates import correct_processing_dates
                    correct_processing_dates(conn,cid,{'acknowledge_sent_on':value},actor_user_id=actor,occurred_at=stamp)
            def authorize(conn,case):
                if not g['can_workflow_module_save'](self.user,'Visa'):raise PermissionError('visa_write_forbidden')
            with g['db'].transaction() as conn:
                if not enabled(conn):raise ValueError('业务同步模式已变化，请刷新后重试。')
                submit_lodgement(conn,student_id=str(student_id),case_id=int(case_id),
                    requested_cycle_id=self.form.get('application_cycle_id'),actual_date=str(self.form.get('actual_lodgement_date') or ''),
                    actor=self.user,idempotency_key=nonce,expected_version=expected,occurred_at=g['now_iso'](),
                    registry=Registry([]),authorize=authorize,after_write=g['process_revenue_visa_change'],record_dates=dates,
                    client_ip=self.client_address[0],user_agent=self.headers.get('User-Agent'))
        except PermissionError:return self.deny_module_access('visa')
        except (ValueError,SchemaUnavailable) as exc:return self.flash_redirect(return_path,str(exc),'error')
        return self.flash_redirect(return_path,'VISA Application Submitted 已记录。')

    def business_change_store_visa_material(self,case,item,upload,folder,file_type):
        """One verified file + source/link/checklist/registered evidence + events."""
        from business_changes.commands import Mutation
        from business_changes.domain import run_domain_write
        from business_changes.files import persist_verified_file
        from business_changes.visa_domains import case_snapshot,case_scopes
        g=self.business_change_runtime()
        if not g['can_workflow_module_save'](self.user,'Visa'):
            raise PermissionError('你没有保存 Visa 材料的权限。')
        case_id=int(case['id']);student_id=str(case['student_id']);item_id=int(item['id'])
        destination,version=g['next_version_path'](folder,upload.filename)
        prepared=self.business_change_prepare_upload_review('Visa',file_type,upload,'0')
        def save(path,digest):
            with g['db'].transaction() as conn:
                if not enabled(conn):raise ValueError('业务同步模式已变化，请刷新后重试。')
                current=conn.execute('SELECT * FROM visa_cases WHERE id=? AND student_id=? AND archived_at IS NULL',(case_id,student_id)).fetchone()
                if current is None:raise ValueError('Visa Case 不存在或已归档。')
                if conn.execute('SELECT 1 FROM visa_checklist_items WHERE id=? AND visa_case_id=?',(item_id,case_id)).fetchone() is None:
                    raise ValueError('材料项不属于当前 Visa Case。')
                current=dict(current)
                result_item=self.visa_material_saved_item_category(current,item)=='Visa Result / S56' or file_type!='other'
                document_type=self.visa_lodgement_document_type(file_type) if result_item else 'VisaMaterialChecklist'
                def capture(view):
                    return (case_snapshot(view,case_id),
                        tuple(tuple(r) for r in view.execute('SELECT * FROM visa_case_document_links WHERE visa_case_id=? ORDER BY id',(case_id,))),
                        tuple(tuple(r) for r in view.execute('SELECT * FROM visa_checklist_items WHERE id=?',(item_id,))))
                def operation(view):
                    did=view.execute('''INSERT INTO student_documents(student_id,folder_category,document_type,original_name,
                        stored_name,file_path,version,file_size,uploaded_by,uploaded_at,is_active,document_status)
                        VALUES(?,'Visa Management',?,?,?,?,?,?,?,?,1,'active')''',
                        (student_id,document_type,upload.filename,path.name,str(path),version,len(upload.data),self.user['id'],g['now_iso']())).lastrowid
                    self.visa_material_insert_document_link(current,item,did,connection=view)
                    if result_item:
                        self.visa_lodgement_sync_material_upload_to_visa_tab(current,item,did,upload.filename,connection=view,
                            prepared_bytes=prepared['visa_bytes'],prepared_text=prepared['visa_text'])
                    else:self._visa_register_uploaded_document(current,did,connection=view,extract_letter=False,
                        prepared_bytes=prepared['visa_bytes'])
                    view.execute("UPDATE visa_checklist_items SET status='uploaded',updated_at=? WHERE id=? AND visa_case_id=?",
                        (g['now_iso'](),item_id,case_id))
                    return did
                document_id=run_domain_write(conn,event_type='visa.material_uploaded',actor_kind='user',actor_ref=str(self.user['id']),
                    capture=capture,operation=operation,
                    describe=lambda view,result,before,after:Mutation('visa.material_uploaded',Subject('visa_case',str(case_id)),
                        (*case_scopes(view,current),Subject('document',str(result)))))
            return document_id
        return persist_verified_file(destination,upload.data,save)

    def business_change_prepare_upload_review(self,module,document_type,upload,index):
        """Slow inference and immutable tuition storage never hold a writer."""
        from pathlib import Path
        g=self.business_change_runtime();database=g['db']
        state=getattr(database,'_request_state',lambda:None)()
        active=(state.get('business_connection') or state.get('connection')) if state else None
        if active is not None and active.in_transaction and not active.execute('PRAGMA query_only').fetchone()[0]:
            raise ValueError('材料识别与附件准备不能在写事务中执行。')
        result={'inferred':{},'tuition_upload':None,'tuition_error':None,'tuition_prepared':False}
        if module=='Offer':result['inferred']=g['infer_offer_details'](upload)
        elif module=='Visa':
            from visa_legacy_document_adapter import read_legacy_letter_bytes
            text=''
            if Path(upload.filename).suffix.lower() in {'.pdf','.txt'}:
                try:text=read_legacy_letter_bytes(upload.filename,upload.data,g['PdfReader'])
                except (ValueError,OSError,UnicodeError):pass
            result.update(visa_bytes=upload.data,visa_text=text,
                inferred=g['infer_visa_details'](upload,text=text))
        elif module=='COE + OSHC':
            kind=g['coe_kind_from_review'](document_type)
            result['inferred']=g['infer_coe_oshc_details'](upload,kind)
            if kind in {'COE','COE_CAAW'}:
                from reviewed_upload_workflow import prepare_coe_identity
                result['inferred']['_reviewed_student_identity']=prepare_coe_identity(upload.data)
            result['tuition_prepared']=True
            if kind in {'COE','COE_CAAW'} and self.form.get(f'intake_defer_{index}')!='1':
                try:
                    from revenue.student_intake_tuition import read_review_fields
                    from revenue.school_courses import read_course_fields
                    course_values=read_course_fields(self.form,index)
                    values=course_values if course_values is not None else read_review_fields(self.form,index)
                    if values is not None:
                        from revenue.first_year_tuition import OFFER_EXTENSIONS
                        from revenue.tuition_storage import TuitionEvidenceStorage
                        result['tuition_upload']=TuitionEvidenceStorage(Path(database.path).parent/'revenue_documents').stage(upload,OFFER_EXTENSIONS-{'.txt'})
                except ValueError as exc:
                    result['tuition_error']=str(exc)
        return result

    def business_change_confirm_application_upload(self,student,upload,item_id,document_type,target_name,notes,stored_form):
        return self.business_change_confirm_upload_review(student,upload,item_id,document_type,target_name,notes,stored_form)

    def business_change_review_failure(self,student_id,module,token,error):
        """Describe persisted batch facts after a per-file failure, never guess."""
        import logging
        from urllib.parse import quote
        from business_changes.snapshots import read_scope
        g=self.business_change_runtime()
        logging.getLogger(__name__).error('Upload Review confirmation did not finish',
            exc_info=(type(error),error,error.__traceback__))
        with read_scope(g['db']) as conn:
            from business_changes.authorization import current_staff
            try:current=current_staff(conn,self.user['id'])
            except PermissionError:return self.error_page('员工权限已变化，请重新登录后核对材料。',403)
            if not g['can_review_module_action'](current,module,'confirm'):
                return self.error_page('员工权限已变化，请重新登录后核对材料。',403)
            counts=dict(conn.execute('''SELECT i.status,count(*) FROM upload_review_items i
                JOIN upload_sessions s ON s.id=i.upload_session_id AND s.student_id=i.student_id AND s.module_key=i.module_key
                WHERE s.session_token=? AND s.student_id=? AND s.module_key=? GROUP BY i.status''',
                (token,str(student_id),module)))
        saved=counts.get('Confirmed',0);pending=counts.get('NeedsReview',0)
        message=(f'该批次已保存 {saved} 份，{pending} 份仍待审核。保存未全部完成，请核对文件状态后重试。'
                 if pending else f'该批次已保存 {saved} 份。收尾未完成，请核对已保存的文件后再操作。')
        return self.flash_redirect(f'/students/{quote(str(student_id),safe="")}/upload-review/{quote(str(token),safe="")}',message,'error')

    def business_change_update_review_state(self,student_id,module_key,item_ids,action):
        from business_changes.upload_review import update_review_state
        g=self.business_change_runtime();database=g['db']
        def authorize(view,review):
            from business_changes.authorization import current_staff
            user=current_staff(view,self.user['id'])
            if not g['can_review_module_action'](user,module_key,'confirm'):
                raise PermissionError('员工权限已变化，不能更新材料审核状态。')
        with database.transaction() as conn:
            for item_id in dict.fromkeys(int(value) for value in item_ids if value):
                update_review_state(conn,item_id=item_id,student_id=student_id,module_key=module_key,
                    action=action,actor=self.user,occurred_at=g['now_iso'](),authorize=authorize)

    def business_change_confirm_upload_review(self,student,upload,item_id,document_type,target_name,notes,stored_form,
            *,module_key='Applications',business_after=None,request_context=None):
        """Prepare verified file/previews first; commit the whole review next."""
        import hashlib
        import os
        from pathlib import Path
        from business_changes.files import persist_verified_file
        from business_changes.upload_review import confirm_application_document
        import upload_review_confirmation_coordinator as coordinator
        g=self.business_change_runtime();database=g['db'];student_id=str(student['student_id'])
        if module_key not in g['MODULE_REVIEW_CONFIG']:raise ValueError('未知材料审核模块。')
        if module_key!='Applications' and business_after is None:raise ValueError('当前模块缺少明确的业务持久化适配。')
        category=g['MODULE_REVIEW_CONFIG'][module_key]['category']
        if not item_id:raise ValueError('材料缺少持久化审核记录，请重新建立 Upload Review。')
        item=database.one('SELECT * FROM upload_review_items WHERE id=? AND student_id=? AND module_key=?',(int(item_id),student_id,module_key))
        if item is None:raise ValueError('材料审核记录不属于当前学生。')
        file_hash=hashlib.sha256(upload.data).hexdigest()
        def fresh_actor(view):
            from business_changes.authorization import current_staff
            return current_staff(view,self.user['id'])
        def authorize(view,review):
            actor=fresh_actor(view)
            if not g['can_review_module_action'](actor,module_key,'confirm'):
                raise PermissionError('你没有确认当前模块材料的权限。')
            if module_key=='COE + OSHC' and any(str(key).startswith('course_mode_') and value for key,value in self.form.items()):
                with database.business_scope(view):
                    if not (g['can_workflow_module_save'](actor,'Offer')
                            and g['can_perform_revenue_action'](actor,'cases/tuition') and g['allowed'](actor,'coe.manage')):
                        raise PermissionError('确认课程入读及学费需要Offer、CoE及课程财务权限。')
            if document_type not in g['module_document_types'](module_key):
                raise ValueError('请选择当前模块允许的 DocumentType。')
        def command(conn,path,version,after):
            return confirm_application_document(conn,item_id=int(item_id),student_id=student_id,
                document_type=document_type,target_name=target_name,notes=notes,original_name=upload.filename,
                file_sha256=file_hash,file_size=len(upload.data),destination=str(path),file_version=version,
                actor=self.user,occurred_at=g['now_iso'](),request_context=stored_form if request_context is None else request_context,
                authorize=authorize,after_document=after,module_key=module_key)
        def retained(document_id):
            document=database.one('SELECT * FROM student_documents WHERE id=? AND student_id=?',(document_id,student_id))
            if document is None:raise ValueError('原确认文件记录不存在，请核对材料列表。')
            path=Path(document['file_path'])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=file_hash:
                raise ValueError('原确认文件不可访问或内容已变化，请核对原始文件。')
            return int(document_id),path,int(document['version'])
        if item['status']=='Confirmed':
            with database.transaction() as conn:
                result=command(conn,item['final_file_path'],1,lambda view,did:None)
            return retained(result.result['document_id'])
        if not g['can_review_module_action'](self.user,module_key,'confirm'):
            raise PermissionError('你没有确认当前模块材料的权限。')
        destination,version=g['next_version_path'](g['nas'].category_folder(student,category),
            g['classified_filename'](document_type,upload.filename,target_name))
        prepared=[];promoted=[]
        def cleanup(paths):
            failed=[]
            for path in reversed(paths):
                try:path.unlink(missing_ok=True)
                except OSError:failed.append(str(path))
            if failed:raise RuntimeError('材料缓存清理失败：'+'；'.join(failed))
        def save(path,digest):
            if digest!=file_hash:raise ValueError('材料文件校验值已变化。')
            key='prepared_'+uuid.uuid4().hex
            for size in ('thumb','large'):
                target=g['preview_target'](key,size)
                prepared.append(target)
                rendered=Path(g['make_preview'](key,path.name,upload.data,upload.content_type,size=size))
                if rendered.resolve()!=target.resolve() or not target.is_file():
                    raise ValueError('材料预览未保存到预期位置。')
            def after(view,document_id):
                for size,source_path in zip(('thumb','large'),prepared):
                    final=g['preview_target'](document_id,size)
                    os.link(source_path,final)  # Atomic exclusive promotion; never overwrites an old cache.
                    promoted.append(final)
                view.execute('UPDATE student_documents SET preview_path=? WHERE id=?',
                    (str(g['preview_target'](document_id,'thumb')),document_id))
                business_record_id=None
                previous_actor=self.user
                self.user=fresh_actor(view)
                try:
                    with database.business_scope(view):
                        if business_after is not None:
                            business_record_id=business_after(view,document_id,path)
                        coordinator.coordinate_confirmed_upload(database.path,stored_form=stored_form,
                            confirmed_upload=coordinator.ConfirmedUpload(module_key,document_id,business_record_id),
                            actor_user_id=int(self.user['id']),actor_permissions=g['get_permissions'](self.user),
                            flags=self.transfer_case_flags(),caller_transaction=view)
                finally:
                    self.user=previous_actor
                return business_record_id
            with database.transaction() as conn:
                result=command(conn,path,version,after)
            return result
        try:
            result=persist_verified_file(destination,upload.data,save)
        except BaseException as exc:
            try:cleanup([*prepared,*promoted])
            finally:
                if isinstance(exc,Exception):
                    try:self.business_change_update_review_state(student_id,module_key,[item_id],'failed')
                    except Exception:
                        exc.add_note('材料失败状态未能单独持久化，请核对审核记录后重试。')
            raise
        # Cleanup after a successful DB commit is outside file compensation:
        # cleanup failure must never delete a now-referenced original file.
        cleanup([*prepared,*([destination] if result.replayed else [])])
        if result.replayed:return retained(result.result['document_id'])
        return int(result.result['document_id']),destination,version
