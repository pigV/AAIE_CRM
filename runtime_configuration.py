"""Explicit code/data identity. Loading and preflight never create runtime state."""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import socket
import sqlite3

PRODUCTION_PORTS = frozenset({8780, 8781, 8790, 8792, 8793})
LEGACY_PRODUCTION = Path("/Users/faqiang/Downloads/AAIE_CRM_Test_5985902_20261030")
REQUIRED_DIRECTORIES = (
    "storage", "config", "logs", "cache", "tmp", "home", "templates",
    "secure_assets", "school_library_files", "upload_staging", "reports", "backups",
)
REQUIRED_TEMPLATES = (
    "AAIE_Student_Info_Template.xlsx", "StudyAbroadConsultingVisaContract_Template_V1.docx",
)


class RuntimeConfigurationError(RuntimeError):
    pass


def canonical(path):
    p = Path(path)
    if not p.is_absolute() or ".." in p.parts:
        raise RuntimeConfigurationError("absolute_canonical_path_required")
    for part in (p, *p.parents):
        if part.is_symlink():
            raise RuntimeConfigurationError("symlink_path_rejected")
    if p.exists() and p.is_file() and p.stat().st_nlink != 1:
        raise RuntimeConfigurationError("hardlink_path_rejected")
    return p.resolve()


@dataclass(frozen=True)
class RuntimeConfiguration:
    path: Path
    source_root: Path
    root: Path
    mode: str
    database: str
    admin_port: int
    parent_port: int
    extraction_port: int
    namespace: str
    config_sha256: str

    @property
    def db(self):
        return self.root / "database" / self.database

    @property
    def key(self):
        return self.root / "config/field_encryption.key"

    @property
    def map_port(self):
        return self.admin_port + 13

    @property
    def agent_reach_port(self):
        return self.admin_port + 10 if self.mode == "staging" else 8790

    @property
    def ports(self):
        return (self.admin_port, self.parent_port, self.extraction_port, self.map_port,
                self.agent_reach_port)

    @classmethod
    def load(cls, path, *, source_root):
        config_path = canonical(path)
        source = canonical(source_root)
        try:
            payload = config_path.read_bytes()
            value = json.loads(payload)
        except (OSError, ValueError, UnicodeError) as exc:
            raise RuntimeConfigurationError("runtime_config_unreadable") from exc
        keys = {"version", "mode", "root", "database", "admin_port", "parent_port",
                "extraction_port", "namespace"}
        if not isinstance(value, dict) or set(value) != keys or type(value["version"]) is not int or value["version"] != 1:
            raise RuntimeConfigurationError("invalid_runtime_config_schema")
        if not isinstance(value["mode"], str) or value["mode"] not in {"production", "staging"}:
            raise RuntimeConfigurationError("invalid_runtime_mode")
        if not isinstance(value["root"], str):
            raise RuntimeConfigurationError("invalid_runtime_root")
        root = canonical(value["root"])
        runtime = canonical(source.parent / "runtime")
        if value["mode"] == "production":
            if root != runtime / "production" or value["namespace"] != "aaie_production_":
                raise RuntimeConfigurationError("production_identity_mismatch")
        else:
            staging = runtime / "staging"
            if root == staging or not root.is_relative_to(staging):
                raise RuntimeConfigurationError("staging_identity_mismatch")
            if not isinstance(value["namespace"], str) or not re.fullmatch(r"aaie_staging_[a-z0-9_]+_", value["namespace"]):
                raise RuntimeConfigurationError("staging_namespace_required")
        if root == source or root.is_relative_to(source) or source.is_relative_to(root):
            raise RuntimeConfigurationError("code_data_overlap")
        if not source.is_dir() or not root.is_dir() or config_path != root / "config/runtime.json":
            raise RuntimeConfigurationError("runtime_config_location_mismatch")
        if (root / "ARCHIVED_NOT_ACTIVE.json").exists():
            raise RuntimeConfigurationError("archived_runtime_not_startable")
        name = value["database"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:db|sqlite|sqlite3)", name):
            raise RuntimeConfigurationError("invalid_database_filename")
        ports = [value[k] for k in ("admin_port", "parent_port", "extraction_port")]
        if any(type(p) is not int or not 1024 <= p <= 65535 for p in ports):
            raise RuntimeConfigurationError("invalid_port")
        ports.append(ports[0] + 13)
        ports.append(ports[0] + 10 if value["mode"] == "staging" else 8790)
        if max(ports) > 65535 or len(set(ports)) != len(ports):
            raise RuntimeConfigurationError("port_collision")
        if value["mode"] == "staging" and PRODUCTION_PORTS.intersection(ports):
            raise RuntimeConfigurationError("production_ports_forbidden")
        if value["mode"] == "production" and ports != [8780, 8781, 8792, 8793, 8790]:
            raise RuntimeConfigurationError("production_ports_mismatch")
        result = cls(config_path, source, root, value["mode"], name, *ports[:3],
                     value["namespace"], hashlib.sha256(payload).hexdigest())
        for p in (result.db, result.key, Path(str(result.db) + "-wal"),
                  Path(str(result.db) + "-shm"), Path(str(result.db) + "-journal")):
            canonical(p)
        return result

    def check_current(self):
        current = type(self).load(self.path, source_root=self.source_root)
        if current != self:
            raise RuntimeConfigurationError("runtime_configuration_changed")

    def require_ready(self):
        result = preflight(self)
        if not result["ready"]:
            raise RuntimeConfigurationError("runtime_not_ready:" + ",".join(result["errors"]))
        return result


