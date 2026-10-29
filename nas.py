import os
import re
import shutil
import zipfile
from pathlib import Path
from staging_environment import ACTIVE as STAGING

from nas_access_capability import (
    NasAccessError,
    inspect_nas_access,
    require_readable_file,
    require_writable_directory,
)

STANDARD_FOLDERS = [
    "Applications", "Offer", "COE + OSHC", "VISA",
    "Homestay + Airport Pickup", "Forms", "Payments", "Others",
]

PRODUCTION_ROOT = Path("/Volumes/Server/Students/2026")
TEST_ROOT = Path("/Volumes/Server/Students/CRM_TEST")
STUDENTS_MOUNT = Path("/Volumes/Server/Students")
MVP_PRODUCTION_LOCKED = True
SAFE_PRODUCTION_STORAGE_ENV = "AAIE_SAFE_PRODUCTION_STORAGE_ROOT"
SAFE_PRODUCTION_STORAGE_BASE = Path("/private/tmp/aaie-combined-release")


class NasSafetyError(RuntimeError):
    pass


def sanitize_component(value):
    cleaned = re.sub(r"[^A-Za-z0-9 ._()'-]+", "", str(value or "")).strip(" .")
    return cleaned[:100] or "Unnamed"


def ensure_within(path, root):
    path = Path(path).resolve()
    root = Path(root).resolve()
    if path != root and root not in path.parents:
        raise NasSafetyError("Unsafe path rejected")
    return path


