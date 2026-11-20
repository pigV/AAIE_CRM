"""Explicit additive migration helpers; never install on import or read paths."""
from functools import lru_cache
from pathlib import Path
import re
from migration_runner import split_sql_statements

MIGRATION='20261119_050_business_changes.sql'
TABLES=tuple('business_change_'+x for x in ('commands','events','versions','impacts','deliveries'))


class SchemaUnavailable(RuntimeError):
    pass


def _normalized(sql):
    return re.sub(r'\s+',' ',sql.strip().rstrip(';')).strip()


@lru_cache(maxsize=1)
def _definitions():
    source=Path(__file__).resolve().parents[1]/'migrations'/MIGRATION
    result=[]
    for statement in split_sql_statements(source.read_text()):
        match=re.match(r'CREATE\s+(TABLE|INDEX)\s+(\w+)',statement,re.I)
        if not match:raise SchemaUnavailable('unexpected_migration_statement')
        result.append((match[1].lower(),match[2],statement))
    return tuple(result)


def require_schema(conn):
    from snapshot_read_cache import memoized_read
    return memoized_read(conn,('business_sync_schema',),lambda:_require_schema(conn))


def _require_schema(conn):
    for kind,name,sql in _definitions():
        row=conn.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?',(kind,name)).fetchone()
        if row is None or _normalized(row[0])!=_normalized(sql):
            raise SchemaUnavailable('business_change_schema_missing_or_mismatched')


def install(conn):
    """Called only by an explicitly authorized migration/test, in caller's TX."""
    if not conn.in_transaction:raise ValueError('outer_transaction_required')
    if conn.execute('PRAGMA foreign_keys').fetchone()[0]!=1:
        raise ValueError('foreign_keys_required')
    marks=','.join('?' for _ in TABLES)
    present=conn.execute(f'SELECT name FROM sqlite_master WHERE name IN ({marks})',TABLES).fetchall()
    if present:
        require_schema(conn)
        return False
    for _kind,_name,sql in _definitions():conn.execute(sql)
    require_schema(conn)
    return True
