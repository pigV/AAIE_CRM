"""Fail-closed POSIX storage for Revenue artefacts, without encryption or fallback.

Only descriptor-relative I/O crosses the storage boundary. Payload bytes never
enter a new file before its actual permissions, identity and ACL are checked.
"""
from contextlib import contextmanager
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import stat
import sys
import uuid


class StorageUnavailable(ValueError):
    message = '安全文件存储未配置或不满足权限要求，操作已停止。'
    def __init__(self):
        super().__init__(self.message)


def _path(value):
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw.startswith('/') or '\x00' in raw or '\\' in raw:
        raise StorageUnavailable()
    if any(p in ('.', '..') for p in raw.split('/')):
        raise StorageUnavailable()
    return Path(raw)


def _acl(fd):
    """Reject permissive extended ACLs; unknown ACL capability fails closed."""
    if sys.platform == 'darwin':
        libc = ctypes.CDLL(None, use_errno=True)
        libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        libc.acl_get_fd_np.restype = ctypes.c_void_p
        libc.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
        libc.acl_to_text.restype = ctypes.c_void_p
        libc.acl_free.argtypes = [ctypes.c_void_p]
        ctypes.set_errno(0)
        acl = libc.acl_get_fd_np(fd, 0x100)  # SDK sys/acl.h ACL_TYPE_EXTENDED
        if not acl:
            # Darwin filesec_get_property(FILESEC_ACL) reports ENOENT when
            # the open descriptor has no ACL. Other retrieval errors are fatal.
            if ctypes.get_errno() == errno.ENOENT:
                os.fstat(fd)  # The descriptor must still identify a live object.
                return
            raise StorageUnavailable()
        text = None
        try:
            size = ctypes.c_ssize_t()
            text = libc.acl_to_text(acl, ctypes.byref(size))
            if not text:
                raise StorageUnavailable()
            value = ctypes.string_at(text, size.value).decode('utf-8', errors='strict')
            # Deny-only inherited entries (e.g. home deny delete) do not grant access.
            if ':allow:' in value:
                raise StorageUnavailable()
        finally:
            if text: libc.acl_free(text)
            libc.acl_free(acl)
    elif sys.platform.startswith('linux'):
        try:
            acl = os.getxattr(fd, 'system.posix_acl_access')
        except OSError as exc:
            if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
                raise StorageUnavailable() from exc
        else:
            if acl: raise StorageUnavailable()
    else:
        raise StorageUnavailable()


def _directory(fd, *, managed=False):
    s = os.fstat(fd)
    if not stat.S_ISDIR(s.st_mode) or s.st_uid not in (0, os.geteuid()):
        raise StorageUnavailable()
    if stat.S_IMODE(s.st_mode) & 0o022:
        raise StorageUnavailable()
    if managed and (stat.S_IMODE(s.st_mode) != 0o700 or
                    (s.st_uid, s.st_gid) != (os.geteuid(), os.getegid())):
        raise StorageUnavailable()
    _acl(fd)


@contextmanager
def _open_directory(value, *, create=False):
    path = _path(value)
    fd = -1
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open('/', flags)
        _directory(fd)
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create: raise
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            _directory(fd)
        _directory(fd, managed=True)
        yield path, fd
    except (OSError, UnicodeError, TypeError) as exc:
        raise StorageUnavailable() from exc
    finally:
        if fd >= 0: os.close(fd)


def configured_root(environ=None):
    values = os.environ if environ is None else environ
    raw = values.get('REVENUE_SECURE_STORAGE_ROOT')
    if not raw: raise StorageUnavailable()
    with _open_directory(raw) as (path, _):
        return path


def _regular(fd, *, legacy=False):
    s = os.fstat(fd)
    if (not stat.S_ISREG(s.st_mode) or stat.S_IMODE(s.st_mode) not in ((0o600, 0o700) if legacy else (0o600,)) or
            (s.st_uid, s.st_gid) != (os.geteuid(), os.getegid()) or s.st_nlink != 1):
        raise StorageUnavailable()
    _acl(fd)
    return s


def _name(value):
    if (not isinstance(value, str) or not value or value in ('.', '..') or
            '/' in value or '\\' in value or '\x00' in value):
        raise StorageUnavailable()
    return value


