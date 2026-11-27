"""Real CRMHandler on a schema-only database with synthetic identities and students."""
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
import socket
import os
import threading
import app
from security import password_hash
from staging_environment import ACTIVE
from revenue.repository import RevenueRepository
from revenue.services import RevenueService

if ACTIVE is None or ACTIVE.admin_port != 19146:
    raise RuntimeError('Dedicated task runtime required')
for port in (19146,19147):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',port))
app.ensure_dirs()
app.db.initialize()
stamp = app.now_iso()
with app.db.transaction() as c:
    for key,value in {'mode':'staging','test_root':str(ACTIVE.storage),'production_root':'',
        'auto_backup_enabled':'false','auto_cleanup_enabled':'false',
        'CRM_PUBLIC_BASE_URL':'http://127.0.0.1:19146',
        'CRM_ADMIN_LAN_URL':'http://127.0.0.1:19146',
        'PARENT_FORM_PUBLIC_BASE_URL':'http://127.0.0.1:19147',
        'CRM_ADMIN_BIND_HOST':'127.0.0.1'}.items():
        c.execute('INSERT INTO system_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(key,value,stamp))
    for uid,role,username in ((990027,'Manager','tong-finance-manager'),
                              (990028,'Consultant','tong-finance-consultant')):
        role_id = c.execute('SELECT id FROM roles WHERE name=?',(role,)).fetchone()[0]
        c.execute('''INSERT OR IGNORE INTO users(id,username,display_name,password_hash,role_id,is_active,
            must_change_password,created_at) VALUES(?,?,?,?,?,1,0,?)''',
            (uid,username,'Synthetic '+role,password_hash('TongFinance!2026'),role_id,stamp))
        for key in ('dashboard','students','revenue_commission'):
            c.execute('''INSERT OR IGNORE INTO user_module_permissions(user_id,module_key,can_access,can_view,created_at,updated_at)
                VALUES(?,?,1,1,?,?)''',(uid,key,stamp,stamp))
        c.execute('''INSERT OR REPLACE INTO sessions(token,user_id,csrf_token,expires_at,created_at,last_seen_at)
            VALUES(?,?,?,?,?,?)''',(username,uid,'csrf-'+username,
                (datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),stamp,stamp))
    for sid,cid,status in (('990027',990027,'active'),('990028',990028,'closed')):
        folder = ACTIVE.storage/'synthetic-students'/sid
        folder.mkdir(parents=True,exist_ok=True)
        c.execute('''INSERT OR IGNORE INTO students(student_id,passport_last_name,passport_first_name,
            current_status,created_date,updated_at,form_data_json,folder_path,nas_status)
            VALUES(?,'Finance','Synthetic','Active',?,?,'{}',?,'ready')''',(sid,stamp,stamp,str(folder)))
        c.execute('''INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,
            status,outcome,closed_at,education_stream,opened_at,created_at,updated_at)
            VALUES(?,?,?,'initial',1,?,?,?,'school_education',?,?,?) ON CONFLICT(id) DO NOTHING''',
            (cid,'tong-finance-cycle-'+sid,sid,status,'completed' if status=='closed' else None,
             stamp if status=='closed' else None,'2020-01-01','2020-01-01',stamp))
    service = RevenueService(RevenueRepository(c))
    case = service.open_case(990027,990027,'browser-empty-case')
manifest = {'url':'http://127.0.0.1:19146','database':str(ACTIVE.db),
    'storage':str(ACTIVE.storage),'secure_storage':str(ACTIVE.root/'revenue_documents'),
    'students':['990027','990028'],'case_uid':case['uid'],'production_data_copied':0}
(ACTIVE.root/'browser-manifest.json').write_text(json.dumps(manifest,indent=2))
server = ThreadingHTTPServer(('127.0.0.1',19146),app.CRMHandler)
server.service_mode = 'admin'
parent_server = ThreadingHTTPServer(('127.0.0.1',19147),app.CRMHandler)
parent_server.service_mode = 'parent'
threading.Thread(target=parent_server.serve_forever,daemon=True).start()
(ACTIVE.root/'browser.pid').write_text(str(os.getpid()))
print(json.dumps(manifest),flush=True)
server.serve_forever()
