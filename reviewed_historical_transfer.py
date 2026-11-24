"""Audited ownership for completed transfers retained in their original cycle.

No cycle, case, application, contract or financial row is created or changed.
There is no HTTP write endpoint; an authorized fixed-scope caller owns the
transaction. Display reads revalidate the referenced formal evidence.
"""
from datetime import datetime
import json

ACTION = 'historical_transfer_ownership_confirmed'


def _columns(connection, table):
    return {row[1] for row in connection.execute('PRAGMA table_info('+table+')')}


def _evidence(connection, student_id, cycle_id, offer_id, confirmation_id):
    cycle = connection.execute('SELECT student_id,status,outcome,sequence_no FROM application_cycles WHERE id=?', (cycle_id,)).fetchone()
    if not cycle or str(cycle[0]) != str(student_id) or cycle[1:3] != ('closed', 'completed'):
        raise ValueError('只能确认该学生已完成并关闭的原周期。')
    if 'deleted_at' in _columns(connection, 'students') and not connection.execute(
        'SELECT 1 FROM students WHERE student_id=? AND deleted_at IS NULL', (str(student_id),)).fetchone():
        raise ValueError('学生不存在或已删除。')
    offer = connection.execute('SELECT school_name,offer_status,offer_file_path FROM offers WHERE id=? AND student_id=? AND application_cycle_id=?',
        (offer_id, str(student_id), cycle_id)).fetchone()
    letter = connection.execute('SELECT school_name,document_kind,file_path FROM coe_oshc WHERE id=? AND student_id=? AND application_cycle_id=?',
        (confirmation_id, str(student_id), cycle_id)).fetchone()
    if not offer or str(offer[1]).casefold() != 'accepted' or not letter or letter[1] != 'LetterOfConfirmation':
        raise ValueError('需同学生同原周期的已接受Offer及入学确认信。')
    normal = lambda value: ' '.join(str(value or '').split()).casefold()
    if not normal(offer[0]) or normal(offer[0]) != normal(letter[0]):
        raise ValueError('入学确认信与已接受Offer的学校不一致。')
    if {'legacy_offer_id','id'} <= _columns(connection, 'offer_received_events') and _columns(connection, 'offer_received_event_state_events'):
        for event_id, in connection.execute('SELECT id FROM offer_received_events WHERE legacy_offer_id=?', (offer_id,)):
            state = connection.execute('SELECT state FROM offer_received_event_state_events WHERE offer_received_event_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1', (event_id,)).fetchone()
            if state and str(state[0]).casefold() != 'active':
                raise ValueError('该Offer已撤回或被取代，需重新核对。')
    docs = []
    for path in (offer[2], letter[2]):
        explicit_cycle = 'application_cycle_id' in _columns(connection, 'student_documents')
        guard = ' AND (application_cycle_id IS NULL OR application_cycle_id=?)' if explicit_cycle else ''
        params = (str(student_id), path, cycle_id) if explicit_cycle else (str(student_id), path)
        rows = connection.execute("SELECT id FROM student_documents WHERE student_id=? AND file_path=? AND is_active=1 AND document_status='active' AND voided_at IS NULL" + guard + " ORDER BY id", params).fetchall()
        if len(rows) != 1:
            raise ValueError('原件登记缺失、失效或存在不明确的重复登记。')
        docs.append(int(rows[0][0]))
    return {'cycle_id':int(cycle_id), 'cycle_sequence':int(cycle[3]), 'offer_id':int(offer_id),
        'confirmation_id':int(confirmation_id), 'offer_document_id':docs[0],
        'confirmation_document_id':docs[1], 'target_school':' '.join(str(offer[0]).split())}


def confirm_historical_transfer_ownership(connection, *, student_id, cycle_id,
        offer_id, confirmation_id, actor_id, reason, stamp):
    if not connection.in_transaction:
        raise ValueError('归属确认必须在调用方事务中执行。')
    if not str(reason or '').strip() or datetime.fromisoformat(stamp).tzinfo is None:
        raise ValueError('需提供确认依据及带时区的核对时间。')
    actor = connection.execute('SELECT u.is_active,r.name FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=?', (actor_id,)).fetchone()
    if not actor or actor[0] != 1 or actor[1] not in {'Admin','Manager'}:
        raise PermissionError('仅在职Admin/Manager可确认历史转学归属。')
    if 'deleted_at' in _columns(connection, 'users') and connection.execute('SELECT deleted_at FROM users WHERE id=?', (actor_id,)).fetchone()[0] is not None:
        raise PermissionError('核对人员账号已删除。')
    evidence = _evidence(connection, student_id, cycle_id, offer_id, confirmation_id)
    payload = {'version':1, 'decision':'confirmed', **evidence, 'reason':str(reason).strip()}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    prior = connection.execute("SELECT new_value FROM audit_logs WHERE action_type=? AND module_name='application_cycles' AND student_id=? AND record_id=? ORDER BY id DESC LIMIT 1", (ACTION,str(student_id),str(cycle_id))).fetchone()
    if prior:
        try:
            if json.loads(prior[0]) == payload:
                return False
        except (TypeError,ValueError):
            pass
    connection.execute('''INSERT INTO audit_logs(user_id,action_type,module_name,module_key,record_id,
        student_id,old_value,new_value,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',
        (actor_id,ACTION,'application_cycles','students',str(cycle_id),str(student_id),prior[0] if prior else None,encoded,stamp))
    return True


def load_historical_transfer_ownership(connection, student_ids):
    ids = tuple(dict.fromkeys(str(sid) for sid in student_ids if sid))
    required = {'id','action_type','module_name','student_id','record_id','new_value'}
    if not ids or not required <= _columns(connection, 'audit_logs'):
        return {}
    marks = ','.join('?' for _ in ids)
    rows = connection.execute(f"""SELECT student_id,record_id,new_value FROM audit_logs
        WHERE action_type=? AND module_name='application_cycles' AND student_id IN ({marks}) ORDER BY id DESC""", (ACTION,*ids)).fetchall()
    found = {};seen = set()
    for student_id, record_id, value in rows:
        identity = (str(student_id),str(record_id))
        if identity in seen:
            continue
        seen.add(identity)
        try:
            payload = json.loads(value)
            if payload.get('version') != 1 or payload.get('decision') != 'confirmed' or str(payload['cycle_id']) != str(record_id):
                continue
            evidence = _evidence(connection, str(student_id), int(record_id), payload['offer_id'], payload['confirmation_id'])
            if any(payload.get(key) != val for key,val in evidence.items()):
                continue
        except (ValueError,TypeError,KeyError):
            continue
        found.setdefault(str(student_id), []).append(evidence)
    return {sid:tuple(sorted(items,key=lambda item:(item['cycle_sequence'],item['cycle_id']),reverse=True)) for sid,items in found.items()}
