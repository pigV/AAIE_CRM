"""Canonical, fail-closed environment loading for Production CRM services."""

import os
import re
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path


PRODUCTION_ROOT = Path("/Volumes/Zhang/AAIE CodeX/CRM")
ENV_FILE_ORDER = (".env", ".env.production", ".env.local")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ProductionEnvironmentError(RuntimeError):
    """Safe configuration error that never includes environment values."""

    def __init__(self, code, *, filename="", line_number=None):
        self.code = str(code)
        self.filename = str(filename)
        self.line_number = line_number
        location = self.filename
        if line_number is not None:
            location = f"{location}:{int(line_number)}"
        super().__init__(f"{self.code}:{location}" if location else self.code)


@dataclass(frozen=True)
class ProductionEnvironmentLoadResult:
    ok: bool
    production_file_present: bool
    files_loaded: tuple[str, ...]
    loaded_keys: tuple[str, ...]


def _parse_environment_file(path):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ProductionEnvironmentError(
            "PRODUCTION_ENVIRONMENT_READ_FAILED", filename=Path(path).name
        ) from exc

    values = {}
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ProductionEnvironmentError(
                "PRODUCTION_ENVIRONMENT_MALFORMED",
                filename=Path(path).name,
                line_number=line_number,
            )
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY.fullmatch(key):
            raise ProductionEnvironmentError(
                "PRODUCTION_ENVIRONMENT_MALFORMED",
                filename=Path(path).name,
                line_number=line_number,
            )
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_canonical_production_environment(*, root=PRODUCTION_ROOT, environ=None):
    """Load canonical env files atomically while preserving process precedence."""

    root = Path(root)
    target = os.environ if environ is None else environ
    if not isinstance(target, MutableMapping):
        raise TypeError("environ must be a mutable mapping")

    protected_keys = set(target)
    pending = {}
    files_loaded = []
    production_path = root / ".env.production"
    for filename in ENV_FILE_ORDER:
        path = root / filename
        if not path.is_file():
            continue
        parsed = _parse_environment_file(path)
        files_loaded.append(filename)
        for key, value in parsed.items():
            if key not in protected_keys:
                pending[key] = value

    target.update(pending)
    return ProductionEnvironmentLoadResult(
        ok=True,
        production_file_present=production_path.is_file(),
        files_loaded=tuple(files_loaded),
        loaded_keys=tuple(sorted(pending)),
    )
