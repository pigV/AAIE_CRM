"""Owned Student profile and formal submission transaction boundaries."""
from functools import wraps
import json

from .commands import Command, Mutation, Outcome, digest, execute_command, _domain_owner
from .contracts import Subject
from .domain import run_domain_write
from .registry import Registry
from .runtime import enabled
from .versions import version_of


def student_impacts(conn, student_id):
    if conn.execute('SELECT 1 FROM students WHERE student_id=?', (student_id,)).fetchone() is None:
        raise ValueError('学生不存在')
    affected = [Subject('dashboard', 'workflow'), Subject('dashboard', 'overview')]
    affected.extend(Subject('module', name) for name in
                    ('students', 'applications', 'contracts', 'visa_cases', 'revenue'))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='application_cycles' AND type='table'").fetchone():
        cursor = conn.execute("SELECT * FROM application_cycles WHERE student_id=? AND status='active' ORDER BY id", (student_id,))
        columns = [c[0] for c in cursor.description]
        for row in cursor.fetchall():
            cycle = dict(zip(columns, row))
            affected.append(Subject('application_cycle', str(cycle['id'])))
            if cycle.get('source_student_case_id') is not None:
                affected.append(Subject('student_case', str(cycle['source_student_case_id'])))
    return tuple(affected)


def _profile_snapshot(conn, student_id):
    result = []
    # This fixed list is the canonical profile writer's source graph. Replacing
    # child rows with identical values is housekeeping, not a second change.
    tables = ('students', 'parents', 'siblings', 'accommodation_preferences',
              'health_declarations', 'visa_risk_info', 'emergency_contacts', 'study_background')
    for table in tables:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        cursor = conn.execute('SELECT * FROM '+table+' WHERE student_id=?', (student_id,))
        positions = [i for i, c in enumerate(cursor.description)
                     if c[0] not in {'updated_at', 'updated_by', 'created_at'}
                     and not (table != 'students' and c[0] == 'id')]
        rows = tuple(sorted((tuple(row[i] for i in positions) for row in cursor.fetchall()), key=repr))
        result.append((table, rows))
    return tuple(result)


def synchronized_profile_write(function):
    @wraps(function)
    def wrapped(conn, student, data, user, changed_at):
        if not enabled(conn):
            return function(conn, student, data, user, changed_at)
        student_id = str(student['student_id'])
        return run_domain_write(conn, event_type='student.profile_updated',
            actor_kind='user' if user else 'system',
            actor_ref=str(user['id']) if user else 'parent_profile_service',
            capture=lambda view: _profile_snapshot(view, student_id),
            operation=lambda view: function(view, student, data, user, changed_at),
            describe=lambda view, result, before, after: Mutation('student.profile_updated',
                Subject('student', student_id), student_impacts(view, student_id)))
    return wrapped


def publish_student_submission(conn, *, student_id, parent_form_token, event_type, occurred_at):
    """Called after source/profile/token writes, before the original commit.

    Parent identity and submission locking remain with the secure form service.
    Token contents and submitted profile values never enter the common queue.
    """
    conn = _domain_owner(conn)
    if not enabled(conn):
        return
    student_id = str(student_id)
    if event_type not in {'student.created', 'parent_form.submitted'}:
        raise ValueError('unsupported_student_submission_event')
    if bool(parent_form_token) != (event_type == 'parent_form.submitted'):
        raise ValueError('student_submission_identity_mismatch')
    if parent_form_token:
        row = conn.execute('SELECT student_id,submitted_at FROM parent_form_tokens WHERE token=?', (parent_form_token,)).fetchone()
        if row is None or str(row[0]) != student_id or not row[1]:
            raise ValueError('parent_submission_owner_mismatch')
    affected = student_impacts(conn, student_id)
    source = Subject('student', student_id)
    identity = digest(['parent_submission', parent_form_token]) if parent_form_token else student_id
    command = Command(event_type.replace('.', '_'), 'parent' if parent_form_token else 'system',
        identity if parent_form_token else 'student_creation_service', identity,
        {'student_id': student_id, 'event_type': event_type})
    receipt = conn.execute('SELECT result_json FROM business_change_commands WHERE command_key=?', (command.key,)).fetchone()
    expected = version_of(conn, source)
    if receipt:
        committed = json.loads(receipt[0]).get('version')
        if type(committed) is not int or committed < 1:
            raise ValueError('invalid_student_submission_receipt')
        expected = committed - 1
    def operation(view):
        view.execute('''INSERT INTO audit_logs(action_type,module_name,module_key,record_id,student_id,status,created_at)
            VALUES(?,'students','students',?,?,'success',?)''',
            (event_type.replace('.', '_'), student_id, student_id, occurred_at))
        return Outcome({'student_id': student_id, 'version': expected+1}, (Mutation(event_type, source, affected),))
    return execute_command(conn, command, registry=Registry([]), authorize=lambda view: None,
        operation=operation, expected_versions={source: expected}, occurred_at=occurred_at)
