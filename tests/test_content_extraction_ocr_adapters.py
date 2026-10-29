import base64
import datetime
import tempfile
import unittest
import json
from unittest.mock import patch
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from content_extraction.models import SubtitleSegment
from content_extraction.ocr.iflytek import (
    IFlytekHTTPError,
    IFlytekOCRAdapter,
    IFlytekOCRConfig,
    IFlytekUniversalOCRClient,
    build_iflytek_auth_url,
    build_iflytek_request_body,
)
from content_extraction.ocr.paddle import PaddleOCRAdapter, PaddleOCRV5Engine
from content_extraction.ocr.easyocr import EasyOCRAdapter
from content_extraction.ocr.tencent import (
    TencentGeneralAccurateOCRAdapter,
    TencentOCRAdapter,
    TencentOCRConfig,
    TencentSDKGeneralAccurateClient,
)


def segment(path):
    return SubtitleSegment("seg-1", "run-1", 1200, 4650, str(path), "sha", "crop-v1")


class FakePaddleEngine:
    def predict(self, path):
        return [{
            "text": "孩子最好在十岁以前出国", "score": 0.92,
            "box": [[1, 2], [21, 2], [21, 7], [1, 7]],
        }]


class FakePaddleResult(dict):
    def __init__(self):
        super().__init__({"input_path": "crop.png", "rec_texts": []})

    @property
    def json(self):
        return {
            "res": {
                "rec_texts": ["孩子最好在10岁以前", "VISA 590"],
                "rec_scores": [0.99, 0.88],
                "rec_polys": [
                    [[1, 2], [21, 2], [21, 7], [1, 7]],
                    [[5, 6], [25, 6], [25, 12], [5, 12]],
                ],
            }
        }


