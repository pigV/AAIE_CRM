"""Temporary release admission; ordinary reads stay separate from business writes.

This module never initializes operator state, changes a database, or starts a job.
The operator must provision the required Production state before activating code.
"""
from contextlib import nullcontext
from functools import wraps
from pathlib import Path
import json
import hashlib
import os
import re
import threading
from urllib.parse import urlsplit

_READ_ROUTES = (
    r"/", r"/health", r"/version", r"/login", r"/assets/[^?]+",
    r"/students", r"/students/new", r"/students/\d+",
    r"/students/\d+/(?:summary|overview)",
    r"/visa-management", r"/visa-management/applicants/new",
    r"/visa-management/cases/\d+(?:/workspace)?",
    r"/visa-management/cases/\d+/documents/\d+/file",
    r"/documents/\d+/download",
    r"/api/topbar-status", r"/mobile", r"/account",
)


def request_kind(method, target):
    path = urlsplit(target).path
    if method == 'POST' and path in ('/login', '/logout'):
        return 'auth'
    if method in ('GET', 'HEAD') and any(re.fullmatch(p, path) for p in _READ_ROUTES):
        return 'read'
    return 'business'


def managed_production_root():
    return Path(__file__).resolve().parent.parent / 'runtime/production'


def configured_gate(root):
    """Only the formal runtime auto-binds; isolated tests explicitly inject a Gate."""
    root = Path(root).resolve()
    if root == managed_production_root():
        from runtime_configuration import RuntimeConfiguration
        from business_write_gate import Gate, install_sqlite_guard
        config = RuntimeConfiguration.load(root / 'config/runtime.json',
                                           source_root=Path(__file__).resolve().parent)
        gate = Gate(config.db, root / 'config/business-write-gate', required=True)
        install_sqlite_guard(gate)
        return gate
    if root != Path('/Volumes/Zhang/AAIE CodeX/CRM').resolve():
        return None
    from business_write_gate import Gate, install_sqlite_guard
    directory = Path.home() / 'Library/Application Support/AAIE CRM/production-services/business-write-gate'
    gate = Gate(root / 'database/aaie_crm.db', directory, required=True)
    install_sqlite_guard(gate)
    return gate


def closed(gate):
    return gate is not None and gate.status()['state'] != 'OPEN'


def startup_readonly(gate):
    return closed(gate) or (gate is not None and
            ((gate.directory / 'bootstrap-readonly.json').exists() or
             (gate.directory / 'bootstrap-readonly.json').is_symlink()))


def create_bootstrap_marker(gate, candidate_sha):
    state=gate.status()
    if state['state']!='OPEN' or not re.fullmatch('[0-9a-f]{40}',candidate_sha):
        raise RuntimeError('bootstrap_marker_authority_invalid')
    path=gate.directory/'bootstrap-readonly.json'
    payload=json.dumps({'candidate_sha':candidate_sha,'generation':state['generation']},sort_keys=True).encode()
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:os.write(fd,payload);os.fsync(fd)
    finally:os.close(fd)
    fd=os.open(gate.directory,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)
    return {'path':str(path),'sha256':hashlib.sha256(payload).hexdigest(),'generation':state['generation']}


def remove_bootstrap_marker(gate, receipt, *, release_verified):
    path=gate.directory/'bootstrap-readonly.json';state=gate.status()
    if (release_verified is not True or state['state']!='OPEN' or state['generation']!=receipt['generation']
            or str(path)!=receipt['path'] or path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest()!=receipt['sha256']):
        raise RuntimeError('bootstrap_marker_removal_not_proven')
    path.unlink()
    fd=os.open(gate.directory,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def maintenance_response(handler):
    text = '系统正在进行短暂发布维护。您仍可查看资料；本次修改未保存，请稍后重新提交。'
    as_json = 'application/json' in handler.headers.get('Accept', '').lower()
    body = (json.dumps({'ok': False, 'code': 'release_read_only', 'message': text},
                       ensure_ascii=False) if as_json else
            '<!doctype html><meta charset="utf-8"><title>暂时只读</title>'
            '<main><h1>系统暂时只读</h1><p>' + text + '</p><a href="/">返回查看资料</a></main>')
    payload = body.encode('utf-8')
    handler.send_response(503)
    handler.send_header('Content-Type', ('application/json' if as_json else 'text/html') + '; charset=utf-8')
    handler.send_header('Content-Length', str(len(payload)))
    handler.send_header('Retry-After', '15')
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Connection', 'close')
    handler.end_headers()
    handler.close_connection = True
    if handler.command != 'HEAD':
        handler.wfile.write(payload)


def install_handler(handler_class, gate_getter):
    """Guard before request parsing/file side effects; no requests are queued."""
    from business_write_gate import MaintenanceError
    for method in ('GET', 'HEAD', 'POST'):
        original = getattr(handler_class, 'do_' + method)
        def wrapped(self, _method=method, _original=original):
            gate = gate_getter()
            kind = request_kind(_method, self.path)
            try:
                with gate.operation(kind) if gate is not None else nullcontext():
                    return _original(self)
            except MaintenanceError:
                return maintenance_response(self)
        setattr(handler_class, 'do_' + method, wrapped)


def optional_business_task(task, gate_getter, *, skipped=None):
    """Skip this invocation while closed, never replay it when reopened."""
    from business_write_gate import MaintenanceError
    @wraps(task)
    def wrapped(*args, **kwargs):
        gate = gate_getter()
        try:
            if startup_readonly(gate):
                raise MaintenanceError('release_optional_task_deferred')
            with gate.operation('business') if gate is not None else nullcontext():
                return task(*args, **kwargs)
        except MaintenanceError:
            if skipped:
                skipped()
            return None
    return wrapped


def start_business_thread(task, gate_getter, *, name, kwargs=None):
    """Transfer an admitted request to an admitted worker without a drain gap."""
    gate=gate_getter();ready=threading.Event();failures=[]
    admission=threading.Lock();state={'started':False,'cancelled':False}
    def worker():
        try:
            with gate.operation('business') if gate is not None else nullcontext():
                with admission:
                    if state['cancelled']:return
                    state['started']=True
                    ready.set()
                task(**(kwargs or {}))
        except Exception as exc:
            if not ready.is_set():failures.append(exc)
            else:raise
        finally:ready.set()
    with gate.operation('business') if gate is not None else nullcontext():
        thread=threading.Thread(target=worker,name=name,daemon=True)
        thread.start()
        if not ready.wait(5):
            with admission:
                if not state['started']:
                    state['cancelled']=True
                    from business_write_gate import MaintenanceError
                    raise MaintenanceError('worker_admission_not_proven')
                # The worker won the handshake and holds its own gate lease.
                # Report admission honestly instead of claiming no work occurred.
        if failures:raise failures[0]
        return thread
