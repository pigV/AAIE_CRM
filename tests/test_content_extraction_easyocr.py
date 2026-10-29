import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from content_extraction.billing import FreeOnlyBillingGate
from content_extraction.models import SubtitleSegment
from content_extraction.ocr.parallel import ParallelOCRRunner
from content_extraction.ocr.easyocr import (
    EasyOCRAdapter,
    EasyOCRWorkerClient,
    EasyOCRWorkerConfig,
    model_fingerprint,
)
from content_extraction.repository import ContentExtractionRepository


def make_segment(path, image_hash="same-image"):
    return SubtitleSegment(
        "frame-1", "video-1", 1200, 4650, str(path), image_hash,
        "opencv-subtitle-v1",
    )


class FakeEasyOCRClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {
            "detections": [
                {
                    "polygon": [[220, 820], [980, 820], [980, 875], [220, 875]],
                    "text": "孩子最好在10岁以前进入英语环境",
                    "confidence": 0.98,
                },
                {
                    "polygon": [[990, 820], [1100, 820], [1100, 875], [990, 875]],
                    "text": "VISA",
                    "confidence": 98,
                },
            ],
            "image": {"width": 1280, "height": 1280},
            "inference_ms": 123.4,
            "network_request_count": 0,
        }
        self.error = error
        self.calls = 0

    def recognize(self, path):
        self.calls += 1
        if self.error:
            raise self.error
        return self.payload


class EasyOCRAdapterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.image = Path(self.folder.name) / "subtitle.jpg"
        self.image.write_bytes(b"image")

    def test_chinese_english_parsing_confidence_and_coordinates(self):
        result = EasyOCRAdapter(client=FakeEasyOCRClient()).recognize(
            make_segment(self.image)
        )

        self.assertEqual(result.provider, "easyocr")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.text, "孩子最好在10岁以前进入英语环境VISA")
        self.assertAlmostEqual(result.confidence, 0.98)
        self.assertEqual(result.polygons[0][0], {"x": 220, "y": 820})
        self.assertEqual(result.bounding_boxes[1], {
            "x": 990, "y": 820, "width": 110, "height": 55,
        })
        self.assertTrue(result.request_id.startswith("local:easyocr:"))
        self.assertEqual(result.request_id_source, "local")
        self.assertEqual(result.billing_type, "local")
        self.assertEqual(result.cost, 0)
        self.assertEqual(result.raw_response["network_request_count"], 0)

    def test_multiple_boxes_are_ordered_by_space(self):
        payload = {
            "detections": [
                {"polygon": [[1, 50], [11, 50], [11, 60], [1, 60]], "text": "三", "confidence": .9},
                {"polygon": [[30, 10], [40, 10], [40, 20], [30, 20]], "text": "二", "confidence": .8},
                {"polygon": [[1, 10], [11, 10], [11, 20], [1, 20]], "text": "一", "confidence": .7},
            ],
            "network_request_count": 0,
        }
        result = EasyOCRAdapter(client=FakeEasyOCRClient(payload)).recognize(
            make_segment(self.image)
        )
        self.assertEqual(result.text, "一二三")
        self.assertAlmostEqual(result.confidence, .8)

    def test_invalid_geometry_is_not_joined_into_text(self):
        payload = {
            "detections": [
                {"polygon": [], "text": "点赞 123", "confidence": .99},
                {"polygon": [[1, 2], [21, 2], [21, 8], [1, 8]], "text": "字幕", "confidence": .9},
            ],
            "network_request_count": 0,
        }
        result = EasyOCRAdapter(client=FakeEasyOCRClient(payload)).recognize(
            make_segment(self.image)
        )
        self.assertEqual(result.text, "字幕")
        self.assertEqual(len(result.polygons), 1)

    def test_invalid_image_missing_model_and_worker_failure_are_isolated(self):
        missing = Path(self.folder.name) / "missing.jpg"
        invalid = EasyOCRAdapter(client=FakeEasyOCRClient()).recognize(make_segment(missing))
        unavailable = EasyOCRAdapter(client=None).recognize(make_segment(self.image))
        failed = EasyOCRAdapter(
            client=FakeEasyOCRClient(error=TimeoutError("worker timeout"))
        ).recognize(make_segment(self.image))

        self.assertEqual(invalid.error_code, "EASYOCR_INVALID_IMAGE")
        self.assertEqual(unavailable.status, "dependency_unavailable")
        self.assertEqual(unavailable.error_code, "EASYOCR_UNAVAILABLE")
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error_code, "EASYOCR_WORKER_TIMEOUT")

    def test_local_provider_obeys_free_only_without_credentials_or_network(self):
        env = {
            "TENCENT_OCR_SECRET_ID": "must-not-leak",
            "IFLYTEK_OCR_API_SECRET": "must-not-leak-either",
        }
        gate = FreeOnlyBillingGate.from_environ(env)
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            result = EasyOCRAdapter(client=FakeEasyOCRClient()).recognize(
                make_segment(self.image)
            )
        serialized = json.dumps(result.to_unified_dict(), ensure_ascii=False)
        self.assertTrue(gate.allows("easyocr", "local"))
        self.assertNotIn("must-not-leak", serialized)
        self.assertEqual(result.cost, 0)

    def test_model_fingerprint_changes_with_model_contents(self):
        model_dir = Path(self.folder.name) / "models"
        model_dir.mkdir()
        model = model_dir / "zh_sim.pth"
        model.write_bytes(b"one")
        first = model_fingerprint(model_dir)
        model.write_bytes(b"two")
        second = model_fingerprint(model_dir)
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 16)

    def test_worker_protocol_converts_array_and_scalar_values_to_json_types(self):
        from content_extraction.easyocr_worker import json_safe

        class Scalar:
            def __init__(self, value):
                self.value = value

            def item(self):
                return self.value

        class Array:
            def tolist(self):
                return [[Scalar(1), Scalar(2)], [Scalar(3), Scalar(4)]]

        converted = json_safe(Array())
        self.assertEqual(converted, [[1, 2], [3, 4]])
        self.assertEqual(json.loads(json.dumps(converted)), [[1, 2], [3, 4]])


class EasyOCRPersistentWorkerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.model_dir = root / "models"
        self.model_dir.mkdir()
        (self.model_dir / "fake.pth").write_bytes(b"model")
        self.script = root / "fake_worker.py"
        self.script.write_text(textwrap.dedent("""
            import json, os, sys
            print(json.dumps({"event":"ready", "init_ms":12.5, "network_request_count":0}), flush=True)
            count = 0
            for line in sys.stdin:
                request = json.loads(line)
                if request.get("command") == "shutdown":
                    print(json.dumps({"event":"shutdown", "worker_exit":"clean"}), flush=True)
                    break
                count += 1
                print(json.dumps({
                    "event":"result", "request_id":request["request_id"],
                    "detections":[], "reader_identity":"one-reader",
                    "worker_inference_count":count, "network_request_count":0,
                    "seen_secret": bool(os.environ.get("TENCENT_OCR_SECRET_ID") or os.environ.get("IFLYTEK_OCR_API_SECRET")),
                }), flush=True)
        """), encoding="utf-8")
        self.config = EasyOCRWorkerConfig(
            python_path=sys.executable,
            worker_path=str(self.script),
            model_dir=str(self.model_dir),
            timeout_seconds=2,
        )

    def test_worker_starts_once_reuses_reader_and_has_minimal_environment(self):
        old_tencent = os.environ.get("TENCENT_OCR_SECRET_ID")
        old_iflytek = os.environ.get("IFLYTEK_OCR_API_SECRET")
        os.environ["TENCENT_OCR_SECRET_ID"] = "never-child"
        os.environ["IFLYTEK_OCR_API_SECRET"] = "never-child"
        client = EasyOCRWorkerClient(self.config)
        try:
            first = client.recognize("one.jpg")
            second = client.recognize("two.jpg")
            self.assertEqual(first["reader_identity"], second["reader_identity"])
            self.assertEqual(second["worker_inference_count"], 2)
            self.assertFalse(first["seen_secret"])
            self.assertEqual(client.start_count, 1)
            self.assertEqual(client.inference_count, 2)
        finally:
            client.close()
            if old_tencent is None:
                os.environ.pop("TENCENT_OCR_SECRET_ID", None)
            else:
                os.environ["TENCENT_OCR_SECRET_ID"] = old_tencent
            if old_iflytek is None:
                os.environ.pop("IFLYTEK_OCR_API_SECRET", None)
            else:
                os.environ["IFLYTEK_OCR_API_SECRET"] = old_iflytek
        self.assertEqual(client.worker_exit, "clean")

    def test_timeout_and_crash_are_reported_without_restart(self):
        slow = Path(self.folder.name) / "slow.py"
        slow.write_text(
            "import time; time.sleep(5)\n", encoding="utf-8"
        )
        config = EasyOCRWorkerConfig(
            python_path=sys.executable, worker_path=str(slow),
            model_dir=str(self.model_dir), timeout_seconds=.05,
        )
        client = EasyOCRWorkerClient(config)
        with self.assertRaises(TimeoutError):
            client.recognize("one.jpg")
        self.assertEqual(client.start_count, 1)
        client.close()

        crash = Path(self.folder.name) / "crash.py"
        crash.write_text("raise RuntimeError(\"boom\")\n", encoding="utf-8")
        config = EasyOCRWorkerConfig(
            python_path=sys.executable, worker_path=str(crash),
            model_dir=str(self.model_dir), timeout_seconds=1,
        )
        client = EasyOCRWorkerClient(config)
        with self.assertRaises(RuntimeError):
            client.recognize("one.jpg")
        self.assertEqual(client.start_count, 1)
        client.close()


