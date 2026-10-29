import json
import os
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path

from content_extraction.models import SubtitleSegment
from content_extraction.ocr.apple_vision import (
    AppleVisionOCRAdapter,
    AppleVisionWorkerClient,
    AppleVisionWorkerConfig,
)


def segment(path):
    return SubtitleSegment(
        "frame-1", "video-1", 1000, 2000, str(path), "abc123",
        "opencv-subtitle-v2",
    )


class FakeVisionClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {
            "detections": [{
                "text": "孩子考砸了",
                "confidence": 0.97,
                "polygon": [[10, 20], [210, 20], [210, 70], [10, 70]],
            }],
            "image": {"width": 220, "height": 80},
            "inference_ms": 12.5,
            "network_request_count": 0,
        }
        self.error = error

    def recognize(self, _path):
        if self.error:
            raise self.error
        return self.payload


class AppleVisionOCRAdapterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.image = Path(self.folder.name) / "crop.jpg"
        self.image.write_bytes(b"image")

    def test_normalizes_text_confidence_geometry_and_local_metadata(self):
        payload = {
            "detections": [
                {"text": "GPA", "confidence": .9,
                 "polygon": [[40, 60], [100, 60], [100, 80], [40, 80]]},
                {"text": "高二", "confidence": .98,
                 "polygon": [[10, 10], [90, 10], [90, 40], [10, 40]]},
            ],
            "image": {"width": 120, "height": 100},
            "inference_ms": 8.0,
            "network_request_count": 0,
        }
        result = AppleVisionOCRAdapter(FakeVisionClient(payload)).recognize(segment(self.image))

        self.assertEqual(result.provider, "apple_vision")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.text, "高二GPA")
        self.assertAlmostEqual(result.confidence, .94)
        self.assertEqual(result.bounding_boxes[0], {"x": 10, "y": 10, "width": 80, "height": 30})
        self.assertEqual(result.polygons[1][2], {"x": 100, "y": 80})
        self.assertTrue(result.request_id.startswith("local:apple_vision:"))
        self.assertEqual(result.request_id_source, "local")
        self.assertEqual(result.billing_type, "local")
        self.assertEqual(result.cost, 0)
        self.assertEqual(result.raw_response["network_request_count"], 0)

    def test_invalid_image_and_worker_failure_are_isolated(self):
        missing = Path(self.folder.name) / "missing.jpg"
        invalid = AppleVisionOCRAdapter(FakeVisionClient()).recognize(segment(missing))
        failed = AppleVisionOCRAdapter(
            FakeVisionClient(error=RuntimeError("native worker failed"))
        ).recognize(segment(self.image))

        self.assertEqual(invalid.error_code, "APPLE_VISION_INVALID_IMAGE")
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error_code, "APPLE_VISION_WORKER_FAILED")
        self.assertNotIn("authorization", json.dumps(failed.to_unified_dict()).lower())

    def test_empty_detection_is_completed_no_text_and_zero_cost(self):
        result = AppleVisionOCRAdapter(FakeVisionClient({
            "detections": [], "image": {"width": 20, "height": 10},
            "inference_ms": 2.0, "network_request_count": 0,
        })).recognize(segment(self.image))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.text, "")
        self.assertEqual(result.confidence, 0)
        self.assertEqual(result.cost, 0)


class AppleVisionNativeWorkerTests(unittest.TestCase):
    @unittest.skipUnless(platform.system() == "Darwin", "Apple Vision requires macOS")
    def test_native_worker_ready_result_and_clean_shutdown(self):
        source = (
            Path(__file__).parents[1] / "cache" / "content_extractions" /
            "runtime" / "ocr" / "apple_vision" / "source" /
            "apple_vision_worker.m"
        )
        self.assertTrue(source.is_file(), "native worker source must exist")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / "apple-vision-worker"
            module_cache = root / "module-cache"
            module_cache.mkdir()
            env = dict(os.environ, CLANG_MODULE_CACHE_PATH=str(module_cache))
            subprocess.run([
                "xcrun", "clang", "-fobjc-arc", "-O2",
                "-framework", "Foundation", "-framework", "Vision",
                "-framework", "ImageIO", str(source), "-o", str(binary),
            ], check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            image = root / "blank.png"
            import cv2
            import numpy
            self.assertTrue(cv2.imwrite(str(image), numpy.zeros((128, 128, 3), dtype=numpy.uint8)))
            client = AppleVisionWorkerClient(AppleVisionWorkerConfig(
                binary_path=str(binary), state_dir=str(root / "state"), timeout_seconds=10,
            ))
            try:
                payload = client.recognize(image)
                self.assertEqual(payload["event"], "result")
                self.assertEqual(payload["network_request_count"], 0)
                self.assertIn("detections", payload)
                self.assertEqual(client.inference_count, 1)
            finally:
                client.close()
            self.assertEqual(client.worker_exit, "clean")


if __name__ == "__main__":
    unittest.main()