class OCRAdapterContractTests(unittest.TestCase):
    def test_four_adapters_are_independent_types_with_one_result_shape(self):
        class EasyClient:
            def recognize(self, _path):
                return {
                    "detections": [{
                        "polygon": [[1, 2], [21, 2], [21, 7], [1, 7]],
                        "text": "孩子最好在10岁以前出国",
                        "confidence": 0.95,
                    }],
                    "network_request_count": 0,
                }

        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "crop.jpg"
            image.write_bytes(b"not-a-real-secret-image")
            adapters = (
                PaddleOCRAdapter(engine=FakePaddleEngine()),
                TencentGeneralAccurateOCRAdapter(
                    client=lambda _data: {
                        "TextDetections": [{"DetectedText": "孩子最好在10岁以前出国", "Confidence": 96,
                                            "ItemPolygon": {"X": 1, "Y": 2, "Width": 20, "Height": 5},
                                            "Polygon": [
                                                {"X": 1, "Y": 2}, {"X": 21, "Y": 2},
                                                {"X": 21, "Y": 7}, {"X": 1, "Y": 7},
                                            ]}]
                    }
                ),
                IFlytekOCRAdapter(client=lambda _data, _format: IFlytekOCRAdapterTests.success_payload()),
                EasyOCRAdapter(client=EasyClient()),
            )
            results = tuple(adapter.recognize(segment(image)) for adapter in adapters)

        self.assertEqual(len({type(adapter) for adapter in adapters}), 4)
        self.assertEqual(
            [result.provider for result in results],
            ["paddle", "tencent", "iflytek", "easyocr"],
        )
        for result in results:
            self.assertEqual((result.start_ms, result.end_ms), (1200, 4650))
            self.assertEqual(result.status, "completed")
            self.assertTrue(result.text)
            self.assertGreater(result.confidence, 0)
            self.assertIsInstance(result.boxes, tuple)
            self.assertTrue(result.model_version)

    def test_paddle_explicitly_uses_pp_ocrv5_and_reports_missing_dependency(self):
        configured = PaddleOCRAdapter(engine=FakePaddleEngine())
        self.assertEqual(configured.model_version, "PP-OCRv5")

        unavailable = PaddleOCRAdapter(engine=None).recognize(
            SubtitleSegment("seg", "run", 0, 1000, "/missing.jpg", "sha", "crop-v1")
        )
        self.assertEqual(unavailable.status, "dependency_unavailable")
        self.assertEqual(unavailable.error_code, "PADDLEOCR_UNAVAILABLE")

    def test_paddle_v37_result_shape_is_normalized_and_factory_locks_mobile_v5(self):
        captured = {}

        class Pipeline:
            def predict(self, input):
                captured["input"] = input
                return [FakePaddleResult()]

        def factory(**kwargs):
            captured.update(kwargs)
            return Pipeline()

        engine = PaddleOCRV5Engine(factory=factory)
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "crop.png"
            image.write_bytes(b"image")
            recognized = PaddleOCRAdapter(engine=engine).recognize(segment(image))

        self.assertEqual(captured["text_detection_model_name"], "PP-OCRv5_mobile_det")
        self.assertEqual(captured["text_recognition_model_name"], "PP-OCRv5_mobile_rec")
        self.assertFalse(captured["use_doc_orientation_classify"])
        self.assertEqual(recognized.text, "孩子最好在10岁以前VISA 590")
        self.assertAlmostEqual(recognized.confidence, 0.935)
        self.assertEqual(len(recognized.boxes), 2)
        self.assertEqual(recognized.bounding_boxes[0], {
            "x": 1, "y": 2, "width": 20, "height": 5,
        })
        self.assertEqual(len(recognized.polygons[0]), 4)
        self.assertEqual(recognized.request_id_source, "local")
        self.assertEqual(recognized.billing_type, "local")
        self.assertEqual(recognized.cost, 0)

    def test_paddle_local_request_id_provenance_image_validation_and_no_network(self):
        calls = []

        class Engine:
            def predict(self, path):
                calls.append(path)
                return FakePaddleEngine().predict(path)

        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "subtitle.jpg"
            image.write_bytes(b"local-image")
            with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
                result = PaddleOCRAdapter(engine=Engine()).recognize(segment(image))
            invalid = Path(folder) / "subtitle.gif"
            invalid.write_bytes(b"invalid")
            invalid_result = PaddleOCRAdapter(engine=Engine()).recognize(segment(invalid))

        self.assertEqual(result.provider, "paddle")
        self.assertTrue(result.request_id.startswith("local:paddle:"))
        self.assertEqual(result.to_unified_dict()["request_id_source"], "local")
        self.assertEqual(result.to_unified_dict()["billing_type"], "local")
        self.assertEqual(result.to_unified_dict()["cost"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(invalid_result.error_code, "PADDLEOCR_INVALID_IMAGE")

    def test_paddle_smoke_is_one_local_inference_with_zero_network(self):
        from content_extraction.paddle_smoke import run_smoke

        class Engine:
            def predict(self, path):
                return FakePaddleEngine().predict(path)

        class Factory:
            def __call__(self):
                return Engine()

        with tempfile.TemporaryDirectory() as folder:
            from PIL import Image

            image = Path(folder) / "subtitle.jpg"
            Image.new("RGB", (1280, 240), "white").save(image)
            output = run_smoke(image, engine_factory=Factory())

        self.assertEqual(output["provider"], "paddle")
        self.assertEqual(output["status"], "completed")
        self.assertEqual(output["local_inference_count"], 1)
        self.assertEqual(output["network_request_count"], 0)
        self.assertEqual(output["billing_type"], "local")
        self.assertEqual(output["cost"], 0)
        self.assertEqual(output["request_id_source"], "local")
        self.assertEqual(output["image"], {"width": 1280, "height": 240})

    def test_paddle_does_not_join_text_without_valid_four_point_geometry(self):
        class Engine:
            def predict(self, _path):
                return [
                    {"text": "点赞 1234", "score": 0.99, "box": []},
                    {"text": "字幕正文", "score": 98, "box": [[1, 2], [21, 2], [21, 8], [1, 8]]},
                ]

        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "subtitle.png"
            image.write_bytes(b"image")
            result = PaddleOCRAdapter(engine=Engine()).recognize(segment(image))

        self.assertEqual(result.text, "字幕正文")
        self.assertEqual(result.confidence, 0.98)
        self.assertEqual(len(result.polygons), 1)

    def test_cloud_credentials_are_not_required_by_contract_or_exposed_in_errors(self):
        secret_id = "SECRET_ID_DO_NOT_PRINT"
        secret_key = "SECRET_KEY_DO_NOT_PRINT"

        def failing_client(_data):
            raise RuntimeError(f"request failed SecretId={secret_id} SecretKey={secret_key}")

        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "crop.jpg"
            image.write_bytes(b"image")
            for adapter in (
                TencentGeneralAccurateOCRAdapter(client=failing_client, sensitive_values=(secret_id, secret_key)),
                IFlytekOCRAdapter(
                    client=lambda data, _format: failing_client(data),
                    sensitive_values=(secret_id, secret_key),
                ),
            ):
                result = adapter.recognize(segment(image))
                self.assertEqual(result.status, "failed")
                self.assertNotIn(secret_id, result.error_message)
                self.assertNotIn(secret_key, result.error_message)
                self.assertIn("[REDACTED]", result.error_message)

        no_client = TencentGeneralAccurateOCRAdapter(client=None).recognize(
            SubtitleSegment("seg", "run", 0, 1000, "/missing.jpg", "sha", "crop-v1")
        )
        self.assertEqual(no_client.status, "dependency_unavailable")
        self.assertEqual(no_client.error_code, "TENCENT_OCR_NOT_CONFIGURED")


class TencentOCRAdapterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.image = Path(self.folder.name) / "subtitle.png"
        self.image.write_bytes(b"subtitle-image")
        self.segment = SubtitleSegment(
            "frame-17", "video-9", 1200, 4650, str(self.image),
            "frame-sha", "opencv-subtitle-v1",
        )

    @staticmethod
    def success_payload():
        return {
            "Response": {
                "RequestId": "request-123",
                "Angle": 0.0,
                "TextDetections": [
                    {
                        "DetectedText": "孩子最好在10岁以前出国",
                        "Confidence": 96,
                        "ItemPolygon": {"X": 120, "Y": 800, "Width": 900, "Height": 80},
                        "Polygon": [
                            {"X": 120, "Y": 800}, {"X": 1020, "Y": 800},
                            {"X": 1020, "Y": 880}, {"X": 120, "Y": 880},
                        ],
                        "Words": [],
                    }
                ],
            }
        }

    def test_config_requires_both_environment_secrets_without_defaults(self):
        self.assertIsNone(TencentOCRConfig.from_environ({}))
        self.assertIsNone(TencentOCRConfig.from_environ({"TENCENT_OCR_SECRET_ID": "only-id"}))

        config = TencentOCRConfig.from_environ({
            "TENCENT_OCR_SECRET_ID": "safe-id",
            "TENCENT_OCR_SECRET_KEY": "safe-key",
        })

        self.assertEqual(config.region, "")
        self.assertEqual(config.endpoint, "ocr.tencentcloudapi.com")
        self.assertEqual(config.timeout_seconds, 15)

    def test_sdk_client_builds_general_accurate_request_lazily_with_timeout(self):
        captured = {}

        class Request:
            def from_json_string(self, value):
                captured["request"] = json.loads(value)

        class Response:
            def to_json_string(self):
                return json.dumps(TencentOCRAdapterTests.success_payload()["Response"])

        class Client:
            def GeneralAccurateOCR(self, request):
                captured["request_type"] = type(request).__name__
                return Response()

        def factory(config):
            captured["config"] = config
            return Client(), Request

        config = TencentOCRConfig("safe-id", "safe-key", timeout_seconds=7)
        client = TencentSDKGeneralAccurateClient(config, client_factory=factory)

        payload = client(b"image-bytes")

        self.assertEqual(captured["config"].timeout_seconds, 7)
        self.assertEqual(captured["request_type"], "Request")
        self.assertEqual(captured["request"], {
            "ImageBase64": "aW1hZ2UtYnl0ZXM=",
            "IsPdf": False,
        })
        self.assertEqual(payload["Response"]["RequestId"], "request-123")

    def test_standardizes_request_geometry_provenance_and_safe_raw_response(self):
        secret_id = "secret-id-value"
        secret_key = "secret-key-value"
        payload = self.success_payload()
        payload["Response"]["Debug"] = f"SecretKey={secret_key}"
        adapter = TencentOCRAdapter(
            client=lambda _data: payload,
            sensitive_values=(secret_id, secret_key),
        )

        result = adapter.recognize(self.segment)
        unified = result.to_unified_dict()

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.request_id, "request-123")
        self.assertEqual(result.timestamp_ms, 1200)
        self.assertEqual(result.frame_id, "frame-17")
        self.assertEqual(result.source_video_id, "video-9")
        self.assertEqual(unified["bounding_box"], (
            {"x": 120, "y": 800, "width": 900, "height": 80},
        ))
        self.assertEqual(len(unified["polygon"][0]), 4)
        self.assertIsNone(unified["error"])
        serialized = json.dumps(unified, ensure_ascii=False)
        self.assertNotIn(secret_id, serialized)
        self.assertNotIn(secret_key, serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_invalid_geometry_ui_text_is_not_joined_into_subtitle_text(self):
        payload = self.success_payload()
        payload["Response"]["TextDetections"].insert(0, {
            "DetectedText": "点赞 1234",
            "Confidence": 99,
            "ItemPolygon": {"X": 0, "Y": 0, "Width": 0, "Height": 0},
            "Polygon": [],
        })
        result = TencentOCRAdapter(client=lambda _data: payload).recognize(self.segment)
        self.assertEqual(result.text, "孩子最好在10岁以前出国")
        self.assertEqual(len(result.boxes), 1)
        self.assertEqual(len(result.raw_response["Response"]["TextDetections"]), 2)

    def test_timeout_retries_once_then_succeeds(self):
        calls = []
        sleeps = []

        def client(_data):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("socket timed out")
            return self.success_payload()

        result = TencentOCRAdapter(
            client=client, max_attempts=2, sleep=lambda seconds: sleeps.append(seconds)
        ).recognize(self.segment)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [0.2])

    def test_auth_failure_is_classified_not_retried_and_log_is_redacted(self):
        secret_id = "auth-secret-id"
        secret_key = "auth-secret-key"
        calls = []

        class AuthFailure(Exception):
            def get_code(self):
                return "AuthFailure.SecretIdNotFound"

        def client(_data):
            calls.append(1)
            raise AuthFailure(f"SecretId={secret_id} SecretKey={secret_key}")

        adapter = TencentOCRAdapter(
            client=client,
            sensitive_values=(secret_id, secret_key),
            max_attempts=2,
            sleep=lambda _seconds: None,
        )
        with self.assertLogs("content_extraction.ocr.tencent", level="WARNING") as logs:
            result = adapter.recognize(self.segment)
        self.assertEqual(result.error_code, "TENCENT_OCR_AUTH_FAILED")
        self.assertEqual(len(calls), 1)
        output = "\n".join(logs.output) + result.error_message
        self.assertNotIn(secret_id, output)
        self.assertNotIn(secret_key, output)
        self.assertIn("[REDACTED]", output)

    def test_api_error_is_classified_and_provider_failure_stays_structured(self):
        class ApiFailure(Exception):
            def get_code(self):
                return "FailedOperation.ServiceIsolated"

            def get_request_id(self):
                return "failed-request-7"

        result = TencentOCRAdapter(
            client=lambda _data: (_ for _ in ()).throw(ApiFailure("service unavailable")),
            max_attempts=1,
        ).recognize(self.segment)
        self.assertEqual(result.provider, "tencent")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "TENCENT_OCR_API_ERROR")
        self.assertEqual(result.request_id, "failed-request-7")
        self.assertEqual(result.to_unified_dict()["error"]["code"], "TENCENT_OCR_API_ERROR")

    def test_smoke_helper_returns_safe_summary_without_raw_or_credentials(self):
        from content_extraction.tencent_smoke import run_smoke

        secret_id = "smoke-id-value"
        secret_key = "smoke-key-value"
        output = run_smoke(
            self.image,
            {
                "TENCENT_OCR_SECRET_ID": secret_id,
                "TENCENT_OCR_SECRET_KEY": secret_key,
            },
            client_factory=lambda _config: lambda _data: self.success_payload(),
        )
        self.assertEqual(output["status"], "completed")
        self.assertEqual(output["request_id"], "request-123")
        self.assertEqual(output["bounding_box_count"], 1)
        self.assertEqual(output["polygon_count"], 1)
        self.assertEqual(output["request_count"], 1)
        self.assertEqual(output["text_detections_count"], 1)
        self.assertTrue(output["raw_response_retained"])
        self.assertFalse(output["raw_response_contains_credentials"])
        self.assertEqual(output["item_polygon"][0]["width"], 900)
        self.assertEqual(len(output["polygon"][0]), 4)
        self.assertNotIn("raw_response", output)
        serialized = json.dumps(output, ensure_ascii=False)
        self.assertNotIn(secret_id, serialized)
        self.assertNotIn(secret_key, serialized)


class IFlytekOCRAdapterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.image = Path(self.folder.name) / "subtitle.png"
        self.image.write_bytes(b"subtitle-image")
        self.segment = SubtitleSegment(
            "frame-18", "video-9", 1200, 4650, str(self.image),
            "frame-sha", "opencv-subtitle-v1",
        )

    @staticmethod
    def decoded_payload():
        coord = [
            {"x": 39, "y": 65}, {"x": 1278, "y": 62},
            {"x": 1278, "y": 135}, {"x": 40, "y": 139},
        ]
        return {
            "pages": [{
                "exception": 0, "width": 1280, "height": 180, "angle": 0,
                "lines": [{
                    "exception": 0, "angle": 0, "conf": 0.9715,
                    "coord": coord,
                    "words": [{
                        "content": "孩子最好在10岁以前进入英语环境VISA",
                        "conf": 0.9715, "coord": coord,
                    }],
                }],
            }],
            "category": "ch_en_public_cloud",
            "version": "v1",
        }

    @classmethod
    def success_payload(cls):
        encoded = base64.b64encode(
            json.dumps(cls.decoded_payload(), ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        return {
            "header": {"code": 0, "message": "success", "sid": "sid-123"},
            "payload": {"result": {
                "compress": "raw", "encoding": "utf8", "format": "json",
                "text": encoded,
            }},
        }

    def test_config_requires_all_three_credentials_and_uses_official_endpoint(self):
        self.assertIsNone(IFlytekOCRConfig.from_environ({}))
        self.assertIsNone(IFlytekOCRConfig.from_environ({
            "IFLYTEK_OCR_APP_ID": "app", "IFLYTEK_OCR_API_KEY": "key",
        }))
        config = IFlytekOCRConfig.from_environ({
            "IFLYTEK_OCR_APP_ID": "app",
            "IFLYTEK_OCR_API_KEY": "key",
            "IFLYTEK_OCR_API_SECRET": "secret",
        })
        self.assertEqual(config.endpoint, "https://api.xf-yun.com/v1/private/sf8e6aca1")
        self.assertEqual(config.timeout_seconds, 15)

    def test_signing_uses_host_date_request_line_and_never_places_secret_in_url(self):
        config = IFlytekOCRConfig("app", "test-api-key", "test-api-secret")
        now = datetime.datetime(2021, 8, 11, 6, 55, 18, tzinfo=datetime.timezone.utc)

        signed_url, request_date = build_iflytek_auth_url(config, now)
        query = parse_qs(urlsplit(signed_url).query)
        authorization = base64.b64decode(query["authorization"][0]).decode("utf-8")

        self.assertEqual(request_date, "Wed, 11 Aug 2021 06:55:18 GMT")
        self.assertEqual(query["host"], ["api.xf-yun.com"])
        self.assertEqual(query["date"], [request_date])
        self.assertIn('api_key="test-api-key"', authorization)
        self.assertIn('algorithm="hmac-sha256"', authorization)
        self.assertIn('headers="host date request-line"', authorization)
        self.assertIn(
            'signature="6Z5mi0fX4ZhSbiflD4q7iV3Bv9xZEzZjBXnmC9Al/0E="',
            authorization,
        )
        self.assertNotIn("test-api-secret", signed_url)
        self.assertNotIn("test-api-secret", authorization)

    def test_request_body_matches_official_contract_and_checks_format_and_size(self):
        config = IFlytekOCRConfig("app-id", "key", "secret")
        body = build_iflytek_request_body(config, b"image", "png")

        self.assertEqual(body["header"], {"app_id": "app-id", "status": 3})
        self.assertEqual(body["parameter"]["sf8e6aca1"], {
            "category": "ch_en_public_cloud",
            "result": {"encoding": "utf8", "compress": "raw", "format": "json"},
        })
        self.assertEqual(body["payload"]["sf8e6aca1_data_1"], {
            "encoding": "png", "status": 3,
            "image": base64.b64encode(b"image").decode("ascii"),
        })
        with self.assertRaises(ValueError):
            build_iflytek_request_body(config, b"image", "gif")
        with self.assertRaises(ValueError):
            build_iflytek_request_body(
                IFlytekOCRConfig("app", "key", "secret", max_base64_bytes=3),
                b"image", "png",
            )

    def test_adapter_rejects_invalid_image_before_provider_call(self):
        invalid = Path(self.folder.name) / "subtitle.gif"
        invalid.write_bytes(b"image")
        calls = []
        result = IFlytekOCRAdapter(
            client=lambda _data, _format: calls.append(1)
        ).recognize(segment(invalid))
        self.assertEqual(result.error_code, "IFLYTEK_OCR_INVALID_IMAGE")
        self.assertEqual(calls, [])

    def test_standardizes_decoded_text_confidence_geometry_page_metadata_and_raw(self):
        payload = self.success_payload()
        payload["debug"] = "APISecret=secret-value"
        result = IFlytekOCRAdapter(
            client=lambda _data, _format: payload,
            sensitive_values=("secret-value",),
        ).recognize(self.segment)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.request_id, "sid-123")
        self.assertEqual(result.text, "孩子最好在10岁以前进入英语环境VISA")
        self.assertEqual(result.confidence, 0.9715)
        self.assertEqual(result.bounding_boxes[0], {"x": 39, "y": 62, "width": 1239, "height": 77})
        self.assertEqual(len(result.polygons[0]), 4)
        self.assertEqual(result.boxes[0]["page_width"], 1280)
        self.assertEqual(result.boxes[0]["page_height"], 180)
        self.assertEqual(result.boxes[0]["page_angle"], 0)
        self.assertEqual(result.boxes[0]["line_angle"], 0)
        self.assertIn("decoded_result", result.raw_response)
        serialized = json.dumps(result.to_unified_dict(), ensure_ascii=False)
        self.assertNotIn("secret-value", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_invalid_geometry_ui_text_is_not_joined(self):
        decoded = self.decoded_payload()
        decoded["pages"][0]["lines"].insert(0, {
            "exception": 0, "angle": 0, "conf": 0.99, "coord": [],
            "words": [{"content": "点赞 1234", "conf": 0.99, "coord": []}],
        })
        payload = self.success_payload()
        payload["payload"]["result"]["text"] = base64.b64encode(
            json.dumps(decoded, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")

        result = IFlytekOCRAdapter(client=lambda _data, _format: payload).recognize(self.segment)
        self.assertEqual(result.text, "孩子最好在10岁以前进入英语环境VISA")
        self.assertEqual(len(result.boxes), 1)

    def test_timeout_retries_only_when_allowed_and_smoke_mode_is_single_attempt(self):
        calls = []

        def client(_data, _format):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("timed out")
            return self.success_payload()

        result = IFlytekOCRAdapter(client=client, max_attempts=2, sleep=lambda _s: None).recognize(self.segment)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 2)

        calls.clear()
        failed = IFlytekOCRAdapter(client=client, max_attempts=1, sleep=lambda _s: None).recognize(self.segment)
        self.assertEqual(failed.error_code, "IFLYTEK_OCR_TIMEOUT")
        self.assertEqual(len(calls), 1)

    def test_auth_date_skew_api_rate_limit_and_invalid_response_are_classified(self):
        cases = (
            (IFlytekHTTPError(401, "unauthorized"), "IFLYTEK_OCR_AUTHENTICATION_ERROR"),
            (IFlytekHTTPError(403, "date header is invalid"), "IFLYTEK_OCR_AUTHENTICATION_ERROR"),
            (IFlytekHTTPError(429, "too many requests"), "IFLYTEK_OCR_RATE_LIMIT"),
            (IFlytekHTTPError(500, "service unavailable"), "IFLYTEK_OCR_PROVIDER_UNAVAILABLE"),
            (ValueError("invalid response"), "IFLYTEK_OCR_INVALID_RESPONSE"),
        )
        for error, code in cases:
            with self.subTest(code=code):
                result = IFlytekOCRAdapter(
                    client=lambda _data, _format, error=error: (_ for _ in ()).throw(error),
                    max_attempts=1,
                ).recognize(self.segment)
                self.assertEqual(result.error_code, code)

    def test_api_header_error_and_log_are_structured_and_redacted(self):
        secret = "iflytek-api-secret"
        payload = {
            "header": {"code": 11200, "message": f"APISecret={secret}", "sid": "sid-failed"}
        }
        adapter = IFlytekOCRAdapter(
            client=lambda _data, _format: payload,
            sensitive_values=(secret,), max_attempts=1,
        )
        with self.assertLogs("content_extraction.ocr.iflytek", level="WARNING") as logs:
            result = adapter.recognize(self.segment)
        output = "\n".join(logs.output) + json.dumps(result.to_unified_dict(), ensure_ascii=False)
        self.assertEqual(result.error_code, "IFLYTEK_OCR_API_ERROR")
        self.assertEqual(result.request_id, "sid-failed")
        self.assertNotIn(secret, output)
        self.assertIn("[REDACTED]", output)

    def test_http_client_posts_json_with_timeout_and_safe_injectable_opener(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(IFlytekOCRAdapterTests.success_payload()).encode("utf-8")

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return Response()

        config = IFlytekOCRConfig("app", "key", "secret", timeout_seconds=7)
        client = IFlytekUniversalOCRClient(config, opener=opener)
        payload = client(b"image", "jpg")

        self.assertEqual(payload["header"]["code"], 0)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["timeout"], 7)
        self.assertEqual(captured["body"]["header"]["app_id"], "app")
        self.assertNotIn("secret", json.dumps(captured["body"]))

    def test_smoke_helper_is_single_request_and_returns_safe_summary(self):
        from content_extraction.iflytek_smoke import run_smoke

        values = {
            "IFLYTEK_OCR_APP_ID": "smoke-app",
            "IFLYTEK_OCR_API_KEY": "smoke-key",
            "IFLYTEK_OCR_API_SECRET": "smoke-secret",
        }
        output = run_smoke(
            self.image,
            values,
            client_factory=lambda _config: (
                lambda _data, _format: self.success_payload()
            ),
        )
        self.assertEqual(output["status"], "completed")
        self.assertEqual(output["request_id"], "si...23")
        self.assertEqual(output["header_code"], 0)
        self.assertEqual(output["request_count"], 1)
        self.assertEqual(output["line_count"], 1)
        self.assertEqual(output["lines"][0]["confidence"], 0.9715)
        self.assertEqual(len(output["lines"][0]["coord"]), 4)
        self.assertEqual(output["pages"][0], {
            "width": 1280, "height": 180, "angle": 0,
        })
        self.assertEqual(output["bounding_box_count"], 1)
        self.assertEqual(output["polygon_count"], 1)
        self.assertTrue(output["raw_response_retained"])
        self.assertFalse(output["raw_response_contains_credentials"])
        self.assertNotIn("raw_response", output)
        serialized = json.dumps(output, ensure_ascii=False)
        for value in values.values():
            self.assertNotIn(value, serialized)


if __name__ == "__main__":
    unittest.main()