def preflight(config, *, check_ports=False, owned_ports_only=False):
    """Read-only admission; reports missing resources rather than manufacturing them."""
    config.check_current()
    errors = []
    result = {
        "ready": False, "mode": config.mode, "source_root": str(config.source_root),
        "data_root": str(config.root), "database": str(config.db),
        "admin_port": config.admin_port, "parent_port": config.parent_port,
        "extraction_port": config.extraction_port, "map_port": config.map_port,
        "agent_reach_port": config.agent_reach_port,
        "config_sha256": config.config_sha256, "errors": errors,
        "pending_migrations": [], "database_initialized": False, "migrations_applied": False,
    }
    for name in REQUIRED_DIRECTORIES:
        path = canonical(config.root / name)
        if not path.is_dir():
            errors.append("directory_missing:" + name)
    if not canonical(config.source_root / "assets").is_dir():
        errors.append("code_assets_missing")
    for name in REQUIRED_TEMPLATES:
        path = canonical(config.root / "templates" / name)
        if not path.is_file() or path.stat().st_size == 0:
            errors.append("template_missing:" + name)
    if not config.key.is_file():
        errors.append("field_key_missing")
    else:
        try:
            from cryptography.fernet import Fernet
            Fernet(config.key.read_bytes().strip())
        except (OSError, ValueError, TypeError):
            errors.append("field_key_invalid")
    if not config.db.is_file():
        errors.append("database_missing")
    else:
        from migration_runner import verify_migrations, MigrationError
        try:
            migrations = verify_migrations(config.db, config.source_root / "migrations")
            result["pending_migrations"] = migrations["pending"]
            result["verified_migrations"] = len(migrations["verified"])
            if migrations["pending"]:
                errors.append("database_migrations_pending")
            if migrations["foreign_key_violations"]:
                errors.append("database_foreign_key_violations")
        except (MigrationError, OSError, sqlite3.Error) as exc:
            errors.append("database_verification_failed:" + type(exc).__name__)
    if check_ports:
        ports = (config.admin_port, config.parent_port, config.map_port) if owned_ports_only else config.ports
        for port in ports:
            try:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", port))
            except OSError:
                errors.append("port_unavailable:" + str(port))
    result["ready"] = not errors
    return result
