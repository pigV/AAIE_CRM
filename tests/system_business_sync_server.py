"""Synthetic full-schema fixture, served only under task-owned OS confinement."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def prepare_fixture():
    from staging_environment import ACTIVE
    if ACTIVE is None or ACTIVE.root!=ROOT/'STAGING_DATA' or ACTIVE.admin_port!=19060:
        raise RuntimeError('task-specific OS-confined staging required')
    import app
    from security import password_hash
    from workflow_cutover import activate_workflow_v2,REQUIRED_EVIDENCE_KEYS
    from application_cycle import create_student_cycle
    from business_changes.runtime import SETTING
    from business_changes.schema import require_schema
    app.ensure_dirs();app.db.initialize()
    with app.db.transaction() as conn:require_schema(conn)
    if not app.db.one("SELECT 1 FROM students WHERE student_id='990101'"):
        stamp=app.now_iso()
        app.db.execute("UPDATE users SET password_hash=?,must_change_password=0 WHERE username='admin'",(password_hash('DemoSync!2026'),))
        for sid,first,last in [('990101','Sync','Demo'),('990102','Second','Demo')]:
            folder=app.nas.create_student_tree(sid,first,last)
            app.db.execute('''INSERT INTO students(student_id,passport_first_name,passport_last_name,current_status,
                created_date,updated_at,form_data_json,folder_path,nas_status) VALUES(?,?,?,'Active',?,?,'{}',?,'ready')''',
                (sid,first,last,stamp,stamp,str(folder)))
            with app.db.transaction() as conn:
                cycle=create_student_cycle(conn,sid,'initial',stamp,1,actor_name='Synthetic Admin')
            kinds=['500 Student Visa','590 Guardian Visa'] if sid=='990101' else ['500 Student Visa']
            for kind in kinds:
                app.db.execute('''INSERT INTO visa_cases(student_id,application_cycle_id,visa_case_name,visa_type,
                    applicant_name,application_type,status,result_status,case_owner,created_by,created_at,updated_at)
                    VALUES(?,?,?,?,?,'New Application','Information Collection','Pending','Synthetic Admin',1,?,?)''',
                    (sid,cycle.id,'Synthetic Sync Case',kind,first+' '+last,stamp,stamp))
        with app.db.transaction() as conn:
            activate_workflow_v2(conn,evidence={k:True for k in REQUIRED_EVIDENCE_KEYS},actor='synthetic-empty-fixture')
        app.db.set_setting(SETTING,'enabled',1)
    user=dict(app.db.one("SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.username='admin'"))
    handler=object.__new__(app.CRMHandler);handler.user=user;handler.csrf='fixture';handler.query={};handler.headers={};handler.client_address=('127.0.0.1',0)
    for case in app.db.all("SELECT * FROM visa_cases WHERE student_id IN ('990101','990102')"):
        handler.visa_material_initialize_checklist(case)
    from datetime import datetime,timedelta
    stamp=app.now_iso();expires=(datetime.fromisoformat(stamp)+timedelta(days=1)).isoformat()
    app.db.execute('''INSERT INTO sessions(token,user_id,csrf_token,expires_at,created_at,last_seen_at)
        VALUES('synthetic-sync-admin-session',?,'synthetic-sync-csrf',?,?,?)
        ON CONFLICT(token) DO UPDATE SET expires_at=excluded.expires_at,last_seen_at=excluded.last_seen_at''',(user['id'],expires,stamp,stamp))
    app.maybe_run_auto_backup=lambda:None
    return app


def main():
    app=prepare_fixture()
    if '--init-only' in sys.argv:return
    parent='--parent' in sys.argv
    if parent:
        from staging_environment import ACTIVE
        if ACTIVE.parent_port!=19061:raise RuntimeError('unexpected task parent port')
    port=19061 if parent else 19060
    server=app.ReusableThreadingHTTPServer(('127.0.0.1',port),app.CRMHandler)
    server.service_mode='parent' if parent else 'admin';server.service_name=server.service_mode
    print(f'Synthetic system sync {server.service_mode}: http://127.0.0.1:{port}',flush=True)
    server.serve_forever()


if __name__=='__main__':main()
