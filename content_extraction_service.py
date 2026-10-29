import hmac
import json
import os
import re
import threading
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from content_extraction.orchestrator import Phase2Orchestrator
from production_environment import (
    PRODUCTION_ROOT,
    load_canonical_production_environment,
)


MAX_REQUEST_BYTES = 1024 * 1024


def bootstrap_service_environment(*, root=PRODUCTION_ROOT, environ=None):
    return load_canonical_production_environment(root=root, environ=environ)


def _run_payload(run, repository=None, pipeline_runner=None):
    payload = {
        "ok": True,
        "run_id": run.id,
        "crm_job_id": run.crm_job_id,
        "status": run.status,
        "current_stage": run.current_stage,
        "error_code": run.error_code,
        "user_message": run.error_message,
        "video_path": run.video_path,
    }
    if repository is not None:
        payload["recommended_text"] = repository.recommended_text_for_run(run.id)
    if pipeline_runner is not None:
        try:
            artifacts = pipeline_runner.raw_provider_artifacts_for_run(run.id)
        except Exception:
            artifacts = {}
        if isinstance(artifacts, dict) and artifacts:
            payload["raw_provider_artifacts"] = artifacts
    return payload


class ExtractionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, token, repository, workspace, local_workflow=None,
                 accurate_transcript_coordinator=None, accurate_transcript_enabled=False,
                 pipeline_runner=None, source_mode="test", runtime_version=""):
        super().__init__(address, handler)
        self.token = token
        self.repository = repository
        self.workspace = str(Path(workspace).resolve())
        self.orchestrator = Phase2Orchestrator(repository)
        self.local_workflow = local_workflow
        self.accurate_transcript_coordinator = accurate_transcript_coordinator
        self.accurate_transcript_enabled = bool(accurate_transcript_enabled)
        self.pipeline_runner = pipeline_runner
        self.source_mode = str(source_mode or "test")
        self.runtime_version = str(runtime_version or "")
        self.active_runs = set()
        self.active_lock = threading.Lock()
        self.pipeline_execution_lock = threading.Lock()

    def schedule(self, run_id):
        with self.active_lock:
            if run_id in self.active_runs:
                return
            self.active_runs.add(run_id)

        def worker():
            try:
                if self.pipeline_runner is not None:
                    with self.pipeline_execution_lock:
                        self.pipeline_runner.run(run_id)
                    return
                run = self.repository.get_run(run_id)
                if run and run.current_stage == "created":
                    self.orchestrator.advance_run(run_id)
                    run = self.repository.get_run(run_id)
                if (
                    run and run.current_stage == "ingesting_upload"
                    and self.local_workflow is not None
                ):
                    self.local_workflow.process_upload(run_id)
            except Exception:
                current = self.repository.get_run(run_id)
                if current and current.current_stage not in {"downloaded", "failed", "needs_upload"}:
                    try:
                        self.repository.transition(
                            run_id,
                            "failed",
                            error_code="WORKER_FAILED",
                            error_message="提取任务执行失败",
                        )
                    except Exception:
                        pass
            finally:
                with self.active_lock:
                    self.active_runs.discard(run_id)

        threading.Thread(target=worker, daemon=True).start()


