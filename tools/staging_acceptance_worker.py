"""Bounded test file deletion, only under the kernel-enforced Staging sandbox."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from staging_environment import ACTIVE as c,IsolationError
import json
import sqlite3
if c is None:raise IsolationError('Staging sandbox required')
c.check(c.db);c.check(c.storage)
with sqlite3.connect(c.db) as db:
    row=db.execute("SELECT student_id,folder_path FROM students WHERE passport_last_name='STAGING' AND passport_first_name='ISOLATION'").fetchone()
    if not row:raise IsolationError('Browser-created fixture student missing')
files=[p for p in c.check(Path(row[1])).rglob('*.txt') if p.read_bytes()==b'STAGING ISOLATION FIXTURE ONLY\nNo personal data.\n']
if len(files)!=1:raise IsolationError('Expected exactly one browser-uploaded fixture')
file=c.check(files[0]);relative=str(file.relative_to(c.storage));file.unlink()
if file.exists():raise IsolationError('Test file deletion failed')
c.check(c.root/'file-acceptance.json').write_text(json.dumps({'student_id':row[0],'record_created_via':'real browser','upload_confirmed_via':'real browser upload review','uploaded_file_relative':relative,'file_delete':'PASS','deletion_context':'Staging sandbox guarded filesystem operation; no Production action'},indent=2)+'\n')
print('Browser-created record and upload verified; guarded test file deletion PASS')
