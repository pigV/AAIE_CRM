"""Private, same-job checkpoint persistence for the production pipeline."""

from dataclasses import dataclass
import dataclasses
import hashlib
import importlib
import json
import os
from pathlib import Path


STAGE_ORDER = (
    "downloaded", "audio_normalized", "recognizing_ocr", "recognizing_asr",
    "fusing_asr", "fusing_cross_modal", "correcting_transcript",
    "preparing_review", "completed",
)


def encode_pipeline_value(value):
    if dataclasses.is_dataclass(value):
        return {
            "__pipeline_type__": f"{type(value).__module__}:{type(value).__qualname__}",
            "fields": {
                field.name: encode_pipeline_value(getattr(value, field.name))
                for field in dataclasses.fields(value)
            },
        }
    if isinstance(value, tuple):
        return {"__pipeline_tuple__": [encode_pipeline_value(item) for item in value]}
    if isinstance(value, list):
        return [encode_pipeline_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): encode_pipeline_value(item) for key, item in value.items()}
    if isinstance(value, Path):
        return {"__pipeline_path__": str(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported_pipeline_checkpoint_type:{type(value).__name__}")


def decode_pipeline_value(value):
    if isinstance(value, list):
        return [decode_pipeline_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "__pipeline_tuple__" in value:
        return tuple(decode_pipeline_value(item) for item in value["__pipeline_tuple__"])
    if "__pipeline_path__" in value:
        return Path(str(value["__pipeline_path__"]))
    type_name = value.get("__pipeline_type__")
    if type_name:
        module_name, _, qualified = str(type_name).partition(":")
        if not module_name.startswith("content_extraction.") or "." in qualified:
            raise ValueError("unsupported_pipeline_checkpoint_type")
        module = importlib.import_module(module_name)
        cls = getattr(module, qualified, None)
        if cls is None or not dataclasses.is_dataclass(cls):
            raise ValueError("unsupported_pipeline_checkpoint_type")
        fields = {
            key: decode_pipeline_value(item)
            for key, item in dict(value.get("fields") or {}).items()
        }
        return cls(**fields)
    return {key: decode_pipeline_value(item) for key, item in value.items()}


def _canonical_bytes(payload):
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _atomic_private_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(_canonical_bytes(payload) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class StageCheckpoint:
    stage: str
    input_fingerprint: str
    config_fingerprint: str
    artifact_path: str
    artifact_sha256: str
    payload: dict
    accounting: dict


class PipelineStateStore:
    schema_version = "service-pipeline-state-v1"

    def __init__(self, root, crm_job_id, run_id):
        self.root = Path(root).resolve()
        self.crm_job_id = int(crm_job_id)
        self.run_id = str(run_id)
        self.state_path = self.root / "pipeline-state.json"

    def _empty(self):
        return {
            "schema_version": self.schema_version,
            "crm_job_id": self.crm_job_id,
            "run_id": self.run_id,
            "stages": {},
        }

    def _read(self):
        if not self.state_path.is_file():
            return self._empty()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if (
            payload.get("schema_version") != self.schema_version
            or int(payload.get("crm_job_id", -1)) != self.crm_job_id
            or str(payload.get("run_id", "")) != self.run_id
            or not isinstance(payload.get("stages"), dict)
        ):
            return None
        return payload

    def _write(self, state):
        _atomic_private_json(self.state_path, state)

    def invalidate_from(self, stage):
        state = self._read()
        if state is None:
            return
        try:
            start = STAGE_ORDER.index(str(stage))
        except ValueError:
            start = 0
        for name in STAGE_ORDER[start:]:
            state["stages"].pop(name, None)
        self._write(state)

    def save_stage(
        self, stage, input_fingerprint, config_fingerprint,
        artifact_name, payload, accounting,
    ):
        stage = str(stage)
        if stage not in STAGE_ORDER:
            raise ValueError("unknown_pipeline_stage")
        state = self._read()
        if state is None:
            raise ValueError("pipeline_state_identity_mismatch")
        artifact_path = (self.root / str(artifact_name)).resolve()
        if artifact_path.parent != self.root:
            raise ValueError("invalid_pipeline_artifact_path")
        _atomic_private_json(artifact_path, dict(payload))
        artifact_bytes = artifact_path.read_bytes()
        record = {
            "stage": stage,
            "input_fingerprint": str(input_fingerprint),
            "config_fingerprint": str(config_fingerprint),
            "artifact_path": artifact_path.name,
            "artifact_sha256": _sha256_bytes(artifact_bytes),
            "accounting": dict(accounting or {}),
        }
        state["stages"][stage] = record
        self._write(state)
        return StageCheckpoint(
            stage, record["input_fingerprint"], record["config_fingerprint"],
            str(artifact_path), record["artifact_sha256"], dict(payload),
            dict(record["accounting"]),
        )

    def load_stage(self, stage, input_fingerprint, config_fingerprint):
        stage = str(stage)
        state = self._read()
        if state is None:
            return None
        record = state["stages"].get(stage)
        if not isinstance(record, dict):
            return None
        if (
            record.get("input_fingerprint") != str(input_fingerprint)
            or record.get("config_fingerprint") != str(config_fingerprint)
        ):
            self.invalidate_from(stage)
            return None
        artifact_path = (self.root / str(record.get("artifact_path") or "")).resolve()
        if artifact_path.parent != self.root or not artifact_path.is_file():
            self.invalidate_from(stage)
            return None
        try:
            raw = artifact_path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            self.invalidate_from(stage)
            return None
        if _sha256_bytes(raw) != record.get("artifact_sha256"):
            self.invalidate_from(stage)
            return None
        return StageCheckpoint(
            stage, str(input_fingerprint), str(config_fingerprint),
            str(artifact_path), str(record["artifact_sha256"]), payload,
            dict(record.get("accounting") or {}),
        )

    def load_saved_stage(self, stage, config_fingerprint):
        """Read a current persisted stage after identity, config and hash checks."""
        stage = str(stage)
        state = self._read()
        if state is None:
            return None
        record = state["stages"].get(stage)
        if not isinstance(record, dict):
            return None
        if record.get("config_fingerprint") != str(config_fingerprint):
            return None
        artifact_path = (self.root / str(record.get("artifact_path") or "")).resolve()
        if artifact_path.parent != self.root or not artifact_path.is_file():
            return None
        try:
            raw = artifact_path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return None
        if _sha256_bytes(raw) != record.get("artifact_sha256"):
            return None
        return StageCheckpoint(
            stage, str(record.get("input_fingerprint") or ""),
            str(record.get("config_fingerprint") or ""), str(artifact_path),
            str(record["artifact_sha256"]), payload,
            dict(record.get("accounting") or {}),
        )
