"""Publish secure-link changes on the repository's original transaction.

Only a digest identifies the link in the common queue. Permission checks,
link validation and lifecycle writes remain owned by the secure form service.
"""
from . import commands
from .contracts import Subject
from .registry import Registry
from .runtime import enabled
from .versions import version_of


def publish_parent_link_change(conn, *, token, event_type, occurred_at, actor_id=None):
    if not enabled(conn):
        return
    if not conn.in_transaction:
        raise ValueError('parent_link_change_requires_caller_transaction')
    if event_type not in {'parent_form.link_created','parent_form.link_regenerated','parent_form.link_revoked'}:
        raise ValueError('unsupported_parent_link_change')
    row=conn.execute('SELECT student_id FROM parent_form_tokens WHERE token=?',(token,)).fetchone()
    if row is None:
        raise ValueError('parent_link_missing')
    identity=commands.digest(['secure_parent_form',token])
    source=Subject('parent_form_link',identity)
    affected=[Subject('dashboard','workflow'),Subject('dashboard','overview')]
    affected.extend(Subject('module',name) for name in ('students','applications','parent_forms','reminders'))
    if row[0] is not None:
        affected.append(Subject('student',str(row[0])))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_cycles'").fetchone():
        for cycle in conn.execute('SELECT id,source_student_case_id FROM application_cycles WHERE parent_form_token=?',(token,)):
            affected.append(Subject('application_cycle',str(cycle[0])))
            if cycle[1] is not None:
                affected.append(Subject('student_case',str(cycle[1])))
    command=commands.Command(event_type.replace('.','_'),'user' if actor_id is not None else 'system',
        str(actor_id) if actor_id is not None else 'secure_parent_form_repository',identity,
        {'link_digest':identity,'event_type':event_type})
    return commands.execute_command(conn,command,registry=Registry([]),authorize=lambda view:None,
        expected_versions={source:version_of(conn,source)},occurred_at=occurred_at,
        operation=lambda view:commands.Outcome({'changed':True},(commands.Mutation(event_type,source,tuple(affected)),)))
