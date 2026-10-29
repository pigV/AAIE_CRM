"""One-time sanitization of the authorized backup copy, inside native sandbox."""
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import sys
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from staging_environment import ACTIVE as c
from security import password_hash
assert c is not None
receipt=c.check(c.root/"audit/test_database_prepared.json")
if receipt.exists():raise RuntimeError("Database already prepared; refusing to reset accounts")
password=secrets.token_urlsafe(18)
now=datetime.now(timezone.utc).isoformat()
def schema_hash(conn):
    rows=conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
    return hashlib.sha256(repr(rows).encode()).hexdigest()
with sqlite3.connect(c.check(c.db)) as db:
    db.execute("PRAGMA foreign_keys=ON")
    before=schema_hash(db)
    students=db.execute("SELECT count(*) FROM students").fetchone()[0]
    db.execute("DELETE FROM sessions")
    db.execute("UPDATE users SET is_active=0")
    role=db.execute("SELECT id FROM roles WHERE name='Admin'").fetchone()[0]
    cur=db.execute("INSERT INTO users(username,display_name,password_hash,role_id,is_active,must_change_password,created_at) VALUES(?,?,?,?,1,0,?)",
                   ("local-test-admin","独立测试管理员",password_hash(password),role,now))
    user_id=cur.lastrowid
    columns=[r[1] for r in db.execute("PRAGMA table_info(user_module_permissions)") if r[1].startswith("can_")]
    names=["user_id","module_key",*columns,"created_at","updated_at"]
    sql="INSERT INTO user_module_permissions("+",".join(names)+") VALUES("+",".join("?" for _ in names)+")"
    for (module,) in db.execute("SELECT module_key FROM modules WHERE is_active=1").fetchall():
        db.execute(sql,[user_id,module,*([1]*len(columns)),now,now])
    keys=[r[0] for r in db.execute("SELECT key FROM system_settings")]
    scrubbed=0
    for key in keys:
        low=key.lower()
        if any(word in low for word in ("secret","token","api_key","password","credential","google_drive","wechat")):
            db.execute("UPDATE system_settings SET value='',updated_at=? WHERE key=?",(now,key));scrubbed+=1
    settings={"mode":"staging","test_root":str(c.storage),"production_root":"","production_enabled":"false",
        "auto_backup_enabled":"false","auto_cleanup_enabled":"false",
        "PARENT_FORM_PUBLIC_BASE_URL":f"http://127.0.0.1:{c.parent_port}",
        "CRM_ADMIN_LAN_URL":f"http://127.0.0.1:{c.admin_port}",
        "CRM_PUBLIC_BASE_URL":f"http://127.0.0.1:{c.admin_port}","CRM_ADMIN_BIND_HOST":"127.0.0.1"}
    for key,value in settings.items():
        db.execute("INSERT INTO system_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,value,now))
    assert schema_hash(db)==before
    assert db.execute("SELECT count(*) FROM students").fetchone()[0]==students
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
    db.commit()
report={"status":"PASS","schema_sha256":before,"schema_changed":False,"students_unchanged":True,
        "source_users_disabled":True,"copied_sessions_cleared":True,"scrubbed_settings_count":scrubbed,
        "test_username":"local-test-admin","production_access":False}
receipt.write_text(json.dumps(report,ensure_ascii=False,indent=2))
c.check(c.root/"TEST_LOGIN.txt").write_text("仅此独立测试版使用\n地址：http://127.0.0.1:18880/login\n用户名：local-test-admin\n密码："+password+"\n")
print(json.dumps(report,ensure_ascii=False))
