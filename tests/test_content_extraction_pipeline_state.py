import json
import tempfile
import unittest
from pathlib import Path

from content_extraction.pipeline_state import PipelineStateStore


class PipelineStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = PipelineStateStore(self.root, 42, "run-42")

    def test_atomic_private_stage_round_trip_preserves_accounting(self):
        accounting = {
            "funasr": {
                "current_run_delta": 1,
                "checkpoint_accumulated": 0,
                "diagnostic_rerun_total": 0,
            }
        }

        saved = self.store.save_stage(
            "recognizing_asr", "sha256:input", "sha256:config",
            "asr-result.json", {"fusion_status": "ASR_COMPLETE"}, accounting,
        )
        loaded = self.store.load_stage(
            "recognizing_asr", "sha256:input", "sha256:config"
        )

        self.assertEqual(loaded.payload, {"fusion_status": "ASR_COMPLETE"})
        self.assertEqual(loaded.accounting, accounting)
        self.assertEqual(Path(saved.artifact_path).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "pipeline-state.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse(tuple(self.root.glob("*.tmp")))

    def test_identity_or_artifact_hash_mismatch_fails_closed(self):
        saved = self.store.save_stage(
            "downloaded", "sha256:input", "sha256:config",
            "media.json", {"video_path": "/private/video.mp4"}, {},
        )
        Path(saved.artifact_path).write_text("{}", encoding="utf-8")

        self.assertIsNone(self.store.load_stage(
            "downloaded", "sha256:input", "sha256:config"
        ))
        other_run = PipelineStateStore(self.root, 42, "different-run")
        self.assertIsNone(other_run.load_stage(
            "downloaded", "sha256:input", "sha256:config"
        ))

    def test_config_mismatch_invalidates_stage_and_downstream_only(self):
        self.store.save_stage(
            "downloaded", "sha256:link", "sha256:download-v1",
            "media.json", {"video": "one"}, {},
        )
        self.store.save_stage(
            "recognizing_ocr", "sha256:media", "sha256:ocr-v1",
            "ocr-result.json", {"text": "字幕"}, {},
        )
        self.store.save_stage(
            "recognizing_asr", "sha256:audio", "sha256:asr-v1",
            "asr-result.json", {"text": "口播"}, {},
        )

        self.assertIsNone(self.store.load_stage(
            "recognizing_ocr", "sha256:media", "sha256:ocr-v2"
        ))
        state = json.loads((self.root / "pipeline-state.json").read_text(encoding="utf-8"))
        self.assertIn("downloaded", state["stages"])
        self.assertNotIn("recognizing_ocr", state["stages"])
        self.assertNotIn("recognizing_asr", state["stages"])

    def test_unrelated_historical_artifact_is_never_discovered(self):
        historical = self.root.parent / "benchmark-result.json"
        historical.write_text('{"stage":"downloaded"}', encoding="utf-8")

        self.assertIsNone(self.store.load_stage(
            "downloaded", "sha256:any", "sha256:any"
        ))


if __name__ == "__main__":
    unittest.main()
