"""Permission-scoped, source-consistent snapshots with a minimum revision fence."""
from datetime import datetime,timezone
from contextlib import contextmanager
from .commands import digest,canonical
from .contracts import Subject
from .schema import require_schema
from .versions import version_of


class MinimumVersionUnavailable(RuntimeError):pass


def read_authorization_context(connection, user):
    """Opaque identity/permission fence for already-rendered desktop content."""
    principal=dict(user)
    modules=[]
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_module_permissions'").fetchone():
        modules=[tuple(row) for row in connection.execute(
            'SELECT * FROM user_module_permissions WHERE user_id=? ORDER BY module_key,id',
            (principal.get('id'),))]
    return digest({'user':principal.get('id'),'role':principal.get('role_id'),
                   'permissions':principal.get('permissions_json'),'modules':modules})


def rendered_snapshot_hint(connection, subjects, *, can_read, authorization_context=None):
    """Seed already-rendered HTML without embedding its business payload twice.

    The caller must render and read revisions in this same read transaction.
    The marker is deliberately distinct from a full snapshot's content ETag.
    """
    if not connection.in_transaction or not connection.execute('PRAGMA query_only').fetchone()[0]:
        raise ValueError('active_readonly_render_required')
    require_schema(connection)
    requested=tuple(sorted(set(subjects)))
    if any(not isinstance(subject,Subject) for subject in requested):
        raise ValueError('invalid snapshot scope')
    versions=[{'kind':subject.kind,'id':subject.identifier,'version':version_of(connection,subject)}
              for subject in requested if can_read(subject)]
    result={'versions':versions,'etag':'rendered:'+digest(versions),
            'as_of':datetime.now(timezone.utc).isoformat()}
    if authorization_context is not None:result['authorization_context']=authorization_context
    return result


@contextmanager
def read_scope(database):
    """Reuse an explicit read-only request snapshot, or own a new one.

    Never replace an active writer or change its connection configuration.
    This is for reviewed read helpers, not a global connection interception.
    """
    state=getattr(database,'_request_state',lambda:None)()
    active=state.get('connection') if state else None
    if active is not None and active.in_transaction:
        if not active.execute('PRAGMA query_only').fetchone()[0]:
            raise RuntimeError('cannot borrow an active writer as a read snapshot')
        yield active
        return
    with database.read_snapshot() as connection:
        connection.execute('PRAGMA query_only=ON')
        yield connection


def read_snapshot(database,subjects,*,can_read,load,minimum_versions=None):
    requested=tuple(sorted(set(subjects)))
    if any(not isinstance(s,Subject) for s in requested):raise ValueError('invalid snapshot scope')
    minimum=dict(minimum_versions or {})
    for subject,revision in minimum.items():
        if not isinstance(subject,Subject) or type(revision) is not int or revision<0:raise ValueError('invalid minimum revision')
        if subject not in requested or not can_read(subject):raise PermissionError('snapshot_scope_forbidden')
    with database.read_snapshot() as conn:
        if not conn.in_transaction:raise ValueError('snapshot_transaction_required')
        # Own only this newly created read context, never a caller's business TX.
        conn.execute('PRAGMA query_only=ON')
        require_schema(conn)
        allowed=tuple(s for s in requested if can_read(s))
        versions=[{'kind':s.kind,'id':s.identifier,'version':version_of(conn,s)} for s in allowed]
        for row in versions:
            subject=Subject(row['kind'],row['id'])
            if row['version']<minimum.get(subject,0):raise MinimumVersionUnavailable('committed_revision_not_visible')
        data=load(conn,allowed)
        # Reject unserializable/nonfinite data before it reaches a response.
        canonical(data)
        etag=digest({'versions':versions,'data':data})
        return {'data':data,'versions':versions,'etag':etag,'as_of':datetime.now(timezone.utc).isoformat()}
