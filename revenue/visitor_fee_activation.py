"""Explicit Admin activation of the approved AUD900 visitor policy; no runtime fallback."""
import json
from revenue.repository import RevenueRepository

UID='fee-rule-version-2026-visitor-600-v1'
def activate_visitor_600(connection, *, actor_user_id, actor_role, effective_from, idempotency_key):
    if actor_role != 'Admin':
        raise PermissionError('exact Admin role is required')
    from datetime import date
    date.fromisoformat(effective_from)
    repo=RevenueRepository(connection)
    def operation():
        old=connection.execute('SELECT id FROM revenue_fee_rule_versions WHERE uid=?',(UID,)).fetchone()
        if old:
            raise ValueError('Visitor policy already exists; review it instead of overwriting')
        now=repo.now()
        rs=connection.execute('INSERT INTO revenue_fee_rule_sets(uid,name,description,created_by,created_at) VALUES(?,?,?,?,?)',('fee-rule-set-2026-visitor-600','client_fee.visitor-600','600旅游签证基础服务费 AUD900（未含GST）',actor_user_id,now)).lastrowid
        v=connection.execute("""INSERT INTO revenue_fee_rule_versions(uid,rule_set_id,version_no,effective_from,priority,status,student_classification,education_stream,visa_scope,created_by,created_at) VALUES(?,?,1,?,300,'draft','any','any','600',?,?)""",(UID,rs,effective_from,actor_user_id,now)).lastrowid
        for i,(field,value) in enumerate([('service_kind','visitor_600')]):
            connection.execute('INSERT INTO revenue_fee_rule_conditions(uid,rule_version_id,field_name,operator,comparison_value,sort_order,created_at) VALUES(?,?,?,\'eq\',?,?,?)',(f'visitor-600-condition-{i}',v,field,json.dumps(value),i,now))
        connection.execute("""INSERT INTO revenue_fee_rule_milestones(uid,rule_version_id,milestone_code,calculation_kind,fixed_cents,rate_basis_points,due_offset_days,refundable,sort_order) VALUES('visitor-600-milestone',?,'visitor-600','fixed',90000,NULL,0,0,0)""",(v,))
        connection.execute("UPDATE revenue_fee_rule_versions SET status='active',activated_by=?,activated_at=? WHERE id=?",(actor_user_id,now,v))
        return {'uid':UID,'effective_from':effective_from,'net_cents':90000}
    return repo.command(idempotency_key=idempotency_key,command_type='visitor_600_policy_activated',aggregate_type='fee_rule',actor_user_id=actor_user_id,payload={'effective_from':effective_from,'net_cents':90000},operation=operation)
