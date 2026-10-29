"""Pinned local model identities; this module never performs downloads."""

from dataclasses import dataclass
import hashlib
from pathlib import Path


@dataclass(frozen=True)
class LocalModelSpec:
    engine_name: str
    request_model_name: str
    repository: str
    revision: str
    filename: str
    size_bytes: int
    sha256: str

    def model_path(self, runtime_root: Path) -> Path:
        return Path(runtime_root) / "models" / self.filename

    def verify_artifact(self, path: Path) -> str:
        candidate = Path(path)
        if not candidate.is_file():
            raise RuntimeError("model_missing")
        if candidate.stat().st_size != self.size_bytes:
            raise RuntimeError("model_size_mismatch")
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != self.sha256:
            raise RuntimeError("model_sha256_mismatch")
        return "sha256:" + actual

    @property
    def download_url(self) -> str:
        return (
            f"https://huggingface.co/{self.repository}/resolve/{self.revision}/"
            f"{self.filename}?download=true"
        )


QWEN3_14B_Q4_K_M = LocalModelSpec(
    engine_name="llama.cpp:qwen3-14b-q4_k_m",
    request_model_name="Qwen3-14B-GGUF-Q4_K_M",
    repository="Qwen/Qwen3-14B-GGUF",
    revision="c75e7b2d0234068f674a1bacf548ea32e27ccd29",
    filename="Qwen3-14B-Q4_K_M.gguf",
    size_bytes=9_001_752_960,
    sha256="500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0",
)