def _read_at(directory_fd, name, max_bytes, *, legacy=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 dir_fd=directory_fd)
    try:
        before = _regular(fd, legacy=legacy)
        if before.st_size > max_bytes: raise StorageUnavailable()
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk: break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b''.join(chunks)
        after = _regular(fd, legacy=legacy)
        if (len(data) > max_bytes or len(data) != before.st_size or
                (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
            raise StorageUnavailable()
        return data
    finally:
        os.close(fd)


def write_immutable(root, filename, payload):
    """Publish a new immutable object, or validate exact secure existing bytes.

After publication callers must NOT delete on DB rollback: another concurrent
request may already have reused it. Unregistered secure objects remain inert;
future collection requires an independently authorized reference-safe policy.
"""
    name = _name(filename)
    if not isinstance(payload, bytes) or not payload: raise StorageUnavailable()
    with _open_directory(root, create=True) as (path, directory_fd):
        try:
            existing = _read_at(directory_fd, name, len(payload))
        except FileNotFoundError:
            pass
        else:
            if existing != payload: raise StorageUnavailable()
            return path / name, False
        scratch = '.pending-' + uuid.uuid4().hex
        fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory_fd)
        try:
            before = _regular(fd)  # Empty scratch must satisfy exact authority first.
            os.fsync(fd)
            os.close(fd)
            fd = -1
            fd = os.open(scratch, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=directory_fd)
            reopened = _regular(fd)
            if (before.st_dev, before.st_ino, 0) != (reopened.st_dev, reopened.st_ino, reopened.st_size):
                raise StorageUnavailable()
            view = memoryview(payload)
            while view:
                count = os.write(fd, view)
                if count <= 0: raise StorageUnavailable()
                view = view[count:]
            os.fsync(fd)
            _regular(fd)
            try:
                os.link(scratch, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                        follow_symlinks=False)
                created = True
            except FileExistsError:
                if _read_at(directory_fd, name, len(payload)) != payload:
                    raise StorageUnavailable()
                created = False
        finally:
            if fd >= 0: os.close(fd)
            # Only this operation's never-registered scratch name is removed.
            os.unlink(scratch, dir_fd=directory_fd)
        os.fsync(directory_fd)
        if _read_at(directory_fd, name, len(payload)) != payload:
            raise StorageUnavailable()
        return path / name, created


def read_secure(root, target, expected_sha256, expected_size, *, max_bytes=20 * 1024 * 1024):
    root, target = _path(root), _path(target)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise StorageUnavailable() from exc
    if not relative.parts or type(expected_size) is not int or not 0 < expected_size <= max_bytes:
        raise StorageUnavailable()
    # Verify configured authority even when reading a nested document directory.
    with _open_directory(root):
        pass
    with _open_directory(target.parent) as (_, fd):
        data = _read_at(fd, _name(target.name), max_bytes)
    if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise StorageUnavailable()
    return data


def read_reference(legacy_root, target, expected_sha256, expected_size, *, secure_root=None, max_bytes=20 * 1024 * 1024):
    """Select one explicit reference authority; missing/corrupt data never changes it.

    Legacy owner-only600/700 files are read-only compatibility, not secure writes.
    Roots must be disjoint, and every traversed component is opened NOFOLLOW.
    """
    legacy_root, target = _path(legacy_root), _path(target)
    secure_root = _path(secure_root) if secure_root is not None else None
    if secure_root is not None and (secure_root.is_relative_to(legacy_root) or legacy_root.is_relative_to(secure_root)):
        raise StorageUnavailable()
    legacy = target.is_relative_to(legacy_root)
    secure = secure_root is not None and target.is_relative_to(secure_root)
    if legacy == secure:
        raise StorageUnavailable()
    if secure:
        return read_secure(secure_root, target, expected_sha256, expected_size, max_bytes=max_bytes)
    if type(expected_size) is not int or not 0 < expected_size <= max_bytes:
        raise StorageUnavailable()
    try:
        with _open_directory(legacy_root):
            pass
        with _open_directory(target.parent) as (_, fd):
            data = _read_at(fd, _name(target.name), max_bytes, legacy=True)
    except OSError as exc:
        raise StorageUnavailable() from exc
    if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise StorageUnavailable()
    return data


def configured_write_root(legacy_root, environ=None):
    """An explicit write configuration cannot repurpose the read-only legacy root."""
    legacy_root = _path(legacy_root)
    root = configured_root(environ)
    if root.is_relative_to(legacy_root) or legacy_root.is_relative_to(root):
        raise StorageUnavailable()
    return root