class ExtractionRequestHandler(BaseHTTPRequestHandler):
    server: ExtractionHTTPServer

    def log_message(self, _format, *args):
        return

    def _write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _write_bytes(self, status, body, content_type):
        self.send_response(int(status))
        self.send_header("Content-Type", str(content_type))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _accurate_enabled(self):
        if self.server.accurate_transcript_enabled and self.server.accurate_transcript_coordinator is not None:
            return True
        self._write_json(409, {
            "ok": False,
            "error_code": "ACCURATE_TRANSCRIPT_DISABLED",
            "user_message": "精准文案 PILOT 当前未启用",
        })
        return False

    def _accurate_error(self, error):
        code = str(error.args[0] if getattr(error, "args", ()) else error)
        mapping = {
            "REVIEW_NOT_AVAILABLE": (404, "REVIEW_NOT_AVAILABLE", "当前任务没有审核内容"),
            "MEDIA_NOT_FOUND": (404, "MEDIA_NOT_FOUND", "审核片段不存在"),
            "STALE_RESOLUTION": (409, "STALE_RESOLUTION", "审核内容已更新，请刷新后重试"),
            "revision_conflict": (409, "REVISION_CONFLICT", "审核进度已更新，请刷新后重试"),
            "resolution_conflict": (409, "REVISION_CONFLICT", "该位置已有不同的审核结果"),
        }
        status, error_code, message = mapping.get(
            code, (400, "INVALID_REVIEW", "审核内容无效"),
        )
        self._write_json(status, {
            "ok": False, "error_code": error_code, "user_message": message,
        })

    def _authenticated(self):
        supplied = self.headers.get("X-AAIE-Extraction-Token", "")
        if not hmac.compare_digest(str(supplied), str(self.server.token)):
            self._write_json(403, {"ok": False, "error": "Access Denied"})
            return False
        return True

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > MAX_REQUEST_BYTES:
            return None
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def do_GET(self):
        if self.path == "/lifecycle-health":
            self._write_json(
                200,
                {
                    "ok": True,
                    "service_name": "content_extraction",
                    "port": int(self.server.server_address[1]),
                    "source_mode": self.server.source_mode,
                    "runtime_version": self.server.runtime_version,
                },
            )
            return
        if not self._authenticated():
            return
        if self.path == "/health":
            self._write_json(
                200,
                {"ok": True, "status": "ok", "workspace": self.server.workspace},
            )
            return
        match = re.fullmatch(r"/runs/(\d+)", self.path)
        if match:
            run = self.server.repository.get_run_by_crm_job_id(int(match.group(1)))
            if run is None:
                self._write_json(404, {"ok": False, "error_code": "NOT_FOUND", "user_message": "提取任务不存在"})
            else:
                payload = _run_payload(
                    run, self.server.repository, self.server.pipeline_runner,
                )
                if self.server.accurate_transcript_enabled and self.server.accurate_transcript_coordinator is not None:
                    try:
                        payload["accurate_transcript"] = self.server.accurate_transcript_coordinator.status_for(run.crm_job_id)
                    except Exception:
                        payload["accurate_transcript"] = {
                            "mode": "PILOT",
                            "status": "ACCURATE_TRANSCRIPT_INCOMPLETE",
                            "final_available": False,
                            "final_accurate_transcript": "",
                            "user_message": "当前无法生成准确文案",
                            "error_code": "ACCURATE_TRANSCRIPT_UNAVAILABLE",
                        }
                self._write_json(200, payload)
            return
        accurate_match = re.fullmatch(r"/runs/(\d+)/accurate-transcript", self.path)
        if accurate_match:
            if not self._accurate_enabled():
                return
            try:
                payload = self.server.accurate_transcript_coordinator.status_for(int(accurate_match.group(1)))
            except Exception:
                self._write_json(503, {"ok": False, "error_code": "ACCURATE_TRANSCRIPT_UNAVAILABLE", "user_message": "精准文案服务暂不可用"})
                return
            self._write_json(200, {"ok": True, **payload})
            return
        media_match = re.fullmatch(r"/runs/(\d+)/review/media/([^/]+)", self.path)
        if media_match:
            if not self._accurate_enabled():
                return
            try:
                media = self.server.accurate_transcript_coordinator.media_for(
                    int(media_match.group(1)), unquote(media_match.group(2)),
                )
            except (KeyError, ValueError) as error:
                self._accurate_error(error)
                return
            except Exception:
                self._write_json(503, {"ok": False, "error_code": "ACCURATE_TRANSCRIPT_UNAVAILABLE", "user_message": "审核片段暂不可用"})
                return
            self._write_bytes(200, media.body, media.content_type)
            return
        review_match = re.fullmatch(r"/runs/(\d+)/review", self.path)
        if review_match:
            if not self._accurate_enabled():
                return
            try:
                payload = self.server.accurate_transcript_coordinator.review_for(int(review_match.group(1)))
            except (KeyError, ValueError) as error:
                self._accurate_error(error)
                return
            except Exception:
                self._write_json(503, {"ok": False, "error_code": "ACCURATE_TRANSCRIPT_UNAVAILABLE", "user_message": "审核服务暂不可用"})
                return
            self._write_json(200, {"ok": True, **payload})
            return
        if self.path == "/runs":
            self._write_json(405, {"ok": False, "error_code": "METHOD_NOT_ALLOWED", "user_message": "请求方法不支持"})
            return
        self._write_json(404, {"ok": False, "error_code": "NOT_FOUND", "user_message": "接口不存在"})

    def do_POST(self):
        if not self._authenticated():
            return
        if self.path == "/health":
            self._write_json(405, {"ok": False, "error_code": "METHOD_NOT_ALLOWED", "user_message": "请求方法不支持"})
            return
        if self.path == "/runs":
            payload = self._read_json()
            if payload is None or not isinstance(payload.get("crm_job_id"), int):
                self._write_json(400, {"ok": False, "error_code": "INVALID_REQUEST", "user_message": "请求内容无效"})
                return
            run = self.server.orchestrator.create_run(
                payload["crm_job_id"],
                str(payload.get("raw_input") or ""),
                str(payload.get("upload_path") or ""),
            )
            self.server.schedule(run.id)
            self._write_json(202, _run_payload(run, self.server.repository))
            return
        review_match = re.fullmatch(r"/runs/(\d+)/review/([^/]+)", self.path)
        if review_match:
            if not self._accurate_enabled():
                return
            payload = self._read_json()
            expected_fields = {
                "source_queue_hash", "expected_revision", "action", "selected_text",
                "candidate_references", "reviewer_source",
            }
            if not isinstance(payload, dict) or set(payload) != expected_fields:
                self._write_json(400, {"ok": False, "error_code": "INVALID_REVIEW", "user_message": "审核内容无效"})
                return
            if not isinstance(payload.get("candidate_references"), list):
                self._write_json(400, {"ok": False, "error_code": "INVALID_REVIEW", "user_message": "审核内容无效"})
                return
            review_id = unquote(review_match.group(2))
            if not review_id or Path(review_id).name != review_id:
                self._write_json(400, {"ok": False, "error_code": "INVALID_REVIEW", "user_message": "审核内容无效"})
                return
            try:
                result = self.server.accurate_transcript_coordinator.submit_review(
                    int(review_match.group(1)), review_id,
                    str(payload["source_queue_hash"]), int(payload["expected_revision"]),
                    str(payload["action"]), str(payload["selected_text"]),
                    tuple(str(value) for value in payload["candidate_references"]),
                    str(payload["reviewer_source"]),
                    datetime.now(timezone.utc).isoformat(),
                )
            except (KeyError, ValueError) as error:
                self._accurate_error(error)
                return
            except Exception:
                self._write_json(503, {"ok": False, "error_code": "ACCURATE_TRANSCRIPT_UNAVAILABLE", "user_message": "审核服务暂不可用"})
                return
            self._write_json(200, {"ok": True, **result})
            return
        upload_match = re.fullmatch(r"/runs/(\d+)/upload", self.path)
        if upload_match:
            payload = self._read_json()
            upload_path = str((payload or {}).get("upload_path") or "").strip()
            if not upload_path:
                self._write_json(400, {"ok": False, "error_code": "INVALID_REQUEST", "user_message": "上传路径无效"})
                return
            run = self.server.repository.get_run_by_crm_job_id(int(upload_match.group(1)))
            if run is None:
                self._write_json(404, {"ok": False, "error_code": "NOT_FOUND", "user_message": "提取任务不存在"})
                return
            self.server.repository.set_upload_path(run.id, upload_path)
            try:
                if run.current_stage == "needs_upload":
                    run = self.server.repository.transition(run.id, "ingesting_upload")
                elif run.current_stage == "created":
                    run = self.server.orchestrator.advance_run(run.id)
                else:
                    raise ValueError("run is not waiting for upload")
            except ValueError:
                self._write_json(409, {"ok": False, "error_code": "INVALID_STATE", "user_message": "当前任务不能继续上传"})
                return
            self.server.schedule(run.id)
            self._write_json(202, _run_payload(run, self.server.repository))
            return
        retry_match = re.fullmatch(r"/runs/(\d+)/retry", self.path)
        if retry_match:
            run = self.server.repository.get_run_by_crm_job_id(int(retry_match.group(1)))
            if run is None:
                self._write_json(404, {"ok": False, "error_code": "NOT_FOUND", "user_message": "提取任务不存在"})
                return
            try:
                run = self.server.orchestrator.retry_run(run.id)
            except ValueError:
                self._write_json(409, {"ok": False, "error_code": "INVALID_STATE", "user_message": "当前任务不能重试"})
                return
            self.server.schedule(run.id)
            self._write_json(202, _run_payload(run, self.server.repository))
            return
        self._write_json(404, {"ok": False, "error_code": "NOT_FOUND", "user_message": "接口不存在"})


