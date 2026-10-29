import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from content_extraction.pipeline_stages import (
    ASRStage,
    AccurateTranscriptStage,
    CrossModalStage,
    SubtitleOCRStage,
)


class PipelineStageTests(unittest.TestCase):
    def test_ocr_wrapper_returns_live_fusion_and_disables_job_completion(self):
        calls = []
        fusion = object()

        class Pipeline:
            def process(self, run_id, video_path, complete_job, source_video_id):
                calls.append((run_id, str(video_path), complete_job, source_video_id))
                return SimpleNamespace(
                    status="completed", segment_count=2,
                    recommended_text="字幕", ocr_fusion_result=fusion,
                )

        result = SubtitleOCRStage(Pipeline()).run(SimpleNamespace(
            run_id="run-1", video_path="/private/source.mp4", source_video_id="job_1",
        ))

        self.assertEqual(calls, [("run-1", "/private/source.mp4", False, "job_1")])
        self.assertIs(result.fusion_result, fusion)
        self.assertEqual(result.recommended_text, "字幕")

    def test_asr_uses_one_audio_for_both_providers_and_isolates_failure(self):
        calls = []
        whisper_result = SimpleNamespace(provider="whispercpp", status="completed", full_text="口播")

        class FailingFunASR:
            provider = "funasr"

            def transcribe(self, audio_path, **kwargs):
                calls.append(("funasr", str(audio_path), kwargs))
                raise RuntimeError("private provider detail")

        class Whisper:
            provider = "whispercpp"

            def transcribe(self, audio_path, **kwargs):
                calls.append(("whispercpp", str(audio_path), kwargs))
                return whisper_result

        class Fusion:
            def fuse(self, funasr, whisper):
                calls.append(("fusion", funasr.status, whisper.status))
                return SimpleNamespace(fusion_status="ASR_SINGLE_PROVIDER")

        context = SimpleNamespace(
            audio=SimpleNamespace(path="/private/normalized.wav", sha256="audio-sha", duration_ms=1000),
            source_video_id="video-1",
        )
        result = ASRStage(FailingFunASR(), Whisper(), Fusion()).run(context)

        self.assertEqual([item[0] for item in calls], ["funasr", "whispercpp", "fusion"])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(result.funasr_result.status, "failed")
        self.assertIs(result.whisper_result, whisper_result)
        self.assertEqual(result.fusion_result.fusion_status, "ASR_SINGLE_PROVIDER")
        self.assertNotIn("private provider detail", result.funasr_result.error_message)

    def test_cross_modal_receives_live_same_job_objects(self):
        seen = []
        output = SimpleNamespace(clean_raw_transcript="clean")

        class Engine:
            def fuse(self, ocr, asr):
                seen.append((ocr, asr))
                return output

        ocr = object()
        asr = object()
        result = CrossModalStage(Engine()).run(SimpleNamespace(
            ocr_fusion_result=ocr, asr_fusion_result=asr,
        ))

        self.assertIs(result, output)
        self.assertEqual(seen, [(ocr, asr)])

    def test_accurate_stage_builds_live_bundle_and_always_stops_runtime(self):
        events = []
        cross = object()
        bundle = SimpleNamespace(source_video_id="video-1")
        accurate = SimpleNamespace(status="completed", final_accurate_transcript="准确文案")

        class Runtime:
            endpoint = "http://127.0.0.1:18091"

            def __enter__(self):
                events.append("runtime_start")
                return self

            def __exit__(self, *_args):
                events.append("runtime_stop")

        class Corrector:
            def __init__(self, proposal_engine):
                events.append(("corrector", proposal_engine))

            def correct(self, supplied):
                events.append(("correct", supplied))
                return accurate

        proposal = object()
        stage = AccurateTranscriptStage(
            glossary=SimpleNamespace(version="v1", fingerprint="sha256:g", domain="education"),
            runtime_factory=Runtime,
            proposal_engine_factory=lambda endpoint: events.append(("proposal", endpoint)) or proposal,
            corrector_factory=lambda proposal_engine: Corrector(proposal_engine),
            bundle_builder=lambda result, glossary: events.append(("bundle", result, glossary)) or bundle,
        )

        result = stage.run(SimpleNamespace(cross_modal_result=cross))

        self.assertIs(result.evidence_bundle, bundle)
        self.assertIs(result.correction_result, accurate)
        self.assertEqual(events[0][0], "bundle")
        self.assertIn("runtime_start", events)
        self.assertEqual(events[-1], "runtime_stop")

    def test_accurate_stage_runtime_failure_does_not_create_corrector(self):
        events = []

        class Runtime:
            def __enter__(self):
                raise RuntimeError("model_missing")

            def __exit__(self, *_args):
                events.append("unexpected")

        stage = AccurateTranscriptStage(
            glossary=SimpleNamespace(version="v1", fingerprint="sha256:g", domain="education"),
            runtime_factory=Runtime,
            proposal_engine_factory=lambda _endpoint: events.append("proposal"),
            corrector_factory=lambda _proposal: events.append("corrector"),
            bundle_builder=lambda _result, _glossary: object(),
        )

        with self.assertRaisesRegex(RuntimeError, "model_missing"):
            stage.run(SimpleNamespace(cross_modal_result=object()))
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
