"""Opt-in Staging authority. No Production imports or side effects when disabled."""
from dataclasses import dataclass
import ctypes
import json
import ipaddress
import os
from pathlib import Path
import sys

PRODUCTION = Path('/Volumes/Zhang/AAIE CodeX/CRM')
PRODUCTION_DB = PRODUCTION / 'database/aaie_crm.db'
PROTECTED_STORAGE = (Path('/Volumes/Server/Students/CRM_TEST'), Path('/Volumes/Server/Students/2026'))
NAS_READ_ROOTS = (PROTECTED_STORAGE[0], Path('/Volumes/Server/Schools/CRM_Schools'))
SOURCE = Path(__file__).resolve().parent

class IsolationError(RuntimeError):
    pass


def canonical(path):
    p = Path(path)
    if not p.is_absolute():
        raise IsolationError('Absolute path required')
    # Reject aliases, including parent directory symlinks and hardlinks.
    for part in (p, *p.parents):
        if part.is_symlink():
            raise IsolationError('Symlink path rejected')
    if p.exists() and p.is_file() and p.stat().st_nlink != 1:
        raise IsolationError('Hardlinked file rejected')
    return p.resolve()


def contains(parent, child):
    return parent == child or parent in child.parents


@dataclass(frozen=True)
class Staging:
    root: Path
    admin_port: int
    parent_port: int
    namespace: str

    @property
    def db(self): return self.root / 'database/staging.db'
    @property
    def storage(self): return self.root / 'storage'

    @property
    def admin_lan_ip(self):
        config = self.check(self.root / 'admin-lan.json')
        if not config.exists():
            return ''
        value = json.loads(config.read_text())
        if not isinstance(value, dict) or set(value) != {'address'}:
            raise IsolationError('Invalid Admin LAN configuration')
        try:
            address = ipaddress.IPv4Address(value['address'])
        except (ValueError, TypeError) as exc:
            raise IsolationError('Invalid Admin LAN address') from exc
        if not any(address in ipaddress.IPv4Network(block) for block in ['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16']):
            raise IsolationError('Admin LAN address must be a private IPv4 address')
        return str(address)

    def bind_host(self, role):
        if role not in {'admin', 'parent'}:
            raise IsolationError('Unknown Staging service')
        return '0.0.0.0' if role == 'admin' and self.admin_lan_ip else '127.0.0.1'

    @property
    def nas_read_roots(self):
        config = self.check(self.root / 'nas-readonly.json')
        if not config.exists():
            return ()
        value = json.loads(config.read_text())
        if not isinstance(value, dict) or set(value) != {'enabled'} or type(value['enabled']) is not bool:
            raise IsolationError('Invalid read-only NAS configuration')
        return NAS_READ_ROOTS if value['enabled'] else ()

    @property
    def nas_write_roots(self):
        config = canonical(self.root / 'nas-write.json')
        if not config.exists():
            return ()
        value = json.loads(config.read_text())
        if not isinstance(value, dict) or set(value) != {'enabled'} or type(value['enabled']) is not bool:
            raise IsolationError('Invalid NAS write authorization')
        if not value['enabled']:
            return ()
        roots = self.nas_read_roots
        if not roots:
            raise IsolationError('NAS read authorization is required before write authorization')
        return roots

    def nas_read_path(self, target, root):
        if root not in self.nas_read_roots:
            raise IsolationError('NAS read root is not authorized')
        approved = canonical(root)
        path = canonical(target)
        if not contains(approved, path):
            raise IsolationError('NAS read outside the approved root')
        return path

    @classmethod
    def load(cls, config):
        d = json.loads(canonical(config).read_text())
        if set(d) != {'mode', 'root', 'admin_port', 'parent_port', 'namespace'} or d['mode'] != 'staging':
            raise IsolationError('Invalid staging configuration')
        if any(str(p) in json.dumps(d) for p in PROTECTED_STORAGE):
            raise IsolationError('Production storage reference rejected')
        root = canonical(d['root'])
        if root != SOURCE / 'STAGING_DATA':
            raise IsolationError('Staging root must be this isolated source tree / STAGING_DATA')
        if SOURCE == PRODUCTION:
            raise IsolationError('Production checkout cannot be Staging')
        ports = [d['admin_port'], d['parent_port']]
        if any(type(p) is not int or p < 1024 or p > 65535 or (p in (8780,8781,8790,8792) and ports != [8780,8781]) for p in ports) or ports[0] == ports[1]:
            raise IsolationError('Invalid or colliding ports')
        if d['namespace'] != 'aaie_staging_':
            raise IsolationError('Dedicated cookie namespace required')
        result = cls(root, *ports, d['namespace'])
        result.check(result.db)
        result.check(result.storage)
        return result

    def check(self, target):
        p = canonical(target)
        if contains(self.root, p):
            return p
        for root in self.nas_write_roots:
            if contains(canonical(root), p):
                return p
        for protected in (*PROTECTED_STORAGE, PRODUCTION / 'database'):
            if contains(protected, p) or contains(p, protected):
                raise IsolationError('Production path collision')
        if not contains(self.root, p):
            raise IsolationError('Write outside Staging data root')
        return p

    def check_database(self, target):
        p = canonical(target)
        if not contains(self.root, p):
            raise IsolationError('Databases must remain inside local Staging data')
        return self.check(p)

    def health_snapshot(self):
        import urllib.request
        healthy = True
        for role, port in [('admin', self.admin_port), ('parent', self.parent_port)]:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2) as response:
                    data = json.load(response)
                healthy = healthy and all(data.get(k) == v for k, v in {
                    'status': 'ok', 'environment': 'staging', 'service': role,
                    'port': port, 'database_path': str(self.db),
                    'storage_path': str(self.storage)}.items())
            except (OSError, ValueError):
                healthy = False
        return {'headline': 'Staging 双服务正常' if healthy else 'Staging 服务未就绪',
                'overall_ok': healthy, 'core_ok': healthy, 'services_ok': healthy,
                'monitor_ok': False, 'external_link_ok': False,
                'data_mode': {'key': 'STAGING'},
                'state': 'ok' if healthy else 'warning', 'symbol': 'OK' if healthy else '!'}

    def assert_sandbox(self):
        # Independently verify kernel enforcement; an environment flag is not proof.
        lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        lib.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        lib.sandbox_check.restype = ctypes.c_int
        writable = self.nas_write_roots
        for p in [p for p in [PRODUCTION_DB, *PROTECTED_STORAGE, *NAS_READ_ROOTS, SOURCE / 'app.py'] if p not in writable]:
            if lib.sandbox_check(os.getpid(), b'file-write-data', 1, str(p).encode()) != 1:
                raise IsolationError('Required OS write sandbox missing')
        for p in writable:
            if lib.sandbox_check(os.getpid(), b'file-write-data', 1, str(p).encode()) != 0:
                raise IsolationError('Authorized NAS write access is unavailable')
        for p in self.nas_read_roots:
            if lib.sandbox_check(os.getpid(), b'file-read-data', 1, str(p).encode()) != 0:
                raise IsolationError('Authorized read-only NAS access is unavailable')

    def _audit_path(self, path, dir_fd=None, *, unlink=False):
        """Resolve descriptor-relative audit paths without confusing them with cwd."""
        import fcntl
        import stat
        def descriptor_path(fd, directory=False):
            try:
                if directory and not stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise IsolationError('Audit dir_fd is not a directory')
                # Darwin F_GETPATH; an invalid/unsupported descriptor fails closed.
                return Path(os.fsdecode(fcntl.fcntl(fd, 50, bytes(1024)).split(b'\0', 1)[0]))
            except (OSError, ValueError, TypeError) as exc:
                raise IsolationError('Cannot resolve audit descriptor') from exc
        if isinstance(path, int):
            target = descriptor_path(path)
        else:
            target = Path(os.fsdecode(path))
            if not target.is_absolute():
                parent = descriptor_path(dir_fd, True) if dir_fd not in (None, -1) else Path.cwd()
                target = parent / target
        if unlink:
            # Atomic secure publication briefly has two links. Unlink removes only
            # this checked directory entry; it does not write the linked inode.
            parent = self.check(target.parent)
            if target.is_symlink():
                raise IsolationError('Symlink path rejected')
            return parent / target.name
        return self.check(target)

    def install_audit(self):
        # Relative open audit events omit dir_fd. Only the kernel can resolve
        # these accurately; require its write confinement before installing.
        self.assert_sandbox()
        def hook(event, args):
            if event == 'sqlite3.connect':
                value = args[0]
                if isinstance(value, str) and value.startswith('file:'):
                    from urllib.parse import urlparse, unquote
                    value = unquote(urlparse(value).path)
                if value != ':memory:': self.check_database(value)
            elif event == 'open':
                path, mode, flags = args
                if isinstance(path, (str, bytes, os.PathLike)) and ((flags or 0) & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)):
                    text = os.fsdecode(path)
                    if text != '/dev/null' and os.path.isabs(text):
                        self.check(text)
                    elif not os.path.isabs(text) and not (flags & os.O_NOFOLLOW):
                        raise IsolationError('Relative write requires no-follow open')
                    # Descriptor-relative no-follow open remains kernel-confined.
                    # Secure writer additionally verifies fd identity/mode/link count
                    # before writing any bytes.
            elif event in ('os.remove', 'os.rmdir'):
                self._audit_path(args[0], args[1], unlink=event == 'os.remove')
            elif event in ('os.mkdir', 'os.chmod'):
                self._audit_path(args[0], args[2])
            elif event == 'os.utime':
                self._audit_path(args[0], args[3])
            elif event == 'os.truncate':
                self._audit_path(args[0])
            elif event in ('os.rename', 'os.link'):
                self._audit_path(args[0], args[2])
                self._audit_path(args[1], args[3])
            elif event == 'os.symlink':
                raise IsolationError('Symlink creation rejected')
        sys.addaudithook(hook)


ACTIVE = None
if os.environ.get('AAIE_CRM_ENV') == 'staging':
    ACTIVE = Staging.load(os.environ.get('AAIE_STAGING_CONFIG', ''))
    ACTIVE.assert_sandbox()
    ACTIVE.install_audit()
