import json
import selectors
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .base import OCRAdapter, normalized_confidence
from .paddle import ALLOWED_IMAGE_FORMATS, _bounding_box, _polygon


@dataclass(frozen=True)
class AppleVisionWorkerConfig:
    binary_path: str
    state_dir: str
    timeout_seconds: float = 30.0
    languages: tuple = ("zh-Hans", "en-US")
    revision: int = 3
    recognition_level: str = "accurate"
    sandbox_profile: str = ""

    @property
    def model_version(self):
        return f"Apple-Vision-rev{self.revision}-{self.recognition_level}-{'+'.join(self.languages)}"


class AppleVisionWorkerClient:
    """Persistent JSONL client for the isolated native Vision worker."""

    def __init__(self, config, process_factory=None):
        self.config = config
        self.process_factory = process_factory or subprocess.Popen
        self.process = None
        self.lock = threading.Lock()
        self.start_count = 0
        self.inference_count = 0
        self.init_ms = None
        self.worker_exit = "not_started"

    def _read_json(self, timeout):
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("Apple Vision worker is not running")
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(float(timeout)):
                raise TimeoutError("Apple Vision worker timeout")
            line = self.process.stdout.readline()
        finally:
            selector.close()
        if not line:
            raise RuntimeError(f"Apple Vision worker exited with code {self.process.poll()}")
        try:
            payload = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Apple Vision worker returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Apple Vision worker returned invalid payload")
        return payload

    def _command(self):
        command = [str(Path(self.config.binary_path).resolve()), "--jsonl"]
        if self.config.sandbox_profile:
            command = ["/usr/bin/sandbox-exec", "-f", str(Path(self.config.sandbox_profile).resolve()), *command]
        return command

    def _environment(self):
        state_dir = str(Path(self.config.state_dir).resolve())
        return {"PATH": "/usr/bin:/bin", "TMPDIR": state_dir, "HOME": state_dir}

    def start(self):
        if self.process is not None:
            if self.process.poll() is None:
                return
            raise RuntimeError("Apple Vision worker already exited")
        if not Path(self.config.binary_path).is_file():
            raise FileNotFoundError("Apple Vision worker binary is unavailable")
        Path(self.config.state_dir).mkdir(parents=True, exist_ok=True)
        self.process = self.process_factory(
            self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=self._environment(),
        )
        self.start_count += 1
        self.worker_exit = "running"
        ready = self._read_json(self.config.timeout_seconds)
        if ready.get("event") != "ready":
            self.close()
            raise RuntimeError(str(ready.get("error") or "Apple Vision worker startup failed"))
        self.init_ms = ready.get("init_ms")

    def recognize(self, image_path):
        with self.lock:
            self.start()
            request_id = uuid.uuid4().hex
            request = {
                "command": "recognize", "request_id": request_id,
                "image_path": str(Path(image_path).resolve()),
                "languages": list(self.config.languages),
                "revision": int(self.config.revision),
                "recognition_level": self.config.recognition_level,
            }
            self.process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
            payload = self._read_json(self.config.timeout_seconds)
            if payload.get("request_id") != request_id:
                raise RuntimeError("Apple Vision worker response mismatch")
            if payload.get("event") == "error":
                raise RuntimeError(str(payload.get("error") or "Apple Vision inference failed"))
            if payload.get("event") != "result":
                raise RuntimeError("Apple Vision worker returned unexpected event")
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
                    response = self._read_json(min(self.config.timeout_seconds, 2.0))
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
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass


class AppleVisionOCRAdapter(OCRAdapter):
    provider = "apple_vision"
    model_version = "Apple-Vision-rev3-accurate-zh-Hans+en-US"
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
            return self.failed(segment, "dependency_unavailable", "APPLE_VISION_UNAVAILABLE", "Apple Vision 本地 worker 尚未配置")
        path = Path(segment.representative_path)
        if path.suffix.lower().lstrip(".") not in ALLOWED_IMAGE_FORMATS or not path.is_file():
            return self.failed(segment, "failed", "APPLE_VISION_INVALID_IMAGE", "不支持或不存在的 OCR 图片")
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
                    "text": text, "confidence": normalized_confidence(raw.get("confidence")),
                    "polygon": polygon, "bounding_box": _bounding_box(polygon),
                })
            detections.sort(key=lambda item: (item["bounding_box"]["y"], item["bounding_box"]["x"]))
            confidence = sum(item["confidence"] for item in detections) / len(detections) if detections else 0.0
            request_id = f"local:apple_vision:{segment.run_id}:{segment.id}:{segment.image_hash[:12]}"
            return self.completed(
                segment, "".join(item["text"] for item in detections), confidence,
                boxes=detections, request_id=request_id,
                raw_response={
                    "provider": self.provider, "billing_type": "local", "cost": 0,
                    "network_request_count": int(payload.get("network_request_count") or 0),
                    "image": payload.get("image") or {}, "inference_ms": payload.get("inference_ms"),
                    "results": payload.get("detections") or [],
                },
                bounding_boxes=[item["bounding_box"] for item in detections],
                polygons=[item["polygon"] for item in detections],
            )
        except TimeoutError as exc:
            return self.failed(segment, "failed", "APPLE_VISION_WORKER_TIMEOUT", exc)
        except FileNotFoundError as exc:
            return self.failed(segment, "dependency_unavailable", "APPLE_VISION_WORKER_MISSING", exc)
        except Exception as exc:
            return self.failed(segment, "failed", "APPLE_VISION_WORKER_FAILED", exc)
