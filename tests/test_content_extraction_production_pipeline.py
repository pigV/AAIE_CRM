import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from content_extraction.production_pipeline import build_production_pipeline_runner
from content_extraction.repository import ContentExtractionRepository
from content_extraction.service_pipeline import ServicePipelineRunner


class ProductionPipelineAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = ContentExtractionRepository(self.root / "service.db")
        self.repo.initialize()
        self.workflow = SimpleNamespace(upload_adapter=object(), subtitle_pipeline=object())

    def test_factory_wires_accepted_local_providers_without_running_them(self):
        runner = build_production_pipeline_runner(
            self.repo, self.root, {"BILLING_MODE": "FREE_ONLY"}, self.workflow,
        )

        self.assertIsInstance(runner, ServicePipelineRunner)
        self.assertEqual(runner.asr_stage.funasr_adapter.provider, "funasr")
        self.assertFalse(runner.asr_stage.funasr_adapter.config.download_enabled)
        self.assertEqual(runner.asr_stage.whisper_adapter.provider, "whispercpp")
        self.assertFalse(runner.asr_stage.whisper_adapter.config.download_enabled)
        self.assertEqual(runner.asr_stage.whisper_adapter.config.language, "zh")
        self.assertEqual(runner.accurate_stage.runtime_factory.__name__, "runtime_factory")
        self.assertIsNotNone(runner.review_stage.refinement_stage)
        arbiter = runner.review_stage.refinement_stage.arbiter_factory("sha256:test")
        self.assertEqual(arbiter.config.model, "lite")
        self.assertEqual(arbiter.config.max_attempts, 1)
        self.assertEqual(arbiter.config.billing_mode, "FREE_ONLY")

    def test_factory_rejects_non_free_billing_mode(self):
        with self.assertRaisesRegex(RuntimeError, "billing_mode_must_be_free_only"):
            build_production_pipeline_runner(
                self.repo, self.root, {"BILLING_MODE": "PAID"}, self.workflow,
            )

    def test_qwen_integrity_is_verified_once_per_pipeline_runner(self):
        from content_extraction import production_pipeline

        runner = build_production_pipeline_runner(
            self.repo, self.root, {"BILLING_MODE": "FREE_ONLY"}, self.workflow,
        )

        def verified_digest(path):
            if Path(path).name == "llama-server":
                return production_pipeline.QWEN3_8B_BINARY_SHA256
            return production_pipeline.QWEN3_8B_MODEL_SHA256

        with mock.patch.object(
            production_pipeline, "_sha256_file", side_effect=verified_digest,
        ) as digest:
            runner.accurate_stage.runtime_factory()
            runner.accurate_stage.runtime_factory()

        self.assertEqual(digest.call_count, 2)


if __name__ == "__main__":
    unittest.main()