def build_server(host, port, token, repository, workspace, local_workflow=None,
                 accurate_transcript_coordinator=None,
                 accurate_transcript_enabled=False, pipeline_runner=None,
                 source_mode="test", runtime_version=""):
    if host != "127.0.0.1":
        raise ValueError("content extraction service must bind to 127.0.0.1")
    if not token:
        raise ValueError("service token is required")
    return ExtractionHTTPServer(
        (host, int(port)),
        ExtractionRequestHandler,
        token,
        repository,
        workspace,
        local_workflow,
        accurate_transcript_coordinator,
        accurate_transcript_enabled,
        pipeline_runner,
        source_mode,
        runtime_version,
    )


def build_ocr_adapters(
    environ, paddle_engine=None, tencent_client_factory=None,
    iflytek_client_factory=None, easyocr_client=None, apple_vision_client=None,
):
    from content_extraction.billing import FreeOnlyBillingGate
    from content_extraction.ocr.paddle import PaddleOCRAdapter, PaddleOCRV5Engine
    from content_extraction.ocr.tencent import (
        TencentOCRAdapter,
        TencentOCRConfig,
        TencentSDKGeneralAccurateClient,
    )
    from content_extraction.ocr.iflytek import (
        IFlytekOCRAdapter,
        IFlytekOCRConfig,
        IFlytekUniversalOCRClient,
    )
    from content_extraction.ocr.easyocr import (
        EasyOCRAdapter, EasyOCRWorkerClient, EasyOCRWorkerConfig,
    )
    from content_extraction.ocr.apple_vision import (
        AppleVisionOCRAdapter, AppleVisionWorkerClient, AppleVisionWorkerConfig,
    )

    billing_gate = FreeOnlyBillingGate.from_environ(environ)
    workspace = Path(str(environ.get("AAIE_CRM_WORKSPACE") or Path.cwd())).resolve()

    def project_path(name, default):
        value = Path(str(environ.get(name) or default))
        return value if value.is_absolute() else workspace / value

    engine = paddle_engine or PaddleOCRV5Engine()
    adapters = [PaddleOCRAdapter(engine=engine)]
    apple_config = AppleVisionWorkerConfig(
        binary_path=str(project_path(
            "AAIE_CRM_CONTENT_EXTRACTION_APPLE_VISION_BINARY",
            "cache/content_extractions/runtime/ocr/apple_vision/bin/apple-vision-worker",
        )),
        state_dir=str(project_path(
            "AAIE_CRM_CONTENT_EXTRACTION_APPLE_VISION_STATE_DIR",
            "cache/content_extractions/runtime/ocr/apple_vision/state",
        )),
        timeout_seconds=float(
            environ.get("AAIE_CRM_CONTENT_EXTRACTION_APPLE_VISION_TIMEOUT_SECONDS") or "30"
        ),
        sandbox_profile=str(project_path(
            "AAIE_CRM_CONTENT_EXTRACTION_APPLE_VISION_SANDBOX_PROFILE",
            "cache/content_extractions/runtime/ocr/apple_vision/state/network-deny.sb",
        )),
    )
    adapters.append(AppleVisionOCRAdapter(
        client=apple_vision_client or AppleVisionWorkerClient(apple_config)
    ))
    easyocr_enabled = str(
        environ.get("AAIE_CRM_CONTENT_EXTRACTION_EASYOCR_ENABLED") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if easyocr_enabled and easyocr_client is not None:
        adapters.append(EasyOCRAdapter(client=easyocr_client))
    elif easyocr_enabled:
        config = EasyOCRWorkerConfig(
            python_path=str(project_path(
                "AAIE_CRM_CONTENT_EXTRACTION_EASYOCR_PYTHON",
                "cache/content_extractions/runtime/easyocr-venv/bin/python",
            )),
            worker_path=str(workspace / "content_extraction/easyocr_worker.py"),
            model_dir=str(project_path(
                "AAIE_CRM_CONTENT_EXTRACTION_EASYOCR_MODEL_DIR",
                "cache/content_extractions/runtime/easyocr-models",
            )),
            timeout_seconds=float(
                environ.get("AAIE_CRM_CONTENT_EXTRACTION_EASYOCR_TIMEOUT_SECONDS") or "120"
            ),
        )
        adapters.append(EasyOCRAdapter(client=EasyOCRWorkerClient(config)))
    tencent_config = TencentOCRConfig.from_environ(environ)
    if tencent_config is not None and billing_gate.allows("tencent", "cloud"):
        factory = tencent_client_factory or (
            lambda config: TencentSDKGeneralAccurateClient(config)
        )
        adapters.append(TencentOCRAdapter(
            client=factory(tencent_config),
            sensitive_values=(tencent_config.secret_id, tencent_config.secret_key),
        ))
    iflytek_config = IFlytekOCRConfig.from_environ(environ)
    if iflytek_config is not None and billing_gate.allows("iflytek", "cloud"):
        factory = iflytek_client_factory or (
            lambda config: IFlytekUniversalOCRClient(config)
        )
        adapters.append(IFlytekOCRAdapter(
            client=factory(iflytek_config),
            sensitive_values=(
                iflytek_config.app_id,
                iflytek_config.api_key,
                iflytek_config.api_secret,
            ),
        ))
    return tuple(adapters)


def build_local_first_coordinator(repository, environ, adapters):
    from content_extraction.billing import FreeOnlyBillingGate
    from content_extraction.ocr.local_first import LocalFirstOCRFusionCoordinator

    local_adapters = tuple(
        adapter for adapter in adapters
        if str(getattr(adapter, "billing_type", "")).lower() == "local"
    )
    cloud_adapters = tuple(
        adapter for adapter in adapters
        if str(getattr(adapter, "billing_type", "")).lower() == "cloud"
    )
    configured = tuple(
        item.strip().lower()
        for item in str(
            environ.get("OCR_CLOUD_ARBITRATION_ORDER") or "tencent,iflytek"
        ).split(",")
        if item.strip().lower() in {"tencent", "iflytek"}
    )
    cloud_order = tuple(dict.fromkeys(configured)) or ("tencent", "iflytek")
    return LocalFirstOCRFusionCoordinator(
        repository,
        local_adapters,
        cloud_adapters,
        FreeOnlyBillingGate.from_environ(environ),
        cloud_order=cloud_order,
    )


def build_local_workflow(
    repository, workspace, environ=None, paddle_engine=None,
    tencent_client_factory=None, iflytek_client_factory=None, easyocr_client=None,
    apple_vision_client=None,
):
    from content_extraction.downloaders.local_upload import LocalUploadAdapter
    from content_extraction.local_pipeline import LocalExtractionWorkflow, LocalSubtitlePipeline
    from content_extraction.preprocessing.ffmpeg_frames import FFmpegFrameExtractor, SamplingProfile
    from content_extraction.preprocessing.opencv_subtitles import OpenCVSubtitleDetector, PreprocessingProfile

    environ = environ or os.environ
    workspace = Path(workspace).resolve()

    def project_path(name, default):
        value = str(environ.get(name) or default)
        path = Path(value)
        return path if path.is_absolute() else workspace / path

    ffmpeg_path = project_path(
        "AAIE_CRM_CONTENT_EXTRACTION_FFMPEG_PATH",
        "vendor/content_extraction/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1",
    )
    if not ffmpeg_path.is_file():
        raise FileNotFoundError("content extraction FFmpeg is unavailable")
    media_root = project_path(
        "AAIE_CRM_CONTENT_EXTRACTION_DATA_ROOT", "data/content_extraction"
    ) / "media"
    cache_root = project_path(
        "AAIE_CRM_CONTENT_EXTRACTION_CACHE_ROOT", "cache/content_extractions/v2"
    )
    fps = float(environ.get("AAIE_CRM_CONTENT_EXTRACTION_FRAME_FPS") or "2")
    max_frames = int(environ.get("AAIE_CRM_CONTENT_EXTRACTION_MAX_FRAMES") or "1800")
    timeout_seconds = int(
        environ.get("AAIE_CRM_CONTENT_EXTRACTION_FFMPEG_TIMEOUT_SECONDS") or "900"
    )
    sampling = SamplingProfile(
        str(environ.get("AAIE_CRM_CONTENT_EXTRACTION_SAMPLING_PROFILE") or "initial-2fps"),
        fps,
        max_frames,
        timeout_seconds,
    )
    preprocessing_version = str(
        environ.get("AAIE_CRM_CONTENT_EXTRACTION_PREPROCESSING_VERSION")
        or "opencv-subtitle-v1"
    )
    detector = OpenCVSubtitleDetector(
        PreprocessingProfile(preprocessing_version, minimum_score=0.02)
    )
    adapters = build_ocr_adapters(
        environ,
        paddle_engine=paddle_engine,
        tencent_client_factory=tencent_client_factory,
        iflytek_client_factory=iflytek_client_factory,
        easyocr_client=easyocr_client,
        apple_vision_client=apple_vision_client,
    )
    fusion_coordinator = build_local_first_coordinator(repository, environ, adapters)
    pipeline = LocalSubtitlePipeline(
        repository,
        FFmpegFrameExtractor(ffmpeg_path),
        detector,
        lambda: fusion_coordinator,
        cache_root,
        sampling,
        preprocessing_version,
    )
    return LocalExtractionWorkflow(
        repository, LocalUploadAdapter(media_root), pipeline
    )


def build_accurate_transcript_coordinator(workspace, environ=None):
    from content_extraction.accuracy.integration import AccurateTranscriptCoordinator

    environ = environ or os.environ
    enabled = str(
        environ.get("AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED") or "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None
    workspace = Path(workspace).resolve()

    def project_path(name, default):
        value = Path(str(environ.get(name) or default))
        return value if value.is_absolute() else workspace / value

    runtime_root = project_path(
        "AAIE_CRM_ACCURATE_TRANSCRIPT_RUNTIME_ROOT",
        "cache/content_extractions/runtime/accurate_transcript/quick_review",
    )
    clip_root = project_path(
        "AAIE_CRM_ACCURATE_TRANSCRIPT_CLIP_ROOT",
        "cache/content_extractions/runtime/accurate_transcript/quick_review/clips",
    )
    return AccurateTranscriptCoordinator(runtime_root, clip_root=clip_root)


def build_enabled_pipeline_runner(
    repository, workspace, environ, local_workflow, accurate_coordinator,
):
    if accurate_coordinator is None:
        return None
    from content_extraction.production_pipeline import build_production_pipeline_runner
    return build_production_pipeline_runner(
        repository, Path(workspace), environ, local_workflow,
    )


def build_service_token(workspace):
    from content_extraction_service_control import ensure_service_token

    token_root = Path(workspace).resolve() / "cache/content_extractions/v2"
    return ensure_service_token(token_root)


def main(argv=None):
    from content_extraction.repository import ContentExtractionRepository

    parser = argparse.ArgumentParser(description="AAIE content extraction service")
    parser.add_argument("--workspace", default=str(Path.cwd()))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--source-mode", choices=("production", "test"), default="production")
    parser.add_argument("--runtime-python", default="")
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).resolve()
    environment_root = PRODUCTION_ROOT if args.source_mode == "production" else workspace
    bootstrap_service_environment(root=environment_root)
    cache_root = workspace / "cache/content_extractions/runtime"
    runtime_version = ""
    manifest_path = cache_root / "production-runtime-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        runtime_version = str(manifest.get("runtime_version") or "")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        pass
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(cache_root / "paddlex"))
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    repository = ContentExtractionRepository(
        workspace / "database/content_extraction_service.db"
    )
    repository.initialize()
    repository.recover_interrupted_runs()
    token = build_service_token(workspace)
    workflow = build_local_workflow(repository, workspace)
    accurate_coordinator = build_accurate_transcript_coordinator(workspace)
    pipeline_runner = build_enabled_pipeline_runner(
        repository, workspace, os.environ, workflow, accurate_coordinator,
    )
    server = build_server(
        args.host, args.port, token, repository, workspace,
        local_workflow=workflow,
        accurate_transcript_coordinator=accurate_coordinator,
        accurate_transcript_enabled=accurate_coordinator is not None,
        pipeline_runner=pipeline_runner,
        source_mode=args.source_mode,
        runtime_version=runtime_version,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