class EasyOCRSmokeContractTests(unittest.TestCase):
    def test_smoke_runs_one_local_inference_and_cleanly_closes_worker(self):
        from content_extraction.easyocr_smoke import run_smoke

        class SmokeClient(FakeEasyOCRClient):
            def __init__(self, config):
                super().__init__()
                self.config = config
                self.init_ms = 10.0
                self.inference_count = 0
                self.start_count = 1
                self.worker_exit = "running"

            def recognize(self, path):
                self.inference_count += 1
                payload = super().recognize(path)
                payload["inference_ms"] = 20.0
                return payload

            def close(self):
                self.worker_exit = "clean"

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "subtitle.jpg"
            image.write_bytes(b"image")
            model_dir = root / "models"
            model_dir.mkdir()
            (model_dir / "model.pth").write_bytes(b"model")
            config = EasyOCRWorkerConfig(
                python_path=sys.executable, worker_path=__file__,
                model_dir=str(model_dir),
            )
            output = run_smoke(image, config, client_factory=SmokeClient)

        self.assertEqual(output["local_inference_count"], 1)
        self.assertEqual(output["worker_start_count"], 1)
        self.assertEqual(output["worker_exit"], "clean")
        self.assertEqual(output["network_request_count"], 0)
        self.assertEqual(output["cost"], 0)


class EasyOCRRepositoryIsolationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        ids = iter(("run-1", "seg-1", "seg-2"))
        self.repo = ContentExtractionRepository(
            Path(self.folder.name) / "cache.db", id_factory=lambda: next(ids)
        )
        self.repo.initialize()
        self.repo.create_run(1, "share")
        self.image = Path(self.folder.name) / "subtitle.jpg"
        self.image.write_bytes(b"image")

    def test_cache_and_duplicate_protection_reuse_easyocr_result(self):
        client = FakeEasyOCRClient()
        adapter = EasyOCRAdapter(client=client)
        first = self.repo.add_subtitle_segment(
            "run-1", 0, 1000, str(self.image), "same-hash", "crop-v1"
        )
        second = self.repo.add_subtitle_segment(
            "run-1", 2000, 3000, str(self.image), "same-hash", "crop-v1"
        )
        ParallelOCRRunner(self.repo, (adapter,)).recognize(first)
        cached = ParallelOCRRunner(self.repo, (adapter,)).recognize(second)
        self.assertEqual(client.calls, 1)
        self.assertEqual((cached.results[0].start_ms, cached.results[0].end_ms), (2000, 3000))
        self.assertEqual(cached.results[0].billing_type, "local")

    def test_easyocr_failure_does_not_stop_another_provider(self):
        class SuccessfulAdapter(EasyOCRAdapter):
            provider = "paddle"
            model_version = "fake-paddle"

        failed = EasyOCRAdapter(client=FakeEasyOCRClient(error=RuntimeError("worker crash")))
        successful = SuccessfulAdapter(client=FakeEasyOCRClient())
        segment = self.repo.add_subtitle_segment(
            "run-1", 0, 1000, str(self.image), "isolation-hash", "crop-v1"
        )
        outcome = ParallelOCRRunner(self.repo, (failed, successful)).recognize(segment)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(len(outcome.successful), 1)
        self.assertEqual(outcome.successful[0].provider, "paddle")

if __name__ == "__main__":
    unittest.main()
