"""Parent school confirmation and signed Transfer handoff on the caller's transaction.

Parent text remains evidence until an employee confirms it. No commits, schema
changes, inferred start dates, production paths or writes from read projections.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from uuid import uuid4

from student_lifecycle import transfer_source_authority as authority
from student_lifecycle.enrolment import _event

INTAKE_TERMS = {'02': 'Term 1', '04': 'Term 2', '07': 'Term 3', '10': 'Term 4'}


def _normalized(value):
    return ' '.join(str(value or '').split()).casefold()


def _dicts(conn, sql, parameters=()):
    cursor = conn.execute(sql, parameters)
    names = [col[0] for col in cursor.description]
    return [dict(zip(names, tuple(row))) for row in cursor]


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def _transaction(conn):
    if not conn.in_transaction:
        raise ValueError('转学衔接必须在调用方事务内执行。')


def parent_school_preview(conn, student_id, parent_form_token, *, require_school=True):
    """Return an exact, read-only parent-school candidate; ambiguity stays visible."""
    links = _dicts(conn, """SELECT student_id,intake_type,submitted_at,invalidated_at
        FROM parent_form_tokens WHERE token=?""", (parent_form_token,))
    if (len(links) != 1 or str(links[0]['student_id']) != str(student_id)
            or links[0]['intake_type'] != 'new_student_transfer'
            or not links[0]['submitted_at'] or links[0]['invalidated_at']):
        raise ValueError('需要属于该学生且已递交的新学生转学家长资料。')
    # Student profile JSON is the latest submitted/edited profile, not an archive
    # per token. Never relabel that profile as an older intake's own snapshot.
    if conn.execute('SELECT julianday(?)', (links[0]['submitted_at'],)).fetchone()[0] is None:
        raise ValueError('家长资料缺少有效递交时间，请先核对。')
    newer = conn.execute("""SELECT 1 FROM parent_form_tokens
        WHERE student_id=? AND token<>? AND submitted_at IS NOT NULL
          AND julianday(submitted_at)>=julianday(?) LIMIT 1""",
        (student_id, parent_form_token, links[0]['submitted_at'])).fetchone()
    if newer:
        raise ValueError('所选家长资料已有更新的转学递交，不能将当前学生资料当作旧表单快照；请核对本次来源。')
    students = _dicts(conn, 'SELECT form_data_json FROM students WHERE student_id=?', (student_id,))
    if len(students) != 1:
        raise ValueError('学生不存在。')
    form = json.loads(students[0]['form_data_json'] or '{}')
    name = str(form.get('current_school_name') or '').strip()
    year = str(form.get('current_school_year_level') or form.get('current_year_level') or '').strip()
    if require_school and (not name or year not in {f'Year {n}' for n in range(1, 13)}):
        raise ValueError('家长资料缺少有效的当前学校或年级，请先核对。')
    matches = [row for row in _dicts(conn, """SELECT id,school_name,state,region,school_type,sector_key
        FROM schools WHERE COALESCE(deleted_at,'')=''""")
        if _normalized(row['school_name']) == _normalized(name)]
    snapshot = {'student_id': str(student_id), 'parent_form_token': parent_form_token,
                'submitted_at': links[0]['submitted_at'], 'school_name': name, 'year_level': year}
    return {'parent_school_name': name, 'year_level': year, 'school_matches': matches,
            'suggested_school_id': matches[0]['id'] if len(matches) == 1 else None,
            'snapshot': snapshot, 'snapshot_digest': _digest(snapshot), 'form': form}


def confirm_staff_current(conn, *, student_id, school_id, school_name, year_level,
                          actor_user_id, occurred_at, parent_evidence=None):
    """Employee's explicit school selection; preserve any conflicting old facts."""
    _transaction(conn)
    authority._audit(conn, actor_user_id, occurred_at)
    if year_level not in {f'Year {n}' for n in range(1, 13)}:
        raise ValueError('需要有效的当前年级。')
    if school_id is not None:
        rows = _dicts(conn, "SELECT id,school_name FROM schools WHERE id=? AND COALESCE(deleted_at,'')=''", (school_id,))
        if len(rows) != 1 or _normalized(rows[0]['school_name']) != _normalized(school_name):
            raise ValueError('学校名称与所选学校编号不一致。')
        school_name = rows[0]['school_name']
    elif not str(school_name or '').strip():
        raise ValueError('需要确认学校名称。')
    current = _dicts(conn, """SELECT * FROM student_school_enrolments WHERE student_id=?
        AND enrolment_role='current' AND status='active'""", (student_id,))
    if len(current) > 1:
        raise ValueError('存在多条当前在读记录，请先核对。')
    if current:
        row = current[0]
        if (row['school_id'] not in (None, school_id) or _normalized(row['school_name_snapshot']) != _normalized(school_name)
                or (str(row['year_level_snapshot'] or '').strip() and row['year_level_snapshot'] != year_level)):
            raise ValueError('已有当前在读记录与本次确认不同；未覆盖原记录。')
        enrolment_id = row['id']
        school_name = row['school_name_snapshot']
        # An explicit confirmation may fill missing legacy audit metadata, while
        # preserving the existing school, year, dates and relationship identity.
        conn.execute("""UPDATE student_school_enrolments SET
            school_id=COALESCE(school_id,?),
            year_level_snapshot=CASE WHEN trim(COALESCE(year_level_snapshot,''))='' THEN ? ELSE year_level_snapshot END,
            confirmed_by=COALESCE(confirmed_by,?),confirmed_at=COALESCE(confirmed_at,?),
            confirmation_reason=COALESCE(confirmation_reason,?),updated_by=?,updated_at=?
            WHERE id=?""", (school_id, year_level, actor_user_id, occurred_at, '员工确认新生转学家长资料中的当前学校',
                            actor_user_id, occurred_at, enrolment_id))
    else:
        enrolment_id = conn.execute("""INSERT INTO student_school_enrolments(
            student_id,school_id,school_name_snapshot,enrolment_role,year_level_snapshot,
            status,lifecycle_context,confirmation_reason,confirmed_by,confirmed_at,
            created_by,created_at,updated_by,updated_at)
            VALUES(?,?,?,'current',?,'active','initial',?,?,?,?,?,?,?)""",
            (student_id, school_id, school_name, year_level, '员工确认新生转学家长资料中的当前学校',
             actor_user_id, occurred_at, actor_user_id, occurred_at, actor_user_id, occurred_at)).lastrowid
    evidence = dict(parent_evidence or {})
    evidence.update(school_id=school_id, confirmed_school_name=school_name, enrolment_id=enrolment_id, year_level=year_level)
    key = (f'parent-transfer-current:{enrolment_id}:{_digest(evidence)}'
           if parent_evidence else f'staff-transfer-current:{enrolment_id}:{_digest(evidence)}')
    _event(conn, event_uid=key, student_id=student_id, enrolment_id=enrolment_id,
           event_type='current_confirmed', actor_user_id=actor_user_id,
           occurred_at=occurred_at, reason='员工确认本次转学的当前学校及年级', new_value=evidence)
    return int(enrolment_id)


