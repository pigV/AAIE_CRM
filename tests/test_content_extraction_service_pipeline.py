import sqlite3
import tempfile
import unittest
from pathlib import Path

from content_extraction.accuracy.workload import ReviewWorkloadResult
from content_extraction.link_ingest import MediaStageResult
from content_extraction.models import ASRResult, OCRResult
from content_extraction.ocr.local_first import FrameOCRFusion, FusionMetricsSnapshot
from content_extraction.ocr.temporal import OCRFusedTranscript, TemporalOCRSegment
from content_extraction.pipeline_stages import (
    ASRStageResult, AccurateStageResult, OCRStageResult,
)
from content_extraction.preprocessing.audio import NormalizedAudio
from content_extraction.repository import ContentExtractionRepository
from content_extraction.service_pipeline import ServicePipelineRunner


class ServicePipelineRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = ContentExtractionRepository(self.root / "service.db", id_factory=lambda: "run-1")
        self.repo.initialize()
        self.run = self.repo.create_run(81, "分享 https://xhslink.com/o/a")
        self.video = self.root / "source.mp4"
        self.video.write_bytes(b"video")
        self.audio = self.root / "normalized.wav"
        self.audio.write_bytes(b"wave")
        self.log = []

    def stages(self, *, review_status="AUTO_COMPLETE", fail_cross_once=False):
        outer = self

        class Link:
            def run(self, run):
                outer.log.append("link_ingest")
                outer.repo.transition(run.id, "resolving_platform")
                outer.repo.save_media_result(run.id, outer.video, "video-sha", 5000)
                outer.repo.transition(run.id, "downloading_video")
                outer.repo.transition(run.id, "downloaded")
                return MediaStageResult(
                    str(outer.video), "video-sha", 5000, "xiaohongshu",
                    "https://xhslink.com/o/a", "https://www.xiaohongshu.com/explore/1",
                )

        class Audio:
            def run(self, video_path, crm_job_id):
                outer.log.append("audio")
                return NormalizedAudio(str(outer.audio), "audio-sha", 5000, 16000, 1, 2)

        class OCR:
            def run(self, context):
                outer.log.append("ocr")
                for stage in ("extracting_frames", "segmenting_subtitles", "recognizing_ocr"):
                    outer.repo.transition(context.run_id, stage)
                return OCRStageResult({"text": "字幕"}, "字幕", 1)

        class ASR:
            calls = 0

            def run(self, _context):
                type(self).calls += 1
                outer.log.append("asr")
                return ASRStageResult(
                    {"provider": "funasr", "status": "completed"},
                    {"provider": "whispercpp", "status": "completed"},
                    {"full_text": "口播", "fusion_status": "ASR_COMPLETE"},
                    {"funasr": {"current_run_delta": 1, "checkpoint_accumulated": 0,
                                "diagnostic_rerun_total": 0}},
                )

        class Cross:
            attempts = 0

            def run(self, _context):
                type(self).attempts += 1
                outer.log.append("cross_modal")
                if fail_cross_once and type(self).attempts == 1:
                    raise RuntimeError("cross failed")
                return {"clean_raw_transcript": "干净文案", "segments": []}

        class Accurate:
            def run(self, _context):
                outer.log.append("accurate")
                return AccurateStageResult(
                    {"source_video_id": "job_81"},
                    {"final_accurate_transcript": "准确文案", "segments": []},
                    {"qwen3_8b": {"current_run_delta": 1, "checkpoint_accumulated": 0,
                                  "diagnostic_rerun_total": 0}},
                )

        class Review:
            def run(self, crm_job_id, _bundle, _correction, _video_path):
                outer.log.append("review")
                carrier = outer.root / f"carriers/job_{crm_job_id}.json"
                carrier.parent.mkdir(parents=True, exist_ok=True)
                carrier.write_text("{}", encoding="utf-8")
                queue = ""
                if review_status != "AUTO_COMPLETE":
                    queue_path = outer.root / f"queues/job_{crm_job_id}.json"
                    if review_status != "MISSING_QUEUE":
                        queue_path.parent.mkdir(parents=True, exist_ok=True)
                        queue_path.write_text("{}", encoding="utf-8")
                    queue = str(queue_path)
                return ReviewWorkloadResult(
                    "REVIEW_REQUIRED" if review_status == "MISSING_QUEUE" else review_status,
                    queue, str(carrier), (), {"review_items": 1 if queue else 0},
                    (), {"spark_lite": {"current_run_delta": 1,
                                        "checkpoint_accumulated": 0,
                                        "diagnostic_rerun_total": 0}},
                )

        return Link(), Audio(), OCR(), ASR(), Cross(), Accurate(), Review(), ASR

    def build_runner(self, *, review_status="AUTO_COMPLETE", fail_cross_once=False,
                     config_fingerprint="sha256:test-config"):
        link, audio, ocr, asr, cross, accurate, review, asr_type = self.stages(
            review_status=review_status, fail_cross_once=fail_cross_once,
        )
        return ServicePipelineRunner(
            self.repo, self.root / "runtime", config_fingerprint,
            link_stage=link, upload_stage=None, audio_stage=audio, ocr_stage=ocr,
            asr_stage=asr, cross_modal_stage=cross,
            accurate_stage=accurate, review_stage=review,
        ), asr_type

    def test_full_link_chain_runs_in_order_before_completed(self):
        runner, _asr_type = self.build_runner()

        result = runner.run(self.run.id)

        self.assertEqual(
            self.log,
            ["link_ingest", "audio", "ocr", "asr", "cross_modal", "accurate", "review"],
        )
        self.assertEqual(result.current_stage, "completed")
        self.assertEqual(result.accurate_status, "AUTO_COMPLETE")
        self.assertEqual(self.repo.get_run(self.run.id).status, "completed")
        self.assertEqual(result.accounting["spark_lite"]["current_run_delta"], 1)

    def test_review_required_without_queue_cannot_complete(self):
        runner, _asr_type = self.build_runner(review_status="MISSING_QUEUE")

        result = runner.run(self.run.id)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "REVIEW_WORKLOAD_INCOMPLETE")
        self.assertEqual(self.repo.get_run(self.run.id).current_stage, "failed")

    def test_retry_resumes_same_job_without_repeating_successful_asr(self):
        runner, first_asr_type = self.build_runner(fail_cross_once=True)
        first = runner.run(self.run.id)
        self.assertEqual(first.status, "failed")
        self.assertEqual(first_asr_type.calls, 1)

        self.repo.transition(self.run.id, "parsing_link")
        self.log.clear()
        restarted_runner, restarted_asr_type = self.build_runner()
        second = restarted_runner.run(self.run.id)

        self.assertEqual(second.current_stage, "completed")
        self.assertEqual(restarted_asr_type.calls, 0)
        self.assertNotIn("asr", self.log)
        self.assertIn("cross_modal", self.log)
        self.assertEqual(
            second.accounting["funasr"]["checkpoint_accumulated"], 1
        )

    def test_completed_job_restores_terminal_status_and_accounting_without_provider_work(self):
        first_runner, _first_asr_type = self.build_runner(
            review_status="TRANSCRIPT_READY",
        )
        first = first_runner.run(self.run.id)
        self.assertEqual(first.accurate_status, "TRANSCRIPT_READY")
        self.log.clear()

        restarted_runner, restarted_asr_type = self.build_runner(
            review_status="TRANSCRIPT_READY",
        )
        resumed = restarted_runner.run(self.run.id)

        self.assertEqual(resumed.current_stage, "completed")
        self.assertEqual(resumed.accurate_status, "TRANSCRIPT_READY")
        self.assertEqual(restarted_asr_type.calls, 0)
        self.assertEqual(self.log, [])
        self.assertEqual(
            resumed.accounting["funasr"]["checkpoint_accumulated"], 1,
        )
        self.assertEqual(
            resumed.accounting["qwen3_8b"]["checkpoint_accumulated"], 1,
        )

    def test_config_fingerprint_mismatch_reexecutes_provider_stage(self):
        first_runner, first_asr_type = self.build_runner(
            fail_cross_once=True, config_fingerprint="sha256:config-v1",
        )
        self.assertEqual(first_runner.run(self.run.id).status, "failed")
        self.assertEqual(first_asr_type.calls, 1)
        self.repo.transition(self.run.id, "parsing_link")
        self.log.clear()

        second_runner, second_asr_type = self.build_runner(
            config_fingerprint="sha256:config-v2",
        )
        result = second_runner.run(self.run.id)

        self.assertEqual(result.current_stage, "completed")
        self.assertEqual(second_asr_type.calls, 1)
        self.assertIn("asr", self.log)

    def test_controlled_full_chain_does_not_change_database_schema(self):
        def schema_snapshot():
            with sqlite3.connect(self.root / "service.db") as connection:
                sql = tuple(connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type IN ('table','index') ORDER BY name"
                ))
                columns = {
                    name: tuple(connection.execute(f"PRAGMA table_info({name})"))
                    for name, _definition in connection.execute(
                        "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
                    )
                }
                version = connection.execute(
                    "SELECT value FROM service_meta WHERE key='schema_version'"
                ).fetchone()[0]
            return sql, columns, version

        before = schema_snapshot()
        runner, _asr_type = self.build_runner()
        self.assertEqual(runner.run(self.run.id).current_stage, "completed")

        self.assertEqual(schema_snapshot(), before)

    @staticmethod
    def raw_ocr_result(provider, text, raw_response):
        return OCRResult(
            provider=provider, start_ms=0, end_ms=1000, text=text,
            confidence=0.9, boxes=(), status="completed", model_version="test",
            error_code="", error_message="", raw_response=raw_response,
            billing_type="local", cost=0,
        )

    @staticmethod
    def raw_asr_result(provider, text, raw_response):
        return ASRResult(
            provider=provider, status="completed", full_text=text,
            segments=(), words=(), language="zh", duration_ms=1000,
            request_id=f"local:{provider}", request_id_source="local",
            engine_version="test", model_name="test", model_version="test",
            model_hash="sha256:test", billing_type="local", cost=0,
            raw_response=raw_response, error_code="", error_message="",
            source_video_id="job_81", source_audio_id="audio.wav",
            audio_hash="sha256:audio", timestamp_granularity="none",
            timestamp_quality="none", capabilities={}, latency_ms=1,
        )

    def test_raw_provider_projection_reads_verified_stage_text_without_sensitive_payloads(self):
        runner, _asr_type = self.build_runner()
        metrics = FusionMetricsSnapshot({}, {})
        paddle = self.raw_ocr_result("paddle", "Paddle 原始字幕", {"secret": "never-export"})
        vision = self.raw_ocr_result("apple_vision", "Vision 原始字幕", {"path": "/private/runtime"})
        frame = FrameOCRFusion(
            frame_id="frame-1", timestamp_ms=0, start_ms=0, end_ms=1000,
            fused_text="融合字幕不得冒充原始结果", normalized_text="融合字幕",
            fusion_status="LOCAL_EXACT", confidence_level="HIGH",
            providers_attempted=("paddle", "apple_vision"),
            providers_used=("paddle", "apple_vision"),
            local_providers=("paddle", "apple_vision"), cloud_providers=(),
            cloud_called=False, cloud_call_count=0, agreement_count=2,
            provider_results=(paddle, vision), critical_tokens=(), conflicts=(),
            alternatives=(), selected_source="local", selection_reason="agreement",
            bounding_region={}, metrics=metrics,
        )
        ocr = OCRStageResult(
            OCRFusedTranscript(
                segments=(TemporalOCRSegment(0, 1000, "融合字幕不得冒充原始结果", ("frame-1",), (), ()),),
                frame_fusions=(frame,), source_video_id="job_81", duration_ms=1000,
            ),
            "融合字幕不得冒充原始结果", 1,
        )
        funasr = self.raw_asr_result("funasr", "FunASR 原始口播", {"stdout": "sensitive raw"})
        whisper = self.raw_asr_result("whispercpp", "Whisper 原始口播", {"json_path": "/private/output"})
        asr = ASRStageResult(
            funasr, whisper,
            {"full_text": "ASR 融合稿不得冒充原始结果"}, {},
        )
        store = runner._store(self.run)
        runner._save(store, "recognizing_ocr", "ocr-input", "ocr-result.json", ocr)
        runner._save(store, "fusing_asr", "asr-input", "asr-result.json", asr)

        projection = runner.raw_provider_artifacts_for_run(self.run.id)

        self.assertEqual(projection["schema_version"], "raw-provider-artifacts-v1")
        self.assertEqual(projection["source_run_id"], self.run.id)
        self.assertEqual(projection["ocr"]["provider_texts"], {
            "paddle": "Paddle 原始字幕",
            "apple_vision": "Vision 原始字幕",
        })
        self.assertEqual(projection["asr"]["provider_texts"], {
            "funasr": "FunASR 原始口播",
            "whispercpp": "Whisper 原始口播",
        })
        serialized = str(projection)
        for forbidden in (
            "never-export", "/private/runtime", "sensitive raw", "/private/output",
            "融合字幕不得冒充原始结果", "ASR 融合稿不得冒充原始结果", "raw_response",
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
