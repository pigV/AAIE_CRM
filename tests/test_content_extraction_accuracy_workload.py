import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from content_extraction.accuracy.integration import AccurateTranscriptCoordinator
from content_extraction.accuracy.review_refinement import ReviewRefinementResult
from content_extraction.accuracy.review_queue import ReviewQueue
from content_extraction.accuracy.workload import ReviewWorkloadStage


def segment(segment_id="cm-0001", text="这是一段足够长而且正确的测试文案", *, conflicts=(), alternatives=(), critical_tokens=()):
    return SimpleNamespace(
        segment_id=segment_id, start_ms=1000, end_ms=3000,
        clean_text=text, alternatives=tuple(alternatives), conflicts=tuple(conflicts),
        critical_tokens=tuple(critical_tokens), provider_evidence=(
            SimpleNamespace(provider="funasr", text=text),
            SimpleNamespace(provider="whispercpp", text=text),
        ),
    )


def correction_segment(source, text=None):
    value = text if text is not None else source.clean_text
    return SimpleNamespace(
        segment_id=source.segment_id, start_ms=source.start_ms, end_ms=source.end_ms,
        source_text=source.clean_text, carrier_text=source.clean_text, final_text=value,
    )


class ReviewWorkloadStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "quick_review"
        self.clips = self.runtime / "clips"
        self.video = self.root / "source.mp4"
        self.video.write_bytes(b"video")

        def clip_runner(command, **_kwargs):
            Path(command[command.index("--output") + 1]).write_bytes(b"clip")

        self.stage = ReviewWorkloadStage(
            self.runtime, self.clips, clip_runner=clip_runner
        )

    def test_no_review_writes_carriers_and_projects_auto_complete(self):
        source = segment()
        bundle = SimpleNamespace(source_video_id="video-1", duration_ms=5000, segments=(source,))
        correction = SimpleNamespace(
            segments=(correction_segment(source),), rejected_candidates=(),
            unresolved_conflicts=(), final_accurate_transcript=source.clean_text,
        )

        result = self.stage.run(42, bundle, correction, self.video)
        projection = AccurateTranscriptCoordinator(self.runtime, self.clips).status_for(42)

        self.assertEqual(result.status, "TRANSCRIPT_READY")
        self.assertFalse((self.runtime / "queues/job_42.json").exists())
        self.assertTrue((self.runtime / "carriers/job_42.json").is_file())
        self.assertEqual(projection["status"], "TRANSCRIPT_READY")
        self.assertEqual(projection["readiness"], "CLEAN")
        self.assertEqual(projection["final_accurate_transcript"], source.clean_text)

    def test_conflict_generates_immutable_queue_clip_and_complete_uncertain_transcript(self):
        source = segment(
            text="孩子最好在10岁以前进入一个完整并且连续的英语学习环境",
            conflicts=({"conflict_type": "ASR_CRITICAL_TOKEN_CONFLICT"},),
            alternatives=("另一种说法",),
            critical_tokens=({"value": "10", "token_type": "NUMBER"},),
        )
        bundle = SimpleNamespace(source_video_id="video-1", duration_ms=5000, segments=(source,))
        correction = SimpleNamespace(
            segments=(correction_segment(source),), rejected_candidates=(),
            unresolved_conflicts=({"segment_id": source.segment_id, "reason": "unresolved"},),
            final_accurate_transcript=source.clean_text,
        )

        result = self.stage.run(43, bundle, correction, self.video)
        projection = AccurateTranscriptCoordinator(self.runtime, self.clips).status_for(43)

        self.assertEqual(result.status, "TRANSCRIPT_READY")
        self.assertEqual(projection["status"], "TRANSCRIPT_READY")
        self.assertEqual(projection["readiness"], "WITH_UNCERTAINTIES")
        self.assertTrue(projection["final_available"])
        self.assertEqual(projection["final_accurate_transcript"], source.clean_text)
        self.assertEqual(len(projection["uncertainty_metadata"]), 1)
        self.assertEqual(len(result.clip_paths), 1)
        self.assertTrue(Path(result.clip_paths[0]).is_file())
        self.assertEqual((self.runtime / "queues/job_43.json").stat().st_mode & 0o777, 0o600)

    def test_large_workload_is_transcript_ready_and_historical_limit_is_diagnostic(self):
        sources = tuple(
            segment(
                f"cm-{index:04d}", f"第{index}段需要确认的长文本内容",
                conflicts=({"conflict_type": "ASR_CONFLICT"},),
                alternatives=(f"候选{index}",),
            )
            for index in range(1, 17)
        )
        bundle = SimpleNamespace(source_video_id="video-2", duration_ms=5000, segments=sources)
        correction = SimpleNamespace(
            segments=tuple(correction_segment(item) for item in sources),
            rejected_candidates=(), unresolved_conflicts=(),
            final_accurate_transcript="".join(item.clean_text for item in sources),
        )

        result = self.stage.run(44, bundle, correction, self.video)
        projection = AccurateTranscriptCoordinator(self.runtime, self.clips).status_for(44)

        self.assertEqual(result.status, "TRANSCRIPT_READY")
        self.assertEqual(projection["status"], "TRANSCRIPT_READY")
        self.assertEqual(projection["readiness"], "WITH_UNCERTAINTIES")
        self.assertEqual(result.metrics["historical_limited_review_gate"], "FAIL")
        self.assertTrue(projection["final_available"])

    def test_required_clip_failure_does_not_publish_queue(self):
        source = segment(
            conflicts=({"conflict_type": "ASR_CONFLICT"},), alternatives=("候选",),
        )
        bundle = SimpleNamespace(source_video_id="video-3", duration_ms=5000, segments=(source,))
        correction = SimpleNamespace(
            segments=(correction_segment(source),), rejected_candidates=(),
            unresolved_conflicts=(), final_accurate_transcript=source.clean_text,
        )
        stage = ReviewWorkloadStage(
            self.runtime, self.clips,
            clip_runner=lambda *_args, **_kwargs: None,
        )

        with self.assertRaisesRegex(RuntimeError, "clip_generation_missing_output"):
            stage.run(45, bundle, correction, self.video)
        self.assertFalse((self.runtime / "queues/job_45.json").exists())

    def test_refinement_runs_before_queue_publish_and_updates_canonical_carrier(self):
        source = segment(
            text="高中来个下马杯开始选课",
            conflicts=({"conflict_type": "ASR_CONFLICT"},),
            alternatives=("高中来个下马威开始选课",),
        )
        bundle = SimpleNamespace(source_video_id="video-4", duration_ms=5000, segments=(source,))
        correction = SimpleNamespace(
            segments=(correction_segment(source),), rejected_candidates=(),
            unresolved_conflicts=(), final_accurate_transcript=source.clean_text,
        )
        calls = []

        class Refinement:
            def run(self, queue, carriers):
                calls.append((queue, dict(carriers)))
                return ReviewRefinementResult(
                    ReviewQueue.create(queue.source_video_id, 12, (), 1),
                    {source.segment_id: "高中来个下马威开始选课"},
                    ({"decision": "AUTO_RESOLVED"},),
                    {"spark_lite": {"current_run_delta": 1,
                                    "checkpoint_accumulated": 0,
                                    "diagnostic_rerun_total": 0}},
                )

        stage = ReviewWorkloadStage(
            self.runtime, self.clips, clip_runner=self.stage.clip_runner,
            refinement_stage=Refinement(),
        )
        result = stage.run(46, bundle, correction, self.video)
        projection = AccurateTranscriptCoordinator(self.runtime, self.clips).status_for(46)

        self.assertEqual(len(calls), 1)
        self.assertEqual(result.status, "TRANSCRIPT_READY")
        self.assertEqual(result.accounting["spark_lite"]["current_run_delta"], 1)
        self.assertEqual(projection["final_accurate_transcript"], "高中来个下马威开始选课")
        self.assertFalse((self.runtime / "queues/job_46.json").exists())


if __name__ == "__main__":
    unittest.main()
