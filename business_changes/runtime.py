"""Explicit rollout state in the existing settings table; never auto-migrate."""
from .schema import require_schema

SETTING='business_change_sync_v1'


def enabled(conn):
    from snapshot_read_cache import memoized_read
    return memoized_read(conn,('business_sync_enabled',),lambda:_enabled(conn))


def _enabled(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_settings'").fetchone() is None:return False
    row=conn.execute('SELECT value FROM system_settings WHERE key=?',(SETTING,)).fetchone()
    if row is None or row[0]=='disabled':return False
    if row[0]!='enabled':raise ValueError('invalid_business_change_rollout_state')
    require_schema(conn)
    return True
