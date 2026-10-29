"""Cooperative release admission plus SQLite defense in depth.

No runtime auto-initialization. External/uninstrumented writers still require
formal exclusion. Auth row values are validated by the canonical auth caller;
this layer limits tables/columns, not arbitrary SQL predicates.
"""
from contextlib import contextmanager
import fcntl
import json
import os
import re
from pathlib import Path
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from urllib.parse import unquote, urlsplit, parse_qsl


class MaintenanceError(RuntimeError):
    def __init__(self, code='business_temporarily_read_only'):
        super().__init__(code)


class GateBusy(MaintenanceError):
    pass


def _no_symlink(path):
    for entry in (path, *path.parents):
        if entry.is_symlink():raise MaintenanceError('gate_path_symlink')


class Gate:
    def __init__(self, database, directory, required=True):
        self.database=Path(database).absolute();self.directory=Path(directory).absolute()
        self.required=required;self.local=threading.local();self._exfat=None
        _no_symlink(self.database);_no_symlink(self.directory)

    def _metadata(self,path,mode):
        _no_symlink(path);value=path.stat()
        if value.st_uid!=os.getuid():raise MaintenanceError('gate_owner_invalid')
        if stat.S_IMODE(value.st_mode)!=mode:
            # exFAT synthesizes modes. Never apply this exception to another FS.
            if self._exfat is None:
                self._exfat=False
                result=subprocess.run(['/sbin/mount'],capture_output=True,text=True,timeout=5,check=False)
                if result.returncode==0:
                    matches=[]
                    for line in result.stdout.splitlines():
                        if ' on ' in line and ' (' in line:
                            mount,options=line.split(' on ',1)[1].rsplit(' (',1)
                            if path.is_relative_to(Path(mount)):matches.append((len(mount),options.split(',')[0].rstrip(')')))
                    self._exfat=bool(matches and max(matches)[1]=='exfat')
            if not self._exfat:raise MaintenanceError('gate_mode_invalid')
        return value

    def status(self):
        try:
            self._metadata(self.directory,0o700)
            path=self.directory/'state.json';meta=self._metadata(path,0o600)
            if not stat.S_ISREG(meta.st_mode) or meta.st_size>4096:raise ValueError()
            value=json.loads(path.read_text())
            if set(value)!={'state','generation','lock_identity'} or value['state'] not in ('OPEN','CLOSED') or not isinstance(value['generation'],str) or len(value['generation'])!=32 or any(c not in '0123456789abcdef' for c in value['generation']):raise ValueError()
            lock=self._metadata(self.directory/'gate.lock',0o600)
            if value['lock_identity']!=[lock.st_dev,lock.st_ino]:raise ValueError()
            return {key:value[key] for key in ('state','generation')}
        except Exception:return {'state':'UNKNOWN','generation':None}

    def _open_lock(self):
        self._metadata(self.directory,0o700)
        path=self.directory/'gate.lock';self._metadata(path,0o600)
        fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
        try:self._assert_lock_fd(fd)
        except Exception:os.close(fd);raise
        return fd

    def _assert_lock_fd(self,fd):
        actual=os.fstat(fd);current=self._metadata(self.directory/'gate.lock',0o600)
        if not stat.S_ISREG(actual.st_mode) or actual.st_uid!=os.getuid() or (actual.st_dev,actual.st_ino)!=(current.st_dev,current.st_ino):raise MaintenanceError('gate_lock_identity_changed')
        path=self.directory/'state.json'
        if path.exists():
            _no_symlink(path)
            try:state=json.loads(path.read_text())
            except (ValueError,OSError):return  # UNKNOWN policy below still denies writes.
            if state.get('lock_identity')!=[actual.st_dev,actual.st_ino]:raise MaintenanceError('gate_lock_identity_changed')

    @contextmanager
    def _lock(self,exclusive=False,timeout=5):
        if not 0<timeout<=600:raise ValueError('invalid_gate_timeout')
        fd=self._open_lock();deadline=time.monotonic()+timeout
        try:
            while True:
                try:fcntl.flock(fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise GateBusy('gate_drain_timeout')
                    time.sleep(min(.01,max(0,deadline-time.monotonic())))
            self._assert_lock_fd(fd)
            yield
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)

    def _write(self,state):
        lock=self._metadata(self.directory/'gate.lock',0o600)
        value={'state':state,'generation':uuid.uuid4().hex,'lock_identity':[lock.st_dev,lock.st_ino]}
        temp=self.directory/('.state-'+uuid.uuid4().hex)
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            os.write(fd,(json.dumps(value,sort_keys=True)+'\n').encode());os.fsync(fd)
        finally:os.close(fd)
        os.replace(temp,self.directory/'state.json')
        fd=os.open(self.directory,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        return {key:value[key] for key in ('state','generation')}

    def initialize_open(self):
        _no_symlink(self.directory)
        self.directory.mkdir(mode=0o700,exist_ok=True)
        self._metadata(self.directory,0o700)
        # Exclusive creation prevents missing state from resetting an old window.
        try:fd=os.open(self.directory/'gate.lock',os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:raise MaintenanceError('gate_already_initialized') from None
        os.close(fd)
        with self._lock(True):
            if (self.directory/'state.json').exists():raise MaintenanceError('gate_state_already_exists')
            return self._write('OPEN')

    def enable(self,timeout=5):
        with self._lock(True,timeout):
            state=self.status()
            if state['state']=='UNKNOWN':raise MaintenanceError('gate_state_unknown')
            return state if state['state']=='CLOSED' else self._write('CLOSED')

    def disable(self,*,expected_generation,release_verified,timeout=5):
        with self._lock(True,timeout):
            state=self.status()
            if release_verified is not True or state['state']!='CLOSED' or state['generation']!=expected_generation:raise MaintenanceError('gate_release_not_verified')
            return self._write('OPEN')

    def kind(self):
        return getattr(self.local,'kinds',[])[-1] if getattr(self.local,'kinds',[]) else 'business'

    def current_kind(self):
        return self.kind()

    @contextmanager
    def operation(self,kind='business'):
        if kind not in ('read','business','auth'):raise ValueError('invalid_gate_operation')
        # Missing state can still serve pure reads; no SQL write is authorized.
        if not self.directory.exists() and kind=='read':
            yield;return
        with self._lock():
            state=self.status()['state']
            if (state=='UNKNOWN' and kind!='read') or (state=='CLOSED' and kind=='business'):raise MaintenanceError()
            kinds=getattr(self.local,'kinds',[]);self.local.kinds=kinds
            kinds.append(kind)
            try:yield
            finally:kinds.pop()


class GuardedCursor(sqlite3.Cursor):
    def execute(self,sql,parameters=()):return self.connection._statement(lambda:super(GuardedCursor,self).execute(sql,parameters),self)
    def executemany(self,sql,parameters):return self.connection._statement(lambda:super(GuardedCursor,self).executemany(sql,parameters),self)
    def executescript(self,sql):return self.connection._statement(lambda:super(GuardedCursor,self).executescript(sql),self)
    def _finished(self):
        self.connection._pending.discard(self)
        self.connection._release_if_idle()
    def fetchone(self):
        row=super().fetchone()
        if row is None:self._finished()
        return row
    def fetchmany(self,size=None):
        rows=super().fetchmany() if size is None else super().fetchmany(size)
        if not rows:self._finished()
        return rows
    def fetchall(self):
        rows=super().fetchall();self._finished();return rows
    def __next__(self):
        try:return super().__next__()
        except StopIteration:self._finished();raise
    def close(self):
        try:return super().close()
        finally:self._finished()


class GuardedConnection(sqlite3.Connection):
    def _configure(self,gate,target):
        self._gate=gate;self.write_gate=gate;self._target=target;self._lease=None;self._denied=False;self._state='UNKNOWN';self._kind='business';self._pending=set();self._write_seen=False
        sqlite3.Connection.set_authorizer(self,self._authorize)

    def _authorize(self,action,a,b,db,source):
        if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE):self._write_seen=True
        if action in (sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH):
            self._denied=True;return sqlite3.SQLITE_DENY
        if not self._target:return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_FUNCTION and str(b or a).lower()=='load_extension':
            self._denied=True;return sqlite3.SQLITE_DENY
        if self._state=='OPEN':return sqlite3.SQLITE_OK
        reads={sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE,sqlite3.SQLITE_TRANSACTION,sqlite3.SQLITE_SAVEPOINT}
        safe_pragma=False
        if action==sqlite3.SQLITE_PRAGMA:
            name=str(a).lower();value=str(b).lower()
            safe_pragma=(name in {'table_info','table_xinfo','index_info','index_xinfo','index_list','foreign_key_list','foreign_key_check','integrity_check','quick_check'}
                or b is None and name in {'database_list','compile_options','user_version','schema_version','freelist_count','page_count','page_size','foreign_keys','query_only','journal_mode'}
                or name in {'foreign_keys','query_only'} and value in {'on','1','true'}
                or name=='busy_timeout' and value.isdigit())
        if action in reads or safe_pragma:return sqlite3.SQLITE_OK
        allowed=False
        if self._state=='CLOSED' and self._kind=='auth' and db=='main':
            allowed=(action==sqlite3.SQLITE_INSERT and a in ('sessions','audit_logs') or action==sqlite3.SQLITE_DELETE and a=='sessions' or action==sqlite3.SQLITE_UPDATE and ((a=='sessions' and b=='last_seen_at') or (a=='users' and b in ('last_login_at','last_login_ip'))))
        if not allowed:self._denied=True;return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def _release(self):
        if self._lease is not None:
            lease=self._lease;self._lease=None;lease.__exit__(None,None,None)

    def _release_if_idle(self):
        if not self.in_transaction and not self._pending:self._release()

    def _statement(self,callback,cursor=None):
        if self._target and self._lease is None and self._gate.directory.exists():
            lease=self._gate._lock();lease.__enter__();self._lease=lease
        self._state=self._gate.status()['state'] if self._target else 'OPEN'
        self._kind=self._gate.kind();self._denied=False;self._write_seen=False
        try:
            result=callback()
            if cursor is not None:
                self._pending.discard(cursor)
                if self._write_seen and cursor.description is not None:self._pending.add(cursor)
            return result
        except sqlite3.DatabaseError:
            if self._denied:
                sqlite3.Connection.rollback(self);self._pending.clear()
                raise MaintenanceError() from None
            raise
        finally:
            self._release_if_idle()

    def cursor(self,*args,**kwargs):
        if args or kwargs:raise MaintenanceError('cursor_factory_not_allowed')
        return sqlite3.Connection.cursor(self,GuardedCursor)
    def execute(self,sql,parameters=()):return self.cursor().execute(sql,parameters)
    def executemany(self,sql,parameters):return self.cursor().executemany(sql,parameters)
    def executescript(self,sql):return self.cursor().executescript(sql)
    def commit(self):
        try:return sqlite3.Connection.commit(self)
        finally:
            self._release_if_idle()
    def rollback(self):
        try:return sqlite3.Connection.rollback(self)
        finally:
            self._pending.clear();self._release_if_idle()
    def close(self):
        try:return sqlite3.Connection.close(self)
        finally:self._pending.clear();self._release()
    def __exit__(self,*args):
        try:return sqlite3.Connection.__exit__(self,*args)
        finally:
            self._release_if_idle()
    def set_authorizer(self,*args,**kwargs):raise MaintenanceError('authorizer_override_forbidden')
    def enable_load_extension(self,*args,**kwargs):raise MaintenanceError('extension_loading_forbidden')
    def load_extension(self,*args,**kwargs):raise MaintenanceError('extension_loading_forbidden')
    def deserialize(self,*args,**kwargs):raise MaintenanceError('database_replacement_forbidden')
    def blobopen(self,table,column,row,*,readonly=False,name='main'):
        if not readonly:raise MaintenanceError('writable_blob_forbidden')
        return sqlite3.Connection.blobopen(self,table,column,row,readonly=True,name=name)
    def backup(self,target,*args,**kwargs):
        if isinstance(target,GuardedConnection) and target._target:raise MaintenanceError('database_replacement_forbidden')
        return sqlite3.Connection.backup(self,target,*args,**kwargs)


def install_sqlite_guard(gate):
    """Install before importing consumers. Returns explicit test/process uninstall.

    Non-target ordinary SQL unchanged except ATTACH/DETACH/custom factories are
    refused to prevent attaching the protected DB via another connection.
    """
    original=sqlite3.connect;original_dbapi=sqlite3.dbapi2.connect
    def connect(database,*args,**kwargs):
        if 'factory' in kwargs or len(args)>4:raise MaintenanceError('connection_factory_not_allowed')
        if kwargs.get('check_same_thread',True) is not True or len(args)>3 and args[3] is not True:raise MaintenanceError('cross_thread_connection_not_allowed')
        raw=os.fsdecode(database) if not isinstance(database,str) else database
        path=None
        if raw not in (':memory:',''):
            if raw.startswith('file:'):
                uri=urlsplit(raw)
                if uri.netloc not in ('','localhost') or uri.fragment or re.search(r'%(?![0-9a-fA-F]{2})',raw):raise MaintenanceError('database_uri_authority_unknown')
                try:items=parse_qsl(uri.query,keep_blank_values=True,strict_parsing=True,encoding='utf-8',errors='strict')
                except (ValueError,UnicodeError):raise MaintenanceError('database_uri_parameters_invalid') from None
                options={}
                allowed={'mode':{'ro','rw','rwc','memory'},'cache':{'shared','private'},'immutable':{'0','1'}}
                for key,value in items:
                    if key in options or key not in allowed or value not in allowed[key]:raise MaintenanceError('database_uri_parameters_invalid')
                    options[key]=value
                if options.get('mode')!='memory':
                    try:path=Path(unquote(uri.path,encoding='utf-8',errors='strict')).absolute()
                    except UnicodeError:raise MaintenanceError('database_uri_parameters_invalid') from None
            else:path=Path(raw).absolute()
        target=False
        if path is not None:
            _no_symlink(path)
            target=path==gate.database
            if not target and path.exists() and gate.database.exists():target=os.path.samefile(path,gate.database)
        kwargs['factory']=GuardedConnection;kwargs['cached_statements']=0
        def open_connection():
            if target and not path.exists() and gate.status()['state']!='OPEN':raise MaintenanceError('protected_database_missing')
            conn=original(database,*args,**kwargs)
            conn._configure(gate,target)
            return conn
        if target and gate.directory.exists():
            with gate._lock():return open_connection()
        return open_connection()
    sqlite3.connect=connect;sqlite3.dbapi2.connect=connect
    def uninstall():
        if sqlite3.connect is not connect:raise MaintenanceError('sqlite_guard_installation_changed')
        sqlite3.connect=original;sqlite3.dbapi2.connect=original_dbapi
    return uninstall
