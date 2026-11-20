"""Per-subject committed source revisions; never creates tables or commits."""
from .contracts import Subject


def version_of(conn,subject: Subject) -> int:
    if not isinstance(subject,Subject):raise ValueError('invalid subject')
    row=conn.execute('SELECT version FROM business_change_versions WHERE subject_kind=? AND subject_id=?',
                     (subject.kind,subject.identifier)).fetchone()
    return int(row[0]) if row else 0


def visible_versions(conn,subjects,*,can_read):
    """Call the current domain authorization policy for every requested subject."""
    return tuple((s,version_of(conn,s)) for s in sorted(set(subjects)) if can_read(s))
