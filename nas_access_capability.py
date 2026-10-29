import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class NasAccessStatus:
    root: str
    mounted: bool
    root_exists: bool
    readable: bool
    writable: bool
    ready: bool
    error_code: str = ""
    message: str = ""

    def as_dict(self):
        return asdict(self)


class NasAccessError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = str(code or "nas_access_error")


def _probe_path(directory, probe_prefix):
    return Path(directory) / f"{probe_prefix}_{os.getpid()}_{secrets.token_hex(6)}"


def inspect_nas_access(mount_path, active_root):
    mount = Path(mount_path).resolve()
    root = Path(active_root).resolve()
    mounted = mount.is_dir()
    root_exists = root.is_dir()
    readable = False
    writable = False
    error_code = ""
    message = ""

    if not mounted:
        error_code = "mount_unavailable"
        message = "Student NAS Drive Not Connected"
    elif not root_exists:
        error_code = "root_unavailable"
        message = "CRM 学生 NAS 目录不可用。"
    else:
        try:
            with os.scandir(root) as entries:
                next(entries, None)
            readable = True
        except OSError:
            error_code = "read_denied"
            message = "CRM 当前进程没有 NAS 读取权限。"

        writable = os.access(root, os.W_OK | os.X_OK)
        if readable and not writable:
            error_code = "write_denied"
            message = "CRM 当前进程没有 NAS 写入权限。"

    return NasAccessStatus(
        root=str(root),
        mounted=mounted,
        root_exists=root_exists,
        readable=readable,
        writable=writable,
        ready=mounted and root_exists and readable and writable,
        error_code=error_code,
        message=message,
    )


def require_readable_file(source):
    target = Path(source).resolve()
    if not target.is_file():
        raise NasAccessError("source_unavailable", "NAS 来源文件不可用。")
    try:
        with target.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise NasAccessError("read_denied", "CRM 当前进程没有 NAS 文件读取权限。") from exc
    return target


def require_writable_directory(directory, probe_prefix=".aaie_crm_nas_probe"):
    target = Path(directory).resolve()
    if not target.is_dir():
        raise NasAccessError("root_unavailable", "NAS 目标目录不可用。")

    probe = _probe_path(target, probe_prefix)
    descriptor = None
    created = False
    try:
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        os.close(descriptor)
        descriptor = None
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise NasAccessError("write_denied", "CRM 当前进程没有 NAS 写入权限。") from exc
    finally:
        if created:
            try:
                probe.unlink()
            except OSError as exc:
                raise NasAccessError(
                    "probe_cleanup_failed",
                    "NAS 写入检查文件无法清理，操作已停止。",
                ) from exc
    return target