class NasManager:
    def __init__(self, db):
        self.db = db

    def isolated_safe_production_root(self):
        if os.environ.get("AAIE_SAFE_PRODUCTION") != "1":
            return None
        configured = os.environ.get(SAFE_PRODUCTION_STORAGE_ENV, "").strip()
        if not configured:
            return None
        candidate = Path(configured)
        if not candidate.is_absolute() or candidate.is_symlink():
            raise NasSafetyError("Safe Production storage root is invalid")
        resolved = candidate.resolve()
        approved = SAFE_PRODUCTION_STORAGE_BASE.resolve()
        if approved not in resolved.parents or not resolved.is_dir():
            raise NasSafetyError("Safe Production storage root is outside the APFS validation root")
        return resolved

    def status(self):
        if STAGING:
            root = STAGING.check(STAGING.storage)
            return {"mounted": True, "mode": "staging", "production_enabled": False,
                    "production_locked": True, "root": str(root), "root_error": "",
                    "root_exists": root.is_dir(), "readable": os.access(root, os.R_OK),
                    "writable": os.access(root, os.W_OK), "access_error_code": "",
                    "access_message": "", "ready": root.is_dir()}
        isolated = self.isolated_safe_production_root()
        if isolated is not None:
            access = inspect_nas_access(isolated, isolated)
            return {
                "mounted": access.mounted,
                "mode": "test",
                "production_enabled": False,
                "production_locked": True,
                "root": str(isolated),
                "root_error": "",
                "root_exists": access.root_exists,
                "readable": access.readable,
                "writable": access.writable,
                "access_error_code": access.error_code,
                "access_message": access.message,
                "ready": access.ready,
            }
        mode = self.db.setting("mode", "test")
        production_enabled = self.db.setting("production_enabled", "false") == "true"
        production_locked = MVP_PRODUCTION_LOCKED or self.db.setting("mvp_production_locked", "true") == "true"
        root_error = ""
        try:
            root = self.active_root()
        except NasSafetyError as exc:
            root = TEST_ROOT
            root_error = str(exc)
        access = inspect_nas_access(STUDENTS_MOUNT, root)
        return {
            "mounted": access.mounted,
            "mode": mode,
            "production_enabled": production_enabled,
            "production_locked": production_locked,
            "root": str(root),
            "root_error": root_error,
            "root_exists": access.root_exists,
            "readable": access.readable,
            "writable": access.writable,
            "access_error_code": access.error_code,
            "access_message": access.message,
            "ready": access.ready and not root_error and mode == "test",
        }

    def active_root(self):
        if STAGING:
            return STAGING.check(STAGING.storage)
        isolated = self.isolated_safe_production_root()
        if isolated is not None:
            return isolated
        mode = self.db.setting("mode", "test")
        if mode == "production":
            if MVP_PRODUCTION_LOCKED or self.db.setting("mvp_production_locked", "true") == "true":
                raise NasSafetyError("Production Mode is locked during MVP; only CRM_TEST is allowed")
            if self.db.setting("production_enabled", "false") != "true":
                raise NasSafetyError("Production Mode is disabled")
            configured = Path(self.db.setting("production_root", str(PRODUCTION_ROOT)))
            if configured.resolve() != PRODUCTION_ROOT.resolve():
                raise NasSafetyError("Production root does not match the approved path")
            return configured
        root = Path(self.db.setting("test_root", str(TEST_ROOT)))
        if root.resolve() != TEST_ROOT.resolve():
            raise NasSafetyError("Test root does not match the approved CRM_TEST path")
        if root.resolve() == PRODUCTION_ROOT.resolve() or PRODUCTION_ROOT.resolve() in root.resolve().parents:
            raise NasSafetyError("Test root cannot be inside the production directory")
        return root

    def require_ready(self):
        status = self.status()
        if not status["ready"]:
            raise NasSafetyError(
                status.get("root_error")
                or status.get("access_message")
                or "Student NAS Drive Not Connected"
            )
        return self.active_root()

    def create_student_tree(self, student_id, first_name, last_name):
        root = self.require_ready()
        if self.db.setting("mode", "test") == "test":
            root.mkdir(parents=True, exist_ok=True)
        base_name = sanitize_component(f"{first_name} {last_name}")
        folder = ensure_within(root / base_name, root)
        if folder.exists():
            folder = ensure_within(root / f"{base_name} ({student_id})", root)
        folder.mkdir(parents=False, exist_ok=False)
        for name in STANDARD_FOLDERS:
            ensure_within(folder / name, folder).mkdir()
        return folder

    def student_folder(self, student):
        if not student["folder_path"]:
            raise NasSafetyError("Student folder has not been created")
        root = self.active_root()
        try:
            return ensure_within(student["folder_path"], root)
        except NasSafetyError:
            if STAGING and STAGING.nas_read_roots:
                return STAGING.nas_read_path(student["folder_path"], TEST_ROOT)
            raise

    def category_folder(self, student, category):
        return self.require_category_access(student, category, write=True)

    def require_category_access(self, student, category, *, read=False, write=False):
        self.require_ready()
        if category not in STANDARD_FOLDERS:
            raise NasSafetyError("Unknown folder category")
        student_root = self.student_folder(student)
        folder = ensure_within(student_root / category, student_root)
        if write and STAGING:
            try:
                STAGING.check(folder)
            except RuntimeError as exc:
                raise NasSafetyError('测试环境对 NAS 附件仅有只读权限。') from exc
        if not folder.is_dir():
            raise NasSafetyError(f"{category} folder is unavailable")
        try:
            if read:
                with os.scandir(folder) as entries:
                    next(entries, None)
            if write:
                require_writable_directory(folder)
        except NasAccessError as exc:
            raise NasSafetyError(str(exc)) from exc
        except OSError as exc:
            raise NasSafetyError("CRM 当前进程没有 NAS 读取权限。") from exc
        return folder

    def require_student_file_readable(self, student, source):
        self.require_ready()
        student_root = self.student_folder(student)
        target = ensure_within(source, student_root)
        try:
            return require_readable_file(target)
        except NasAccessError as exc:
            raise NasSafetyError(str(exc)) from exc

    def safe_zip_members(self, zip_path, max_files=100, max_total=200 * 1024 * 1024):
        with zipfile.ZipFile(zip_path) as archive:
            members = [m for m in archive.infolist() if not m.is_dir() and not m.filename.startswith("__MACOSX/")]
            if len(members) > max_files:
                raise NasSafetyError("ZIP contains too many files")
            if sum(m.file_size for m in members) > max_total:
                raise NasSafetyError("ZIP expanded size is too large")
            for member in members:
                p = Path(member.filename)
                if p.is_absolute() or ".." in p.parts:
                    raise NasSafetyError("Unsafe ZIP path rejected")
            return members

    def remove_tree_after_failure(self, path):
        try:
            root = self.active_root()
            safe = ensure_within(path, root)
            if safe.exists() and safe != root:
                shutil.rmtree(safe)
        except Exception:
            pass
