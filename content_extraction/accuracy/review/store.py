"""Atomic private JSON persistence for Ground Truth review."""

import json
import os
from pathlib import Path
import threading

from .models import apply_review_action, evaluate_review_readiness, project_review_item


class GroundTruthReviewStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._lock = threading.RLock()

    def load(self) -> dict:
        with self._lock:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("samples"), list):
            raise ValueError("invalid_review_dataset")
        return payload

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def project(self) -> dict:
        payload = self.load()
        projected = [project_review_item(item) for item in payload["samples"]]
        return {
            "schema_version": payload.get("schema_version"),
            "dataset_id": payload.get("dataset_id"),
            "sample_count": len(projected),
            "readiness": evaluate_review_readiness(payload),
            "samples": projected,
        }

    def replace(self, payload: dict) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("samples"), list):
            raise ValueError("invalid_review_dataset")
        with self._lock:
            self._write(payload)

    def apply(self, sample_id, expected_stage, action, text, now) -> dict:
        with self._lock:
            payload = self.load()
            selected_index = next(
                (index for index, item in enumerate(payload["samples"])
                 if item.get("sample_id") == sample_id),
                None,
            )
            if selected_index is None:
                raise KeyError("unknown_review_sample")
            updated = apply_review_action(
                payload["samples"][selected_index], expected_stage, action, text, now,
            )
            payload["samples"][selected_index] = updated
            self._write(payload)
            return project_review_item(updated)
