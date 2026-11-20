"""Record a business command and its events on the caller's exact connection.

No commits, migrations, background threads, global DBs or external side effects.
Domain authorization and business evidence remain the caller's responsibility.
"""
from dataclasses import dataclass, field, InitVar
from datetime import datetime
import hashlib
import json
import re
import uuid
from weakref import WeakKeyDictionary

from .contracts import Subject, Change, validate_key
from .schema import require_schema
from .versions import version_of


class CommandConflict(ValueError):pass
class VersionConflict(ValueError):pass


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Command:
    command_type: str
    actor_kind: str
    actor_ref: str
    idempotency_key: str=field(repr=False)
    request: InitVar[object]=None
    request_hash: str=field(init=False)

    def __post_init__(self,request):
        validate_key(self.command_type,'command type')
        if self.actor_kind not in ('user','parent','system','integration'):raise ValueError('invalid actor kind')
        for value in (self.actor_ref,self.idempotency_key):
            if not isinstance(value,str) or not value.strip() or len(value)>200:raise ValueError('invalid command identity')
        object.__setattr__(self,'request_hash',digest(request))

    @property
    def key(self):
        return digest([self.command_type,self.actor_kind,self.actor_ref,self.idempotency_key])


@dataclass(frozen=True)
class Mutation:
    event_type: str
    source: Subject
    affected_subjects: tuple[Subject,...]=()
    previous_subjects: tuple[Subject,...]=()

    def __post_init__(self):
        validated=Change(self.event_type,self.source,1,self.affected_subjects)
        previous=Change(self.event_type,self.source,1,self.previous_subjects)
        object.__setattr__(self,'affected_subjects',validated.affected_subjects)
        object.__setattr__(self,'previous_subjects',previous.affected_subjects)


@dataclass(frozen=True)
class Outcome:
    result: dict
    changes: tuple[Mutation,...]


@dataclass(frozen=True)
class CommandResult:
    result: dict
    event_sequences: tuple[int,...]
    replayed: bool


_boundary_owners=WeakKeyDictionary()


def _domain_owner(connection):
    """Internal bridge only: domain callbacks still receive guarded views."""
    while isinstance(connection,TransactionView):
        connection=_boundary_owners[connection]
    return connection


class TransactionView:
    """A domain callback may query/write, but cannot end its caller's transaction."""
    def __init__(self,conn,protected_savepoint):
        self.__conn=conn;self.__savepoint=protected_savepoint
        _boundary_owners[self]=conn

    def commit(self):raise ValueError('outer_transaction_owned_by_caller')
    def rollback(self):raise ValueError('outer_transaction_owned_by_caller')
    def close(self):raise ValueError('outer_connection_owned_by_caller')
    def executescript(self,*args):raise ValueError('implicit_commit_forbidden')

    @property
    def in_transaction(self):return self.__conn.in_transaction

    @property
    def row_factory(self):return self.__conn.row_factory

    @row_factory.setter
    def row_factory(self,value):self.__conn.row_factory=value

    def _check(self,sql):
        clean=re.sub(r'/\*.*?\*/|--[^\n]*',' ',sql,flags=re.S).strip()
        if re.fullmatch(r'PRAGMA\s+foreign_keys\s*=\s*(?:ON|1)\s*;?',clean,re.I):return
        if re.match(r'(?:BEGIN|COMMIT|END|VACUUM|ATTACH|DETACH)\b',clean,re.I):raise ValueError('transaction_control_forbidden')
        if re.match(r'ROLLBACK\b',clean,re.I) and not re.match(r'ROLLBACK\s+TO\b',clean,re.I):raise ValueError('transaction_control_forbidden')
        if re.match(r'(?:SAVEPOINT|RELEASE|ROLLBACK)\b',clean,re.I) and re.search(r'\bbc_',clean,re.I):raise ValueError('protected_savepoint')
        if re.match(r'PRAGMA\b',clean,re.I) and ('=' in clean or re.match(r'PRAGMA\s+(?:journal_mode|writable_schema|foreign_keys)\s*\(',clean,re.I)):
            raise ValueError('connection_configuration_forbidden')

    def execute(self,sql,parameters=()):
        self._check(sql)
        return _CursorView(self,self.__conn.execute(sql,parameters))

    def executemany(self,sql,parameters):
        self._check(sql)
        return _CursorView(self,self.__conn.executemany(sql,parameters))

    def cursor(self):return _CursorView(self,self.__conn.cursor())


class _CursorView:
    def __init__(self,view,cursor):self.connection=view;self.__cursor=cursor
    def __iter__(self):return (row for row in self.__cursor)
    def fetchone(self):return self.__cursor.fetchone()
    def fetchall(self):return self.__cursor.fetchall()
    def fetchmany(self,*args):return self.__cursor.fetchmany(*args)
    def close(self):return self.__cursor.close()
    @property
    def lastrowid(self):return self.__cursor.lastrowid
    @property
    def rowcount(self):return self.__cursor.rowcount
    @property
    def description(self):return self.__cursor.description
    def execute(self,sql,parameters=()):
        self.connection._check(sql);self.__cursor.execute(sql,parameters);return self
    def executemany(self,sql,parameters):
        self.connection._check(sql);self.__cursor.executemany(sql,parameters);return self
    def executescript(self,*args):raise ValueError('implicit_commit_forbidden')


RESULT_KEYS=frozenset({'id','uid','record_id','entity_id','student_id','cycle_id','case_id','document_id',
                       'contract_id','invoice_id','payment_id','event_seq','version','state','issue_codes','changed','created'})


