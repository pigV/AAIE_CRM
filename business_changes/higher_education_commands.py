"""Sealed staff form intents over the existing Higher Education source ports."""
import json
import re

from .commands import Command,CommandConflict,Mutation,Outcome,command_request_hash,digest,execute_command,_domain_owner
from .contracts import Subject
from .higher_education import _cycle
from .registry import Registry


ACTIONS={
    'create_plan':('application_cycle',None),
    'add_item':('he_plan','SELECT application_cycle_id FROM higher_education_plans WHERE id=?'),
    'confirm_plan':('he_plan','SELECT application_cycle_id FROM higher_education_plans WHERE id=?'),
    'remove_item':('he_plan_item','SELECT p.application_cycle_id FROM higher_education_plan_items i JOIN higher_education_plans p ON p.id=i.plan_id WHERE i.id=?'),
    'submit_item':('he_plan_item','SELECT p.application_cycle_id FROM higher_education_plan_items i JOIN higher_education_plans p ON p.id=i.plan_id WHERE i.id=?'),
    'record_offer':('he_application','SELECT application_cycle_id FROM higher_education_applications WHERE id=?'),
    'accept_offer':('he_offer','SELECT application_cycle_id FROM higher_education_offers WHERE id=?'),
    'link_coe':('he_application','SELECT application_cycle_id FROM higher_education_applications WHERE id=?'),
}


def save_action(connection,*,action,identifier,actor,nonce,expected_version,payload,occurred_at,authorize,operation):
    connection=_domain_owner(connection)
    if action not in ACTIONS:raise ValueError('unsupported_higher_education_action')
    if not isinstance(nonce,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',nonce):raise ValueError('缺少保存凭据，请刷新后重试。')
    if type(expected_version) is not int or expected_version<0:raise ValueError('业务版本无效，请刷新后重试。')
    identifier=int(identifier)
    if identifier<1:raise ValueError('业务记录编号无效。')
    kind,owner_query=ACTIONS[action];source=Subject(kind,str(identifier))
    command=Command('higher_education_staff_'+action,'user',str(actor['id']),
        digest([nonce,source.kind,source.identifier]),{'action':action,'id':identifier,'fields':payload})
    expectations={source:expected_version}
    previous=connection.execute('SELECT request_hash,result_json FROM business_change_commands WHERE command_key=?',(command.key,)).fetchone()
    # A hard-deleted draft item still has a replayable receipt. The seal must
    # match before its retained owner is used, then current access is checked.
    if previous and previous[0]!=command_request_hash(command,expectations):raise CommandConflict('idempotency_key_content_conflict')
    retained=json.loads(previous[1]) if previous else None
    ownership={}
    def check(view):
        authorize(view)
        row=view.execute(owner_query,(identifier,)).fetchone() if owner_query else (identifier,)
        if row is None:
            if not retained:raise ValueError('业务记录不存在或已删除。')
            cycle_id=int(retained['cycle_id'])
        else:cycle_id=int(row[0])
        cycle=_cycle(view,cycle_id)
        if cycle.get('education_stream')!='higher_education':raise ValueError('当前周期不属于高等教育申请。')
        if not retained and cycle.get('status')!='active':
            from higher_education_repository import HigherEducationValidationError
            raise HigherEducationValidationError('申请周期已关闭，请刷新后核对当前办理周期。')
        if retained and (int(retained['cycle_id'])!=cycle_id or str(retained['student_id'])!=str(cycle['student_id'])):
            raise ValueError('业务记录所属学生或周期已改变。')
        ownership.update(cycle_id=cycle_id,student_id=str(cycle['student_id']))
    def write(view):
        operation(view)
        return Outcome({**ownership,'changed':True},(Mutation('higher_education.staff_action_saved',source,(
            Subject('application_cycle',str(ownership['cycle_id'])),Subject('student',ownership['student_id']),
            Subject('module','higher_education'),Subject('dashboard','workflow'),Subject('dashboard','overview'))),))
    return execute_command(connection,command,registry=Registry([]),authorize=check,operation=write,
        expected_versions=expectations,occurred_at=occurred_at)