def confirm_parent_current(conn, *, student_id, parent_form_token, school_id,
                           school_name, year_level, confirmed, actor_user_id,
                           occurred_at, expected_digest=None):
    _transaction(conn)
    authority._audit(conn, actor_user_id, occurred_at)
    if confirmed is not True:
        raise ValueError('请明确确认家长填写的当前学校及年级。')
    preview = parent_school_preview(conn, student_id, parent_form_token)
    if expected_digest is not None and preview['snapshot_digest'] != expected_digest:
        raise ValueError('家长学校资料已变化，请刷新后重新确认。')
    if year_level != preview['year_level']:
        raise ValueError('确认年级与家长资料不一致，请先核对学生资料。')
    return confirm_staff_current(
        conn, student_id=student_id, school_id=school_id, school_name=school_name,
        year_level=year_level, actor_user_id=actor_user_id, occurred_at=occurred_at,
        parent_evidence={**preview['snapshot'], 'confirmation_source': 'parent_transfer_intake',
                         'snapshot_digest': preview['snapshot_digest']})


def _confirmed_current(conn, student_id, source):
    current = _dicts(conn, """SELECT * FROM student_school_enrolments WHERE student_id=?
        AND enrolment_role='current' AND status='active'""", (student_id,))
    if len(current) != 1 or not current[0]['confirmed_by'] or not current[0]['confirmed_at']:
        raise ValueError('合同已签妥，待工作人员确认当前学校及年级。')
    row = current[0]
    if source['source_classification'] == 'aaie_existing':
        if row['id'] != source['current_enrolment_id']:
            raise ValueError('当前在读记录与已确认的转学来源不一致。')
        return row, None
    preview = parent_school_preview(conn, student_id, source['parent_form_token'])
    events = _dicts(conn, """SELECT new_value_json FROM school_enrolment_events
        WHERE student_id=? AND enrolment_id=? AND event_type='current_confirmed'
        AND json_extract(new_value_json,'$.confirmation_source')='parent_transfer_intake'
        ORDER BY id DESC""", (student_id, row['id']))
    if not events:
        raise ValueError('合同已签妥，待确认本次家长资料中的当前学校及年级。')
    evidence = json.loads(events[0]['new_value_json'] or '{}')
    if (evidence.get('snapshot_digest') != preview['snapshot_digest']
            or evidence.get('school_id') != row['school_id']
            or _normalized(evidence.get('confirmed_school_name')) != _normalized(row['school_name_snapshot'])
            or row['year_level_snapshot'] != preview['year_level']):
        raise ValueError('学校或家长资料已变化，请重新确认当前学校及年级。')
    return row, preview