def _safe_result(result):
    if not isinstance(result,dict) or set(result)-RESULT_KEYS:raise ValueError('unsafe_command_result')
    for key,value in result.items():
        if key=='issue_codes':
            if not isinstance(value,(list,tuple)) or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',x) for x in value):raise ValueError('invalid_issue_codes')
        elif value is not None and (not isinstance(value,(str,int,bool)) or isinstance(value,str) and len(value)>200):
            raise ValueError('unsafe_command_result')
    return canonical(result)


def _append(conn,command_key,mutation,registry,stamp):
    subjects=sorted(set((mutation.source,*mutation.affected_subjects,*mutation.previous_subjects)))
    revisions={s:version_of(conn,s)+1 for s in subjects}
    change=Change(mutation.event_type,mutation.source,revisions[mutation.source],tuple(subjects))
    seq=conn.execute('''INSERT INTO business_change_events(event_id,command_key,event_type,source_kind,source_id,source_version,occurred_at)
                         VALUES(?,?,?,?,?,?,?)''',(uuid.uuid4().hex,command_key,mutation.event_type,mutation.source.kind,mutation.source.identifier,change.source_version,stamp)).lastrowid
    for subject in subjects:
        conn.execute('''INSERT INTO business_change_versions(subject_kind,subject_id,version,last_event_seq,updated_at)
                         VALUES(?,?,?,?,?) ON CONFLICT(subject_kind,subject_id) DO UPDATE SET
                         version=excluded.version,last_event_seq=excluded.last_event_seq,updated_at=excluded.updated_at''',
                     (subject.kind,subject.identifier,revisions[subject],seq,stamp))
        relation=('source' if subject==mutation.source else 'both' if subject in mutation.previous_subjects and subject in mutation.affected_subjects
                  else 'previous' if subject in mutation.previous_subjects else 'current')
        conn.execute('INSERT INTO business_change_impacts VALUES(?,?,?,?,?)',(seq,subject.kind,subject.identifier,revisions[subject],relation))
    plan=registry.plan(change)
    for target in plan:
        handler=registry.handler(target.handler_name)
        dependencies=[[seq,x.handler_name,x.handler_version,x.subject.kind,x.subject.identifier] for x in plan if x.handler_name in handler.after]
        conn.execute('''INSERT INTO business_change_deliveries(event_seq,handler_name,handler_version,subject_kind,subject_id,state,dependency_keys_json,updated_at)
                         VALUES(?,?,?,?,?,'pending',?,?)''',(seq,target.handler_name,target.handler_version,target.subject.kind,target.subject.identifier,canonical(dependencies),stamp))
    return seq


def command_request_hash(command,expected_versions):
    """Stable receipt seal, also usable by read-only manifest verification."""
    if not isinstance(command,Command):raise ValueError('invalid command')
    expectations=dict(expected_versions)
    for subject,version in expectations.items():
        if not isinstance(subject,Subject) or type(version) is not int or version<0:raise ValueError('invalid_expected_version')
    return digest([command.request_hash,[(s.kind,s.identifier,v) for s,v in sorted(expectations.items())]])


def execute_command(conn,command,*,registry,authorize,operation,expected_versions,occurred_at):
    """Caller owns commit; a failed command rolls back its own savepoint only."""
    if not conn.in_transaction:raise ValueError('outer_transaction_required')
    if conn.execute('PRAGMA foreign_keys').fetchone()[0]!=1:raise ValueError('foreign_keys_required')
    require_schema(conn)
    if not isinstance(command,Command):raise ValueError('invalid command')
    if datetime.fromisoformat(occurred_at).tzinfo is None:raise ValueError('timezone_required')
    expectations=dict(expected_versions)
    request_hash=command_request_hash(command,expectations)
    savepoint='bc_'+uuid.uuid4().hex
    conn.execute('SAVEPOINT '+savepoint)
    view=TransactionView(conn,savepoint)
    try:
        authorization_changes=conn.total_changes
        authorize(view)
        if conn.total_changes!=authorization_changes:raise ValueError('authorization_must_be_readonly')
        row=conn.execute('SELECT request_hash,result_json FROM business_change_commands WHERE command_key=?',(command.key,)).fetchone()
        if row:
            if row[0]!=request_hash:raise CommandConflict('idempotency_key_content_conflict')
            seqs=tuple(x[0] for x in conn.execute('SELECT seq FROM business_change_events WHERE command_key=? ORDER BY seq',(command.key,)))
            result=CommandResult(json.loads(row[1]),seqs,True)
        else:
            for subject,version in expectations.items():
                if version_of(conn,subject)!=version:raise VersionConflict('source_version_changed')
            before=conn.total_changes
            outcome=operation(view)
            if not isinstance(outcome,Outcome) or any(not isinstance(x,Mutation) for x in outcome.changes):raise ValueError('invalid_command_outcome')
            if conn.total_changes!=before and not outcome.changes:raise ValueError('untracked_business_mutation')
            result_json=_safe_result(outcome.result)
            for mutation in outcome.changes:
                if version_of(conn,mutation.source)>0 and mutation.source not in expectations:raise VersionConflict('source_expectation_required')
            conn.execute('''INSERT INTO business_change_commands VALUES(?,?,?,?,?,?,?)''',
                         (command.key,command.command_type,command.actor_kind,command.actor_ref,request_hash,result_json,occurred_at))
            sequences=tuple(_append(conn,command.key,mutation,registry,occurred_at) for mutation in outcome.changes)
            result=CommandResult(json.loads(result_json),sequences,False)
        conn.execute('RELEASE SAVEPOINT '+savepoint)
        return result
    except BaseException:
        conn.execute('ROLLBACK TO SAVEPOINT '+savepoint)
        conn.execute('RELEASE SAVEPOINT '+savepoint)
        raise
