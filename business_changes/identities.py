"""Stable command identities with read-only compatibility for old random IDs."""
import re
from uuid import uuid5,NAMESPACE_URL

ALLOWED={('transfer_v2_command_receipts','event_uid'),('transfer_school_applications','branch_uid'),
         ('transfer_v2_origin_events','event_uid'),('transfer_school_library_requests','request_uid')}


def stable_command_identity(prefix):
    return prefix+':'+uuid5(NAMESPACE_URL,'aaie-crm-command:'+prefix).hex


def identity_prefix(identity):
    prefix,separator,suffix=identity.rpartition(':')
    return prefix+separator if separator and re.fullmatch(r'[0-9a-f]{32}',suffix) else None


def find_command_identity(conn,table,column,identity,*,conflict_error=ValueError):
    if (table,column) not in ALLOWED:raise ValueError('unsupported_command_identity_table')
    row=conn.execute('SELECT * FROM '+table+' WHERE '+column+'=?',(identity,)).fetchone()
    if row is not None:return row
    prefix=identity_prefix(identity)
    if prefix is None:return None
    # Exact prefix AND exact 32-hex suffix: keys containing ':', '%' or '_'
    # cannot match another command. No rewrite/backfill of historical IDs.
    rows=conn.execute('SELECT * FROM '+table+' WHERE substr('+column+',1,?)=? AND length('+column+')=? AND substr('+column+',?) NOT GLOB \'*[^0-9a-f]*\' LIMIT 2',
        (len(prefix),prefix,len(prefix)+32,len(prefix)+1)).fetchall()
    rows=[r for r in rows if re.fullmatch(r'[0-9a-f]{32}',r[column][len(prefix):])]
    if len(rows)>1:raise conflict_error('历史存在重复命令身份，请先核查，未重复执行。')
    return rows[0] if rows else None