def _require_completed_fee(conn, contract_id, cycle_id):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'revenue_module_state' not in tables:
        return  # Compatibility for lifecycle-only deployments without Revenue.
    state = conn.execute('SELECT status FROM revenue_module_state WHERE id=1').fetchone()
    if not state or state[0] != 'active':
        raise ValueError('财务模块尚未启用，合同已签妥；启用后请重试转学收费建档。')
    count = conn.execute("""SELECT count(DISTINCT f.id)
        FROM revenue_fee_items f JOIN revenue_fee_item_contract_links l ON l.fee_item_id=f.id
        JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id
        WHERE l.contract_id=? AND c.application_cycle_id=? AND f.status<>'cancelled'""",
        (contract_id, cycle_id)).fetchone()[0]
    if count != 1:
        raise ValueError('转学收费未生成或存在冲突，请核对收费规则后重试；合同签署已保留。')


def handoff_signed_transfer(conn, contract_id, *, actor_user_id, occurred_at):
    """Atomically attach a signed contract, one Transfer cycle and its service fee."""
    from student_lifecycle.transfer_v2_service import TransferV2Service
    from student_lifecycle.transfer_v2_models import TransferV2CreateInput
    from revenue.operational_integration import process_revenue_contract_completion
    from revenue.parent_guardian_intake import complete_deferred_guardian_fee
    _transaction(conn)
    authority._audit(conn, actor_user_id, occurred_at)
    contracts = _dicts(conn, 'SELECT * FROM contracts WHERE id=?', (contract_id,))
    if len(contracts) != 1:
        raise ValueError('转学合同不存在。')
    contract = contracts[0]; sid = str(contract['student_id'])
    if (contract['contract_type'] != 'TransferApplicationContract'
            or contract['contract_status'] != 'FullyExecuted'
            or contract['deleted_at'] or contract['voided_at']):
        raise ValueError('需要本学生有效的双方签妥转学合同。')
    if contract['application_cycle_id'] is not None:
        source = authority.resolve_for_contract(conn, sid, contract_id)
        cid = int(contract['application_cycle_id'])
        rows = _dicts(conn, """SELECT c.source_student_case_id FROM application_cycles c
            JOIN transfer_cases t ON t.case_id=c.source_student_case_id
            WHERE c.id=? AND c.student_id=? AND c.cycle_type='transfer'
              AND t.source_contract_id=?""", (cid, sid, contract_id))
        if (len(rows) != 1 or source['application_cycle_id'] != cid
                or source['lifecycle_state'] != 'consumed'):
            raise ValueError('已关联的合同与转学轮次归属不一致。')
        process_revenue_contract_completion(conn, contract_id, actor_user_id, occurred_at)
        _require_completed_fee(conn, contract_id, cid)
        complete_deferred_guardian_fee(conn, sid, cid, source['parent_form_token'], occurred_at=occurred_at)
        return {'cycle_id': cid, 'case_id': rows[0]['source_student_case_id'], 'status': 'completed'}
    source = authority.resolve_for_creation(conn, sid, contract_id)
    defer_current_school = source['source_classification'] == 'new_transfer'
    if defer_current_school:
        preview = parent_school_preview(conn, sid, source['parent_form_token'], require_school=False)
        current = {'school_id': preview['suggested_school_id'],
                   'school_name_snapshot': preview['parent_school_name'],
                   'year_level_snapshot': preview['year_level'] if preview['year_level'] in {f'Year {n}' for n in range(1, 13)} else ''}
    else:
        current, preview = _confirmed_current(conn, sid, source)
    form = preview['form'] if preview else json.loads(_dicts(conn, 'SELECT form_data_json FROM students WHERE student_id=?', (sid,))[0]['form_data_json'] or '{}')
    term = INTAKE_TERMS.get(str(form.get('preferred_intake_term') or ''))
    if term is None:
        raise ValueError('请确认转学入学月份（2/4/7/10月）。')
    current_name = current['school_name_snapshot']
    if current['school_id'] is not None:
        school = conn.execute("SELECT school_name FROM schools WHERE id=? AND COALESCE(deleted_at,'')=''",
                              (current['school_id'],)).fetchone()
        if not school:
            raise ValueError('当前在读学校在学校库中已不可用，请先核对。')
        current_name = str(school[0])
    request = TransferV2CreateInput(
        student_id=sid, source_contract_id=int(contract_id), source_label=source['source_classification'],
        current_school_id=current['school_id'], current_school_name=current_name,
        current_year_level=current['year_level_snapshot'], application_year_level=form.get('applying_year_level') or '',
        transfer_year=int(form.get('preferred_intake_year') or 0), transfer_term=term, targets=())
    service = TransferV2Service(':memory:', clock=lambda: occurred_at)
    overview = service.create_cycle(request, actor_user_id=actor_user_id,
        idempotency_key=f'signed-transfer-handoff:{int(contract_id)}', connection=conn,
        defer_current_school=defer_current_school)
    _require_completed_fee(conn, contract_id, overview.cycle_id)
    complete_deferred_guardian_fee(conn, sid, overview.cycle_id, source['parent_form_token'], occurred_at=occurred_at)
    return {'cycle_id': overview.cycle_id, 'case_id': overview.case_id, 'status': 'completed'}


