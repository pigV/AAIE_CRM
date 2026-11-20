"""Explicit Visa checklist edits; no file category or workflow inference."""
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .visa_domains import case_scopes


def _owned_case(conn, student_id, case_id):
    cursor=conn.execute('''SELECT * FROM visa_cases WHERE id=? AND
        (student_id=? OR (student_id IS NULL AND ?='')) AND archived_at IS NULL''',
                        (case_id,student_id,student_id))
    row=cursor.fetchone()
    if row is None:raise ValueError('Visa Case 不存在或已归档。')
    case=dict(zip((item[0] for item in cursor.description),row))
    if student_id:
        columns={item[1] for item in conn.execute('PRAGMA table_info(students)')}
        if 'student_id' not in columns:raise ValueError('学生资料当前不可用。')
        active=" AND coalesce(deleted_at,'')=''" if 'deleted_at' in columns else ''
        if not conn.execute('SELECT 1 FROM students WHERE student_id=?'+active,(student_id,)).fetchone():
            raise ValueError('学生已不存在或已删除。')
    if case.get('application_cycle_id') is not None:
        if not student_id:raise ValueError('独立签证案件不能关联学生申请周期。')
        cycle=conn.execute('SELECT student_id FROM application_cycles WHERE id=?',(case['application_cycle_id'],)).fetchone()
        if not cycle or str(cycle[0])!=str(student_id):raise ValueError('案件与申请周期的学生归属不一致。')
    return case


def initialize_checklist(conn, *, student_id, case_id, actor_user_id, initialize, authorize=None):
    if authorize is not None:authorize(conn)
    case=_owned_case(conn,student_id,case_id)
    def capture(view):
        return tuple(tuple(row) for row in view.execute(
            'SELECT * FROM visa_checklist_items WHERE visa_case_id=? ORDER BY id',(case_id,)))
    return run_domain_write(conn,event_type='visa.checklist_initialized',actor_kind='user',actor_ref=str(actor_user_id),
        capture=capture,operation=lambda view:initialize(view,case),
        describe=lambda view,result,before,after:Mutation('visa.checklist_initialized',Subject('visa_case',str(case_id)),
            (*case_scopes(view,case),*(Subject('checklist_item',str(row[0])) for row in after))))


def save_checklist(conn,*,student_id,case_id,updates,actor_user_id,stamp):
    case=_owned_case(conn,student_id,case_id)
    clean={}
    for item_id,status,notes in updates:
        if status not in {'missing','uploaded','not_applicable'}:raise ValueError('材料状态无效。')
        if int(item_id) in clean:raise ValueError('材料项重复。')
        if conn.execute('SELECT 1 FROM visa_checklist_items WHERE id=? AND visa_case_id=?',(int(item_id),case_id)).fetchone() is None:
            raise ValueError('材料项不属于当前 Visa Case。')
        clean[int(item_id)]=(status,str(notes or ''))
    def capture(view):
        return tuple(tuple(r) for r in view.execute('SELECT id,status,coalesce(notes,\'\') FROM visa_checklist_items WHERE visa_case_id=? ORDER BY id',(case_id,)))
    def operation(view):
        changed=[]
        for item_id,(status,notes) in clean.items():
            previous=view.execute('SELECT status,coalesce(notes,\'\') FROM visa_checklist_items WHERE id=?',(item_id,)).fetchone()
            if tuple(previous)==(status,notes):continue
            view.execute('UPDATE visa_checklist_items SET status=?,notes=?,updated_at=? WHERE id=? AND visa_case_id=?',(status,notes,stamp,item_id,case_id));changed.append(item_id)
        return tuple(changed)
    return run_domain_write(conn,event_type='visa.checklist_updated',actor_kind='user',actor_ref=str(actor_user_id),
        capture=capture,operation=operation,
        describe=lambda view,result,before,after:Mutation('visa.checklist_updated',Subject('visa_case',str(case_id)),
            (*case_scopes(view,case),*(Subject('checklist_item',str(i)) for i in result))))


def save_checklist_command(conn, *, student_id, case_id, updates, actor_user_id, stamp,
                           command_key, expected_version, authorize):
    """Staff whole-form save: reject competing revisions, reauthorize retries."""
    from .commands import Command, Outcome, execute_command
    from .registry import Registry
    clean={}
    for item_id,status,notes in updates:
        identifier=int(item_id)
        if identifier in clean:raise ValueError('材料项重复。')
        if status not in {'missing','uploaded','not_applicable'}:raise ValueError('材料状态无效。')
        clean[identifier]=(status,str(notes or ''))
    scope=Subject('visa_case',str(int(case_id)))
    def owned(view):
        authorize(view)
        case=_owned_case(view,student_id,case_id)
        for identifier in clean:
            if not view.execute('SELECT 1 FROM visa_checklist_items WHERE id=? AND visa_case_id=?',
                                (identifier,case_id)).fetchone():raise ValueError('材料项不属于当前 Visa Case。')
        return case
    def operation(view):
        case=_owned_case(view,student_id,case_id);changed=[]
        for identifier,(status,notes) in clean.items():
            row=view.execute("SELECT status,coalesce(notes,'') FROM visa_checklist_items WHERE id=?",(identifier,)).fetchone()
            if tuple(row)==(status,notes):continue
            view.execute('UPDATE visa_checklist_items SET status=?,notes=?,updated_at=? WHERE id=? AND visa_case_id=?',
                         (status,notes,stamp,identifier,case_id));changed.append(identifier)
        mutation=Mutation('visa.checklist_updated',scope,
            (*case_scopes(view,case),*(Subject('checklist_item',str(i)) for i in changed)))
        return Outcome({'case_id':int(case_id),'changed':bool(changed)},(mutation,) if changed else ())
    command=Command('visa_checklist_save','user',str(actor_user_id),command_key,
        {'student_id':str(student_id),'case_id':int(case_id),'updates':sorted((i,*values) for i,values in clean.items())})
    return execute_command(conn,command,registry=Registry([]),authorize=owned,operation=operation,
                           expected_versions={scope:expected_version},occurred_at=stamp)
