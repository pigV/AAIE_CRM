import tempfile
import unittest
from pathlib import Path

from content_extraction.local_pipeline import LocalExtractionWorkflow, LocalSubtitlePipeline
from content_extraction.models import DownloadResult, OCRResult, Platform, SubtitleFrame
from content_extraction.ocr.parallel import ParallelOCRRunner
from content_extraction.ocr.local_first import FrameOCRFusion, FusionMetricsSnapshot
from content_extraction.preprocessing.ffmpeg_frames import FFmpegFrameError, SamplingProfile
from content_extraction.preprocessing.opencv_subtitles import CandidateEvidence, DetectedSubtitleCrop
from content_extraction.repository import ContentExtractionRepository


class FakeFrameExtractor:
    def extract(self, _video_path, output_dir, profile):
        self.profile = profile
        return tuple(
            SubtitleFrame(index, index * 500, str(Path(output_dir) / f"frame-{index}.jpg"), f"f{index}", 420, 240)
            for index in range(4)
        )


class FakeDetector:
    def detect(self, _frame_path, output_path, region_override=None):
        index = int(Path(output_path).stem.rsplit("-", 1)[-1])
        signatures = (0xFFFF0000FFFF0000, 0xFFFF0000FFFF0001,
                      0xFFFF0000FFFF0000, 0xFFFF0000FFFF0001)
        return DetectedSubtitleCrop(
            str(output_path), f"hash-{index % 2}", signatures[index], 0.8,
            None, 400, 100,
        )


class CountingPaddleAdapter:
    provider = "paddle"
    model_version = "PP-OCRv5"

    def __init__(self):
        self.calls = 0

    def recognize(self, segment):
        self.calls += 1
        return OCRResult(
            "paddle", segment.start_ms, segment.end_ms,
            "孩子最好在10岁以前进入英语环境", 0.96, (),
            "completed", "PP-OCRv5", "", "",
        )


class FailingExtractor:
    def extract(self, *_args, **_kwargs):
        raise FFmpegFrameError("FRAME_EXTRACTION_FAILED")


class LocalSubtitlePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ids = iter(("run-1", *(f"segment-{index}" for index in range(1, 11))))
        self.repo = ContentExtractionRepository(
            self.root / "service.db", id_factory=lambda: next(ids)
        )
        self.repo.initialize()
        run = self.repo.create_run(8, "upload", str(self.root / "video.mp4"))
        self.repo.transition(run.id, "ingesting_upload")
        self.repo.save_media_result(run.id, str(self.root / "video.mp4"), "video-sha", 2000)
        self.repo.transition(run.id, "downloaded")

    def test_deduplicates_before_paddle_and_persists_recommended_text(self):
        adapter = CountingPaddleAdapter()
        pipeline = LocalSubtitlePipeline(
            self.repo,
            FakeFrameExtractor(),
            FakeDetector(),
            lambda: ParallelOCRRunner(self.repo, (adapter,)),
            self.root / "cache",
            SamplingProfile("test-2fps", 2, 20, 10),
            preprocessing_version="opencv-test-v1",
        )

        result = pipeline.process("run-1", self.root / "video.mp4")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.segment_count, 1)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(result.recommended_text, "孩子最好在10岁以前进入英语环境")
        self.assertEqual(self.repo.get_run("run-1").current_stage, "recognizing_ocr")
        self.assertEqual(self.repo.recommended_text_for_run("run-1"), result.recommended_text)

    def test_local_first_fusion_is_used_and_persists_compatible_alignment(self):
        class Coordinator:
            def __init__(self):
                self.calls = 0

            def fuse(self, segment):
                self.calls += 1
                return FrameOCRFusion(
                    segment.id, segment.start_ms, segment.start_ms, segment.end_ms,
                    "澳洲低龄留学", "澳洲低龄留学", "LOCAL_CONSENSUS_EXACT", "HIGH",
                    ("paddle", "easyocr"), ("paddle", "easyocr"),
                    ("paddle", "easyocr"), (), False, 0, 2, (), (), (), (),
                    "paddle", "two local providers agree", {},
                    FusionMetricsSnapshot({}, {}),
                )

        coordinator = Coordinator()
        pipeline = LocalSubtitlePipeline(
            self.repo, FakeFrameExtractor(), FakeDetector(), lambda: coordinator,
            self.root / "cache", SamplingProfile("test-2fps", 2, 20, 10),
            preprocessing_version="opencv-test-v1",
        )
        result = pipeline.process("run-1", self.root / "video.mp4")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.recommended_text, "澳洲低龄留学")
        self.assertEqual(coordinator.calls, 1)
        self.assertEqual(self.repo.recommended_text_for_run("run-1"), "澳洲低龄留学")

    def test_ocr_stage_returns_live_fusion_without_completing_job(self):
        class Coordinator:
            def fuse(self, segment):
                return FrameOCRFusion(
                    segment.id, segment.start_ms, segment.start_ms, segment.end_ms,
                    "澳洲低龄留学", "澳洲低龄留学", "LOCAL_CONSENSUS_EXACT", "HIGH",
                    ("paddle", "apple_vision"), ("paddle", "apple_vision"),
                    ("paddle", "apple_vision"), (), False, 0, 2, (), (), (), (),
                    "paddle", "two local providers agree", {},
                    FusionMetricsSnapshot({}, {}),
                )

        pipeline = LocalSubtitlePipeline(
            self.repo, FakeFrameExtractor(), FakeDetector(), lambda: Coordinator(),
            self.root / "cache", SamplingProfile("test-2fps", 2, 20, 10),
            preprocessing_version="opencv-test-v1",
        )

        result = pipeline.process("run-1", self.root / "video.mp4", complete_job=False)

        self.assertEqual(result.status, "completed")
        self.assertIsNotNone(result.ocr_fusion_result)
        self.assertEqual(result.ocr_fusion_result.text, "澳洲低龄留学")
        self.assertEqual(self.repo.get_run("run-1").current_stage, "recognizing_ocr")

    def test_post_ocr_evidence_gate_excludes_screen_document_text(self):
        class EvidenceDetector:
            def detect(self, _frame_path, output_path, region_override=None):
                index = int(Path(output_path).stem.rsplit("-", 1)[-1])
                classification = "candidate" if index % 2 == 0 else "screen_document"
                evidence = CandidateEvidence.for_test(classification=classification)
                signatures = (0, (1 << 64) - 1, 0xAAAAAAAAAAAAAAAA, 0x5555555555555555)
                return DetectedSubtitleCrop(
                    str(output_path), f"hash-{index}", signatures[index], 0.8,
                    evidence.region, 400, 100, evidence,
                )

        class Coordinator:
            def fuse(self, segment):
                text = "真正字幕" if segment.image_hash in {"hash-0", "hash-2"} else "页面正文99"
                return FrameOCRFusion(
                    segment.id, segment.start_ms, segment.start_ms, segment.end_ms,
                    text, text, "LOCAL_CONSENSUS_EXACT", "HIGH",
                    ("paddle", "apple_vision"), ("paddle", "apple_vision"),
                    ("paddle", "apple_vision"), (), False, 0, 2, (), (), (), (),
                    "paddle", "two local providers agree", {},
                    FusionMetricsSnapshot({}, {}),
                )

        pipeline = LocalSubtitlePipeline(
            self.repo, FakeFrameExtractor(), EvidenceDetector(), lambda: Coordinator(),
            self.root / "cache", SamplingProfile("test-2fps", 2, 20, 10),
            preprocessing_version="opencv-test-v1",
        )

        result = pipeline.process(
            "run-1", self.root / "video.mp4", source_video_id="job_8"
        )

        self.assertEqual(result.ocr_fusion_result.source_video_id, "job_8")
        self.assertIn("真正字幕", result.recommended_text)
        self.assertNotIn("页面正文99", result.recommended_text)
        self.assertEqual(len(result.ocr_fusion_result.excluded_frame_ids), 2)

    def test_local_pipeline_failure_marks_only_run_failed_with_safe_message(self):
        pipeline = LocalSubtitlePipeline(
            self.repo, FailingExtractor(), FakeDetector(),
            lambda: None, self.root / "cache",
            SamplingProfile("test-2fps", 2, 20, 10),
            preprocessing_version="opencv-test-v1",
        )

        result = pipeline.process("run-1", self.root / "video.mp4")

        self.assertEqual(result.status, "failed")
        run = self.repo.get_run("run-1")
        self.assertEqual(run.current_stage, "failed")
        self.assertEqual(run.error_message, "本地字幕识别失败")
        self.assertNotIn("FRAME_EXTRACTION_FAILED", run.error_message)

    def test_upload_workflow_persists_media_then_runs_pipeline_on_same_task(self):
        calls = []
        repo = ContentExtractionRepository(
            self.root / "workflow.db", id_factory=lambda: "workflow-run"
        )
        repo.initialize()
        run = repo.create_run(9, "upload", str(self.root / "incoming.mp4"))
        repo.transition(run.id, "ingesting_upload")
        stored_path = self.root / "stored.mp4"

        class UploadAdapter:
            def acquire(self, request):
                calls.append(("acquire", request.run_id, request.upload_path))
                return DownloadResult(
                    Platform.LOCAL_UPLOAD, "", "", "", "",
                    str(stored_path), 2100, "stored-sha", "downloaded",
                )

        class Pipeline:
            def process(self, run_id, video_path):
                calls.append(("pipeline", run_id, str(video_path)))
                return "done"

        workflow = LocalExtractionWorkflow(repo, UploadAdapter(), Pipeline())

        result = workflow.process_upload("workflow-run")

        self.assertEqual(result, "done")
        self.assertEqual(calls[0][0], "acquire")
        self.assertEqual(calls[1], ("pipeline", "workflow-run", str(self.root / "stored.mp4")))
        run = repo.get_run("workflow-run")
        self.assertEqual(run.video_path, str(self.root / "stored.mp4"))
        self.assertEqual(run.file_hash, "stored-sha")


if __name__ == "__main__":
    unittest.main()
