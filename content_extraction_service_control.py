import os
import secrets
from pathlib import Path


def ensure_service_token(cache_root):
    root = Path(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    token_path = root / "service.token"
    if token_path.exists():
        os.chmod(token_path, 0o600)
        token = token_path.read_text(encoding="utf-8").strip()
        if len(token) != 64:
            raise ValueError("invalid extraction service token")
        return token
    token = secrets.token_hex(32)
    try:
        descriptor = os.open(
            token_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return ensure_service_token(root)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(token)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(token_path, 0o600)
    return token