def attempt_signed_transfer(conn, contract_id, *, actor_user_id, occurred_at):
    """Keep the signed fact; isolate downstream failure and retain a retry record."""
    _transaction(conn)
    rows = _dicts(conn, 'SELECT student_id FROM contracts WHERE id=?', (contract_id,))
    if len(rows) != 1:
        raise ValueError('转学合同不存在。')
    previous_scope = _dicts(conn, 'SELECT request_status FROM external_transfer_case_requests WHERE student_id=? AND contract_id=?',
                            (rows[0]['student_id'], contract_id))
    if previous_scope and previous_scope[0]['request_status'] == 'cancelled':
        return {'status':'failed', 'case_id':None, 'message':'原转学请求已取消，请核对业务后建立新的转学申请。'}
    name = 'transfer_handoff_' + uuid4().hex
    conn.execute('SAVEPOINT ' + name)
    try:
        result = handoff_signed_transfer(conn, contract_id, actor_user_id=actor_user_id, occurred_at=occurred_at)
    except Exception as error:
        # This boundary deliberately preserves the completed signature even for
        # unexpected downstream failures; the failure is visible and retryable.
        conn.execute('ROLLBACK TO SAVEPOINT ' + name)
        result = {'status': 'failed', 'message': str(error)[:500], 'case_id': None}
    finally:
        conn.execute('RELEASE SAVEPOINT ' + name)
    key = f'signed-transfer-handoff:{int(contract_id)}'
    previous = _dicts(conn, 'SELECT id,request_status FROM external_transfer_case_requests WHERE student_id=? AND contract_id=?', (rows[0]['student_id'], contract_id))
    if previous and previous[0]['request_status'] == 'completed' and result['status'] == 'completed':
        return result
    code = None if result['status'] == 'completed' else 'transfer_handoff_pending_review'
    values = (result['status'], result.get('case_id'), code, result.get('message'), occurred_at,
              occurred_at if result['status'] == 'completed' else None)
    if previous:
        conn.execute('''UPDATE external_transfer_case_requests SET request_status=?,case_id=?,
            last_error_code=?,last_error_message=?,updated_at=?,completed_at=?,attempt_count=attempt_count+1
            WHERE id=?''', (*values, previous[0]['id']))
    else:
        conn.execute('''INSERT INTO external_transfer_case_requests(student_id,contract_id,idempotency_key,
            request_status,case_id,last_error_code,last_error_message,updated_at,completed_at,attempt_count,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,1,?)''', (rows[0]['student_id'], contract_id, key, *values, occurred_at))
    return result
