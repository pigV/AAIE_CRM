import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from content_extraction.client import ContentExtractionClient
from content_extraction.repository import ContentExtractionRepository
from content_extraction_service import (
    build_accurate_transcript_coordinator,
    build_enabled_pipeline_runner,
    build_local_first_coordinator,
    build_ocr_adapters,
    build_server,
)
from content_extraction_service_control import ensure_service_token


class ContentExtractionServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.repo = ContentExtractionRepository(self.root / "service.db")
        self.repo.initialize()
        self.token = "test-token-never-log"
        self.server = build_server(
            "127.0.0.1",
            0,
            token=self.token,
            repository=self.repo,
            workspace=self.root,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.client = ContentExtractionClient(self.base_url, self.token, timeout=1.0)

    def raw_request(self, method, path, token, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-AAIE-Extraction-Token": token,
            },
        )
        try:
            response = urllib.request.urlopen(request, timeout=1.0)
        except urllib.error.HTTPError as error:
            response = error
        return response.status, json.loads(response.read().decode("utf-8"))

    def test_lifecycle_health_is_public_and_exposes_only_non_sensitive_identity(self):
        request = urllib.request.Request(
            self.base_url + "/lifecycle-health", method="GET"
        )
        response = urllib.request.urlopen(request, timeout=1.0)
        payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["service_name"], "content_extraction")
        self.assertEqual(payload["port"], self.server.server_address[1])
        self.assertEqual(payload["source_mode"], "test")
        self.assertEqual(payload["runtime_version"], "")
        self.assertNotIn(self.token, json.dumps(payload))
        self.assertNotIn("workspace", payload)

    def test_unauthenticated_health_is_rejected_without_token_echo(self):
        status, payload = self.raw_request("GET", "/health", "wrong")

        self.assertEqual(status, 403)
        self.assertEqual(payload, {"ok": False, "error": "Access Denied"})
        self.assertNotIn(self.token, json.dumps(payload))

    def test_create_returns_accepted_and_worker_advances_same_run(self):
        started = time.monotonic()
        result = self.client.create_run(
            crm_job_id=27,
            raw_input="https://xhslink.com/o/example",
        )

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.payload["crm_job_id"], 27)
        deadline = time.monotonic() + 1.0
        current = result
        while time.monotonic() < deadline:
            current = self.client.get_run(27)
            if current.payload.get("current_stage") == "parsing_link":
                break
            time.sleep(0.01)
        self.assertEqual(current.payload["current_stage"], "parsing_link")

    def test_same_crm_job_id_is_idempotent_and_preserves_first_input(self):
        first = self.client.create_run(28, "first share")
        second = self.client.create_run(28, "different share")

        self.assertEqual(first.payload["run_id"], second.payload["run_id"])
        self.assertEqual(self.repo.get_run(first.payload["run_id"]).raw_input, "first share")

    def test_service_down_is_typed_not_global_exception(self):
        client = ContentExtractionClient("http://127.0.0.1:1", "token", timeout=0.05)

        result = client.health()

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "SERVICE_UNAVAILABLE")
        self.assertEqual(result.user_message, "提取服务不可用")

    def test_invalid_route_and_method_return_safe_json(self):
        missing_status, missing = self.raw_request("GET", "/missing", self.token)
        method_status, method = self.raw_request("POST", "/health", self.token, {})

        self.assertEqual((missing_status, missing["error_code"]), (404, "NOT_FOUND"))
        self.assertEqual((method_status, method["error_code"]), (405, "METHOD_NOT_ALLOWED"))
        self.assertNotIn("Traceback", json.dumps((missing, method)))

    def test_upload_continues_same_crm_run_after_public_media_fallback(self):
        run = self.repo.create_run(29, "https://xhslink.com/restricted")
        self.repo.transition(run.id, "parsing_link")
        self.repo.transition(run.id, "resolving_platform")
        self.repo.transition(
            run.id,
            "needs_upload",
            error_code="VIDEO_UNAVAILABLE",
            error_message="视频已失效或无权访问",
        )
        upload = self.root / "authorized.mp4"
        upload.write_bytes(b"video")

        result = self.client.continue_upload(29, str(upload))

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["run_id"], run.id)
        self.assertEqual(result.payload["current_stage"], "ingesting_upload")
        self.assertEqual(self.repo.get_run(run.id).upload_path, str(upload))

    def test_upload_schedules_injected_local_workflow_without_blocking_response(self):
        class Workflow:
            def __init__(self):
                self.called = threading.Event()
                self.run_id = ""

            def process_upload(self, run_id):
                self.run_id = run_id
                self.called.set()

        workflow = Workflow()
        server = build_server(
            "127.0.0.1", 0, self.token, self.repo, self.root,
            local_workflow=workflow,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        client = ContentExtractionClient(f"http://{host}:{port}", self.token, timeout=1.0)
        run = self.repo.create_run(39, "restricted")
        self.repo.transition(run.id, "parsing_link")
        self.repo.transition(run.id, "resolving_platform")
        self.repo.transition(run.id, "needs_upload", "VIDEO_UNAVAILABLE", "请上传")
        upload = self.root / "workflow.mp4"
        upload.write_bytes(b"video")

        started = time.monotonic()
        response = client.continue_upload(39, str(upload))

        self.assertTrue(response.ok)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(workflow.called.wait(1.0))
        self.assertEqual(workflow.run_id, run.id)

    def test_link_job_schedules_injected_production_runner(self):
        class Runner:
            def __init__(self):
                self.called = threading.Event()
                self.run_id = ""

            def run(self, run_id):
                self.run_id = run_id
                self.called.set()

        runner = Runner()
        server = build_server(
            "127.0.0.1", 0, self.token, self.repo, self.root,
            pipeline_runner=runner,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        client = ContentExtractionClient(f"http://{host}:{port}", self.token, timeout=1.0)

        response = client.create_run(40, "https://xhslink.com/o/real-chain")

        self.assertTrue(response.ok)
        self.assertTrue(runner.called.wait(1.0))
        self.assertEqual(runner.run_id, response.payload["run_id"])

    def test_distinct_production_runs_do_not_execute_pipeline_concurrently(self):
        class Runner:
            def __init__(self):
                self.lock = threading.Lock()
                self.first_started = threading.Event()
                self.release_first = threading.Event()
                self.all_finished = threading.Event()
                self.active = 0
                self.max_active = 0
                self.finished = 0

            def run(self, _run_id):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    is_first = self.finished == 0 and self.active == 1
                if is_first:
                    self.first_started.set()
                    self.release_first.wait(1.0)
                with self.lock:
                    self.active -= 1
                    self.finished += 1
                    if self.finished == 2:
                        self.all_finished.set()

        runner = Runner()
        server = build_server(
            "127.0.0.1", 0, self.token, self.repo, self.root,
            pipeline_runner=runner,
        )
        self.addCleanup(server.server_close)

        server.schedule("run-one")
        self.assertTrue(runner.first_started.wait(1.0))
        server.schedule("run-two")
        time.sleep(0.05)

        self.assertEqual(runner.max_active, 1)
        runner.release_first.set()
        self.assertTrue(runner.all_finished.wait(1.0))
        self.assertEqual(runner.max_active, 1)

    def test_get_run_exposes_sanitized_raw_provider_artifacts_from_runner(self):
        artifacts = {
            "schema_version": "raw-provider-artifacts-v1",
            "source_run_id": "run-artifacts",
            "ocr": {"provider_texts": {"paddle": "原始OCR"}},
            "asr": {"provider_texts": {"funasr": "原始ASR"}},
        }

        class Runner:
            def run(self, _run_id):
                return None

            def raw_provider_artifacts_for_run(self, run_id):
                self.requested_run_id = run_id
                return artifacts

        runner = Runner()
        server = build_server(
            "127.0.0.1", 0, self.token, self.repo, self.root,
            pipeline_runner=runner,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        client = ContentExtractionClient(f"http://{host}:{port}", self.token, timeout=1.0)
        run = self.repo.create_run(401, "share text")

        result = client.get_run(401)

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["raw_provider_artifacts"], artifacts)
        self.assertEqual(runner.requested_run_id, run.id)

    def test_retry_reuses_failed_run_and_clears_safe_error(self):
        run = self.repo.create_run(30, "broken share")
        self.repo.transition(run.id, "failed", "LINK_NOT_FOUND", "未发现有效链接")

        result = self.client.retry_run(30)

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["run_id"], run.id)
        self.assertEqual(result.payload["current_stage"], "parsing_link")
        self.assertEqual(result.payload["error_code"], "")

    def test_retry_from_needs_upload_reschedules_the_same_production_run(self):
        class Runner:
            def __init__(self):
                self.called = threading.Event()
                self.run_id = ""

            def run(self, run_id):
                self.run_id = run_id
                self.called.set()

        runner = Runner()
        server = build_server(
            "127.0.0.1", 0, self.token, self.repo, self.root,
            pipeline_runner=runner,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        client = ContentExtractionClient(f"http://{host}:{port}", self.token, timeout=1.0)
        run = self.repo.create_run(31, "https://xhslink.cn/o/current")
        self.repo.transition(run.id, "parsing_link")
        self.repo.transition(run.id, "resolving_platform")
        self.repo.transition(run.id, "needs_upload", "VIDEO_RESOLVE_FAILED", "视频地址解析失败")

        result = client.retry_run(31)

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["run_id"], run.id)
        self.assertEqual(result.payload["current_stage"], "parsing_link")
        self.assertTrue(runner.called.wait(1.0))
        self.assertEqual(runner.run_id, run.id)

    def test_service_token_is_created_once_with_owner_only_permissions(self):
        cache_root = self.root / "cache"

        first = ensure_service_token(cache_root)
        second = ensure_service_token(cache_root)
        token_path = cache_root / "service.token"

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(token_path.read_text(encoding="utf-8").strip(), first)

    def test_service_main_token_matches_the_existing_crm_client_token_path(self):
        import content_extraction_service as service_module

        builder = getattr(service_module, "build_service_token", None)
        self.assertTrue(callable(builder), "service token builder must be available")
        token = builder(self.root)

        client_token_path = (
            self.root / "cache" / "content_extractions" / "v2" / "service.token"
        )
        self.assertTrue(client_token_path.is_file())
        self.assertEqual(client_token_path.read_text(encoding="utf-8").strip(), token)
        self.assertFalse(
            (self.root / "cache" / "content_extractions" / "runtime" / "service.token").exists()
        )


class AccurateTranscriptServiceContractTests(unittest.TestCase):
    class Coordinator:
        def __init__(self):
            self.submitted = None

        def status_for(self, crm_job_id):
            return {
                "mode": "PILOT", "status": "REVIEW_REQUIRED",
                "review_items": 2, "review_ratio": 0.02,
                "final_available": False, "final_accurate_transcript": "",
                "user_message": "有 2 处需要确认", "error_code": "",
            }

        def review_for(self, crm_job_id):
            return {
                "mode": "PILOT", "queue_hash": "sha256:test", "revision": 0,
                "source_video_id": f"job_{crm_job_id}",
                "items": [{"review_id": "r1", "clip_id": "r1.m4v"}],
            }

        def submit_review(self, crm_job_id, review_id, source_queue_hash,
                          expected_revision, action, selected_text,
                          candidate_references, reviewer_source, now):
            self.submitted = {
                "crm_job_id": crm_job_id, "review_id": review_id,
                "source_queue_hash": source_queue_hash,
                "expected_revision": expected_revision, "action": action,
                "selected_text": selected_text,
                "candidate_references": tuple(candidate_references),
                "reviewer_source": reviewer_source, "now": now,
            }
            return {
                "mode": "PILOT", "status": "HUMAN_REVIEW_COMPLETE",
                "review_items": 1, "review_ratio": 0.01,
                "final_available": True,
                "final_accurate_transcript": "最终文案",
                "user_message": "确认完成", "error_code": "",
            }

        def media_for(self, crm_job_id, clip_id):
            if clip_id != "r1.m4v":
                raise KeyError("MEDIA_NOT_FOUND")
            from content_extraction.accuracy.integration import ReviewMedia
            return ReviewMedia("video/mp4", b"review-media")

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.repo = ContentExtractionRepository(self.root / "service.db")
        self.repo.initialize()
        self.run = self.repo.create_run(81, "share")
        self.token = "service-contract-token"
        self.coordinator = self.Coordinator()
        self.server = build_server(
            "127.0.0.1", 0, self.token, self.repo, self.root,
            accurate_transcript_coordinator=self.coordinator,
            accurate_transcript_enabled=True,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def request(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=body, method=method,
            headers={"Content-Type": "application/json", "X-AAIE-Extraction-Token": self.token},
        )
        try:
            response = urllib.request.urlopen(request, timeout=1)
        except urllib.error.HTTPError as error:
            response = error
        return response.status, response.headers, response.read()

    def test_run_and_accurate_status_are_sanitized(self):
        status, headers, raw = self.request("GET", "/runs/81")
        payload = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(payload["accurate_transcript"]["status"], "REVIEW_REQUIRED")
        self.assertNotIn("provider", raw.decode("utf-8").lower())
        self.assertEqual(headers["Cache-Control"], "no-store")

        status, _headers, raw = self.request("GET", "/runs/81/accurate-transcript")
        payload = json.loads(raw)
        self.assertEqual((status, payload["status"]), (200, "REVIEW_REQUIRED"))
        self.assertEqual(payload["final_accurate_transcript"], "")

    def test_review_projection_and_submission_delegate_to_coordinator(self):
        status, _headers, raw = self.request("GET", "/runs/81/review")
        payload = json.loads(raw)
        self.assertEqual((status, payload["items"][0]["clip_id"]), (200, "r1.m4v"))

        status, _headers, raw = self.request("POST", "/runs/81/review/r1", {
            "source_queue_hash": "sha256:test", "expected_revision": 0,
            "action": "CONFIRMED", "selected_text": "最终",
            "candidate_references": ["alternative:final"],
            "reviewer_source": "crm_quick_review",
        })
        payload = json.loads(raw)
        self.assertEqual((status, payload["status"]), (200, "HUMAN_REVIEW_COMPLETE"))
        self.assertEqual(self.coordinator.submitted["review_id"], "r1")
        self.assertTrue(self.coordinator.submitted["now"])

    def test_review_submission_rejects_extra_fields(self):
        status, _headers, raw = self.request("POST", "/runs/81/review/r1", {
            "source_queue_hash": "sha256:test", "expected_revision": 0,
            "action": "CONFIRMED", "selected_text": "最终",
            "candidate_references": [], "reviewer_source": "crm",
            "internal_path": "/private/file",
        })
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error_code"], "INVALID_REVIEW")
        self.assertIsNone(self.coordinator.submitted)

    def test_media_endpoint_is_binary_and_allowlisted(self):
        status, headers, raw = self.request("GET", "/runs/81/review/media/r1.m4v")
        self.assertEqual((status, headers["Content-Type"], raw), (200, "video/mp4", b"review-media"))
        missing, _headers, raw = self.request("GET", "/runs/81/review/media/missing.m4v")
        self.assertEqual(missing, 404)
        self.assertEqual(json.loads(raw)["error_code"], "MEDIA_NOT_FOUND")

    def test_disabled_accurate_transcript_fails_closed_without_affecting_run(self):
        disabled = build_server(
            "127.0.0.1", 0, self.token, self.repo, self.root,
            accurate_transcript_coordinator=self.coordinator,
            accurate_transcript_enabled=False,
        )
        thread = threading.Thread(target=disabled.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(disabled.server_close)
        self.addCleanup(disabled.shutdown)
        host, port = disabled.server_address
        request = urllib.request.Request(
            f"http://{host}:{port}/runs/81/accurate-transcript",
            headers={"X-AAIE-Extraction-Token": self.token},
        )
        with self.assertRaises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request, timeout=1)
        self.assertEqual(captured.exception.code, 409)
        self.assertEqual(
            json.loads(captured.exception.read())["error_code"],
            "ACCURATE_TRANSCRIPT_DISABLED",
        )
        self.assertIsNotNone(self.repo.get_run_by_crm_job_id(81))


class OCRAdapterAssemblyTests(unittest.TestCase):
    def test_production_runner_is_built_only_when_accurate_pilot_is_enabled(self):
        workflow = object()
        repository = object()
        with mock.patch(
            "content_extraction.production_pipeline.build_production_pipeline_runner",
            return_value="runner",
        ) as factory:
            self.assertIsNone(build_enabled_pipeline_runner(
                repository, Path("/workspace"), {"BILLING_MODE": "FREE_ONLY"},
                workflow, None,
            ))
            self.assertEqual(build_enabled_pipeline_runner(
                repository, Path("/workspace"), {"BILLING_MODE": "FREE_ONLY"},
                workflow, object(),
            ), "runner")
        factory.assert_called_once_with(
            repository, Path("/workspace"), {"BILLING_MODE": "FREE_ONLY"}, workflow,
        )

    def test_accurate_transcript_coordinator_is_built_only_for_exact_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(build_accurate_transcript_coordinator(Path(folder), {}))
            coordinator = build_accurate_transcript_coordinator(Path(folder), {
                "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "true",
                "AAIE_CRM_ACCURATE_TRANSCRIPT_RUNTIME_ROOT": "private/review",
                "AAIE_CRM_ACCURATE_TRANSCRIPT_CLIP_ROOT": "private/clips",
            })
            self.assertEqual(coordinator.runtime_root, (Path(folder) / "private/review").resolve())
            self.assertEqual(coordinator.clip_root, (Path(folder) / "private/clips").resolve())

    def test_local_first_builder_partitions_adapters_and_parses_cloud_order(self):
        class Adapter:
            def __init__(self, provider, billing_type):
                self.provider = provider
                self.billing_type = billing_type
                self.model_version = "v1"

        adapters = (
            Adapter("paddle", "local"), Adapter("apple_vision", "local"),
            Adapter("tencent", "cloud"), Adapter("iflytek", "cloud"),
        )
        coordinator = build_local_first_coordinator(
            object(),
            {"OCR_CLOUD_ARBITRATION_ORDER": "iflytek,tencent"},
            adapters,
        )
        self.assertEqual(
            [item.provider for item in coordinator.local_adapters],
            ["paddle", "apple_vision"],
        )
        self.assertEqual(coordinator.cloud_order, ("iflytek", "tencent"))
        self.assertEqual(set(coordinator.cloud_adapters), {"tencent", "iflytek"})

    def test_default_local_assembly_is_paddle_and_apple_vision(self):
        adapters = build_ocr_adapters(
            {}, paddle_engine=object(), apple_vision_client=object(),
            easyocr_client=object(),
        )

        self.assertEqual([adapter.provider for adapter in adapters], ["paddle", "apple_vision"])
        self.assertEqual(adapters[0].billing_type, "local")
        self.assertEqual(adapters[0].cost, 0)
        self.assertEqual(adapters[1].billing_type, "local")
        self.assertEqual(adapters[1].cost, 0)

    def test_easyocr_requires_explicit_flag_and_remains_available(self):
        adapters = build_ocr_adapters(
            {"AAIE_CRM_CONTENT_EXTRACTION_EASYOCR_ENABLED": "true"},
            paddle_engine=object(), apple_vision_client=object(),
            easyocr_client=object(),
        )
        self.assertEqual(
            [adapter.provider for adapter in adapters],
            ["paddle", "apple_vision", "easyocr"],
        )

    def test_free_only_skips_cloud_credentials_without_explicit_free_confirmation(self):
        adapters = build_ocr_adapters(
            {
                "TENCENT_OCR_SECRET_ID": "id-value",
                "TENCENT_OCR_SECRET_KEY": "key-value",
                "IFLYTEK_OCR_APP_ID": "app-value",
                "IFLYTEK_OCR_API_KEY": "key-value",
                "IFLYTEK_OCR_API_SECRET": "secret-value",
            },
            paddle_engine=object(),
        )

        self.assertEqual([adapter.provider for adapter in adapters], ["paddle", "apple_vision"])

    def test_tencent_adapter_is_added_without_calling_provider(self):
        created = []

        def client_factory(config):
            created.append((config.region, config.endpoint))
            return lambda _data: {}

        adapters = build_ocr_adapters(
            {
                "TENCENT_OCR_SECRET_ID": "id-value",
                "TENCENT_OCR_SECRET_KEY": "key-value",
                "TENCENT_OCR_FREE_ONLY_ALLOWED": "true",
            },
            paddle_engine=object(),
            tencent_client_factory=client_factory,
        )

        self.assertEqual(
            [adapter.provider for adapter in adapters],
            ["paddle", "apple_vision", "tencent"],
        )
        self.assertEqual(created, [("", "ocr.tencentcloudapi.com")])

    def test_iflytek_adapter_is_absent_when_environment_credentials_are_incomplete(self):
        adapters = build_ocr_adapters(
            {"IFLYTEK_OCR_APP_ID": "app", "IFLYTEK_OCR_API_KEY": "key"},
            paddle_engine=object(),
        )
        self.assertEqual([adapter.provider for adapter in adapters], ["paddle", "apple_vision"])

    def test_iflytek_adapter_is_added_without_calling_provider(self):
        created = []

        def client_factory(config):
            created.append((config.app_id, config.endpoint))
            return lambda _data, _format: {}

        adapters = build_ocr_adapters(
            {
                "IFLYTEK_OCR_APP_ID": "app-value",
                "IFLYTEK_OCR_API_KEY": "key-value",
                "IFLYTEK_OCR_API_SECRET": "secret-value",
                "IFLYTEK_OCR_FREE_ONLY_ALLOWED": "true",
            },
            paddle_engine=object(),
            iflytek_client_factory=client_factory,
        )
        self.assertEqual(
            [adapter.provider for adapter in adapters],
            ["paddle", "apple_vision", "iflytek"],
        )
        self.assertEqual(created, [(
            "app-value", "https://api.xf-yun.com/v1/private/sf8e6aca1"
        )])


if __name__ == "__main__":
    unittest.main()
