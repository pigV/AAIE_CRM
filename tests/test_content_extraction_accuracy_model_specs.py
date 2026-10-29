import hashlib
from pathlib import Path
import tempfile
import unittest


class AccuracyModelSpecTests(unittest.TestCase):
    def test_verified_artifact_accepts_only_matching_size_and_sha256(self):
        from content_extraction.accuracy.model_specs import LocalModelSpec

        payload = b"approved-model-bytes"
        spec = LocalModelSpec(
            engine_name="llama.cpp:test",
            request_model_name="test-model",
            repository="Qwen/test",
            revision="0123456789abcdef0123456789abcdef01234567",
            filename="test.gguf",
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.gguf"
            path.write_bytes(payload)
            self.assertEqual(spec.verify_artifact(path), "sha256:" + spec.sha256)
            path.write_bytes(payload + b"tampered")
            with self.assertRaisesRegex(RuntimeError, "model_size_mismatch"):
                spec.verify_artifact(path)

    def test_qwen14_spec_resolves_the_only_approved_runtime_file(self):
        from content_extraction.accuracy.model_specs import QWEN3_14B_Q4_K_M

        root = Path("/private/runtime")
        self.assertEqual(
            QWEN3_14B_Q4_K_M.model_path(root),
            root / "models" / "Qwen3-14B-Q4_K_M.gguf",
        )
        self.assertEqual(QWEN3_14B_Q4_K_M.size_bytes, 9_001_752_960)
        self.assertEqual(
            QWEN3_14B_Q4_K_M.sha256,
            "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0",
        )
        self.assertEqual(
            QWEN3_14B_Q4_K_M.revision,
            "c75e7b2d0234068f674a1bacf548ea32e27ccd29",
        )

    def test_missing_model_fails_closed_without_download_attempt(self):
        from content_extraction.accuracy.model_specs import QWEN3_14B_Q4_K_M

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "model_missing"):
                QWEN3_14B_Q4_K_M.verify_artifact(
                    QWEN3_14B_Q4_K_M.model_path(Path(directory))
                )


if __name__ == "__main__":
    unittest.main()
