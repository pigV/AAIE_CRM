#!/usr/bin/env python3
"""Read-only bounded before/after observations, no application imports."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import urllib.request
ROOT=Path('/Volumes/Zhang/AAIE CodeX/CRM')
DB=ROOT/'database/aaie_crm.db'
NAS=Path('/Volumes/Server/Students/CRM_TEST')

def observe():
    c=sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True)
    c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
    tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    digest={}
    for table in tables:
        q='"'+table.replace('"','""')+'"'
        h=hashlib.sha256()
        # Hash rows in a stable order, without writing or exporting their contents.
        rows=sorted(hashlib.sha256(repr(tuple(r)).encode()).digest() for r in c.execute('SELECT * FROM '+q))
        for row in rows:h.update(row)
        digest[table]={'rows':len(rows),'sha256':h.hexdigest()}
    schema=hashlib.sha256(repr(c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()).encode()).hexdigest()
    settings={str(r[0]):{'sha256':hashlib.sha256(repr(tuple(r[1:])).encode()).hexdigest(),'updated_at':r[2]} for r in c.execute('SELECT key,value,updated_at FROM system_settings')}
    sessions={hashlib.sha256(str(r[0]).encode()).hexdigest():{'stable_sha256':hashlib.sha256(repr(tuple(r[:-1])).encode()).hexdigest(),'last_seen_at':r[-1]} for r in c.execute('SELECT token,user_id,csrf_token,expires_at,created_at,last_seen_at FROM sessions')}
    c.rollback();c.close()
    files=[]
    for p in sorted(NAS.rglob('*')):
        st=p.lstat()
        files.append((str(p.relative_to(NAS)),st.st_mode,st.st_size,st.st_mtime_ns,st.st_ino))
    health={}
    for port in [8780,8781]:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=5) as r:health[str(port)]=json.load(r)
    listeners=subprocess.check_output(['/usr/sbin/lsof','-nP','-iTCP:8780','-iTCP:8781','-sTCP:LISTEN'],text=True)
    return {'sessions':sessions,'settings':settings,'tables':digest,'schema_sha256':schema,'nas_metadata_sha256':hashlib.sha256(repr(files).encode()).hexdigest(),'nas_entries':len(files),'listeners':listeners,'health':health}

if __name__=='__main__':
    target=Path(sys.argv[1]).resolve()
    allowed=Path(__file__).resolve().parents[1]/'STAGING_DATA'
    if allowed not in target.parents:raise SystemExit('Evidence destination outside Staging root')
    result=observe();target.write_text(json.dumps(result,indent=2)+'\n');print('Observation saved; tables',len(result['tables']),'NAS entries',result['nas_entries'])
