import hashlib
import json
import os
import selectors
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .base import OCRAdapter, normalized_confidence
from .paddle import ALLOWED_IMAGE_FORMATS, _bounding_box, _polygon


def model_fingerprint(model_dir):
    root = Path(model_dir)
    digest = hashlib.sha256()
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and not path.name.startswith("._")
    ) if root.is_dir() else []
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(str(path.stat().st_size).encode("ascii"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()[:16] if files else "models-missing"


@dataclass(frozen=True)
class EasyOCRWorkerConfig:
    python_path: str
    worker_path: str
    model_dir: str
    languages: tuple = ("ch_sim", "en")
    timeout_seconds: float = 120.0
    engine_version: str = "1.7.2"

    @property
    def fingerprint(self):
        return model_fingerprint(self.model_dir)

    @property
    def model_version(self):
        languages = "+".join(self.languages)
        return f"EasyOCR-{self.engine_version}-{languages}-{self.fingerprint}"


class EasyOCRWorkerClient:
    """Persistent JSONL subprocess client; the CRM process never imports torch/EasyOCR."""

    def __init__(self, config, process_factory=None):
        self.config = config
        self.process_factory = process_factory or subprocess.Popen
        self.process = None
        self.lock = threading.Lock()
        self.start_count = 0
        self.inference_count = 0
        self.init_ms = None
        self.worker_exit = "not_started"

    def _minimal_environment(self):
        python_dir = str(Path(self.config.python_path).resolve().parent)
        model_parent = str(Path(self.config.model_dir).resolve().parent)
        return {
            "PATH": f"{python_dir}:/usr/bin:/bin",
            "PYTHONUNBUFFERED": "1",
            "PYTHONNOUSERSITE": "1",
            "TMPDIR": model_parent,
        }

    def _read_json(self, timeout):
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("EasyOCR worker is not running")
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(float(timeout)):
                raise TimeoutError("EasyOCR worker timeout")
            line = self.process.stdout.readline()
        finally:
            selector.close()
        if not line:
            code = self.process.poll()
            message = "EasyOCR worker exited"
            if code is not None:
                message += f" with code {code}"
            raise RuntimeError(message)
        try:
            payload = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("EasyOCR worker returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("EasyOCR worker returned invalid payload")
        return payload

    def start(self):
        if self.process is not None:
            if self.process.poll() is None:
                return
            raise RuntimeError("EasyOCR worker already exited")
        python_path = Path(self.config.python_path)
        worker_path = Path(self.config.worker_path)
        model_dir = Path(self.config.model_dir)
        if not python_path.is_file() or not worker_path.is_file():
            raise FileNotFoundError("EasyOCR worker runtime is unavailable")
        if not model_dir.is_dir() or not any(model_dir.rglob("*.pth")):
            raise FileNotFoundError("EasyOCR local models are unavailable")
        command = [
            str(python_path), str(worker_path),
            "--model-dir", str(model_dir),
            "--languages", ",".join(self.config.languages),
            "--offline",
        ]
        self.process = self.process_factory(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=self._minimal_environment(),
        )
        self.start_count += 1
        self.worker_exit = "running"
        try:
            ready = self._read_json(self.config.timeout_seconds)
        except Exception:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
            self.worker_exit = "startup_failed"
            raise
        if ready.get("event") != "ready":
            self.process.terminate()
            self.process.wait(timeout=2)
            self.worker_exit = "startup_failed"
            raise RuntimeError(str(ready.get("error") or "EasyOCR worker failed to start"))
        self.init_ms = ready.get("init_ms")

    def recognize(self, image_path):
        with self.lock:
            self.start()
            request_id = uuid.uuid4().hex
            request = {
                "command": "recognize",
                "request_id": request_id,
                "image_path": str(Path(image_path).resolve()),
            }
            try:
                self.process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeError("EasyOCR worker pipe closed") from exc
            payload = self._read_json(self.config.timeout_seconds)
            if payload.get("request_id") != request_id:
                raise RuntimeError("EasyOCR worker response mismatch")
            if payload.get("event") == "error":
                raise RuntimeError(str(payload.get("error") or "EasyOCR inference failed"))
            if payload.get("event") != "result":
                raise RuntimeError("EasyOCR worker returned unexpected event")
            self.inference_count += 1
            return payload

    def close(self):
        with self.lock:
            if self.process is None:
                return
            process = self.process
            if process.poll() is None:
                try:
                    process.stdin.write('{"command":"shutdown"}\n')
                    process.stdin.flush()
                    response = self._read_json(min(float(self.config.timeout_seconds), 2.0))
                    if response.get("event") == "shutdown":
                        self.worker_exit = str(response.get("worker_exit") or "clean")
                    process.wait(timeout=2)
                except Exception:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                    self.worker_exit = "terminated"
            elif self.worker_exit == "running":
                self.worker_exit = f"exited:{process.returncode}"
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_args):
        self.close()


class EasyOCRAdapter(OCRAdapter):
    provider = "easyocr"
    model_version = "EasyOCR-1.7.2-ch_sim+en-models-missing"
    request_id_source = "local"
    billing_type = "local"
    cost = 0

    def __init__(self, client=None, sensitive_values=()):
        super().__init__(sensitive_values)
        self.client = client
        config = getattr(client, "config", None)
        if config is not None:
            self.model_version = config.model_version

    def recognize(self, segment):
        if self.client is None:
            return self.failed(
                segment, "dependency_unavailable", "EASYOCR_UNAVAILABLE",
                "EasyOCR 独立本地运行环境尚未配置",
            )
        path = Path(segment.representative_path)
        if path.suffix.lower().lstrip(".") not in ALLOWED_IMAGE_FORMATS or not path.is_file():
            return self.failed(
                segment, "failed", "EASYOCR_INVALID_IMAGE", "不支持或不存在的 OCR 图片"
            )
        try:
            payload = self.client.recognize(path)
            detections = []
            for raw in payload.get("detections") or ():
                if not isinstance(raw, dict):
                    continue
                text = str(raw.get("text") or "").strip()
                polygon = _polygon(raw.get("polygon"))
                if not text or not polygon:
                    continue
                detections.append({
                    "text": text,
                    "confidence": normalized_confidence(raw.get("confidence")),
                    "polygon": polygon,
                    "bounding_box": _bounding_box(polygon),
                })
            detections.sort(key=lambda item: (
                item["bounding_box"]["y"], item["bounding_box"]["x"]
            ))
            confidence = (
                sum(item["confidence"] for item in detections) / len(detections)
                if detections else 0.0
            )
            request_id = (
                f"local:easyocr:{segment.run_id}:{segment.id}:{segment.image_hash[:12]}"
            )
            return self.completed(
                segment,
                "".join(item["text"] for item in detections),
                confidence,
                boxes=detections,
                request_id=request_id,
                raw_response={
                    "provider": "easyocr",
                    "billing_type": "local",
                    "cost": 0,
                    "network_request_count": int(payload.get("network_request_count") or 0),
                    "image": payload.get("image") or {},
                    "inference_ms": payload.get("inference_ms"),
                    "worker_inference_count": payload.get("worker_inference_count"),
                    "results": payload.get("detections") or [],
                },
                bounding_boxes=[item["bounding_box"] for item in detections],
                polygons=[item["polygon"] for item in detections],
            )
        except TimeoutError as exc:
            return self.failed(segment, "failed", "EASYOCR_WORKER_TIMEOUT", exc)
        except FileNotFoundError as exc:
            return self.failed(segment, "dependency_unavailable", "EASYOCR_MODEL_MISSING", exc)
        except Exception as exc:
            return self.failed(segment, "failed", "EASYOCR_WORKER_FAILED", exc)
