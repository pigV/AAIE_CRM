from dataclasses import dataclass
from pathlib import Path

from .models import DownloadRequest, Platform, RecognitionAlignment
from .ocr.consensus import choose_ocr_consensus
from .ocr.temporal import merge_fused_frames
from .ocr.evidence_gate import evaluate_frame_evidence
from .preprocessing.subtitle_segments import (
    SubtitleObservation, build_subtitle_segments, classify_subtitle_segments,
)


@dataclass(frozen=True)
class LocalPipelineResult:
    status: str
    segment_count: int
    recommended_text: str
    error_code: str = ""
    ocr_fusion_result: object | None = None


class LocalSubtitlePipeline:
    def __init__(
        self, repository, frame_extractor, subtitle_detector, ocr_runner_factory,
        cache_root, sampling_profile, preprocessing_version,
        similarity_threshold=0.92,
    ):
        self.repository = repository
        self.frame_extractor = frame_extractor
        self.subtitle_detector = subtitle_detector
        self.ocr_runner_factory = ocr_runner_factory
        self.cache_root = Path(cache_root)
        self.sampling_profile = sampling_profile
        self.preprocessing_version = str(preprocessing_version)
        self.similarity_threshold = float(similarity_threshold)

    def process(self, run_id, video_path, complete_job=False, source_video_id=""):
        try:
            run = self.repository.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            self.repository.transition(run_id, "extracting_frames")
            run_root = self.cache_root / str(run_id)
            frames_root = run_root / "frames"
            crops_root = run_root / "subtitle_crops"
            frames = self.frame_extractor.extract(
                video_path, frames_root, self.sampling_profile
            )

            self.repository.transition(run_id, "segmenting_subtitles")
            observations = []
            evidence_by_path = {}
            for frame in frames:
                crop = self.subtitle_detector.detect(
                    frame.path, crops_root / f"crop-{frame.index:06d}.jpg"
                )
                if crop is None:
                    continue
                observations.append(
                    SubtitleObservation(
                        frame.timestamp_ms, crop.path, crop.image_hash,
                        crop.perceptual_signature, crop.content_score,
                    )
                )
                if crop.evidence is not None:
                    evidence_by_path[crop.path] = crop.evidence
            duration_ms = int(run.duration_ms or 0)
            if observations and duration_ms <= observations[-1].timestamp_ms:
                duration_ms = observations[-1].timestamp_ms + self.sampling_profile.interval_ms
            elif not observations and duration_ms <= 0:
                duration_ms = len(frames) * self.sampling_profile.interval_ms
            segments = build_subtitle_segments(
                run_id, observations, duration_ms, self.preprocessing_version,
                similarity_threshold=self.similarity_threshold,
            ) if observations else ()
            original_roles = classify_subtitle_segments(
                segments, evidence_by_path,
            ) if evidence_by_path else {
                segment.id: "spoken_subtitle" for segment in segments
            }

            persisted = tuple(
                self.repository.add_subtitle_segment(
                    run_id, segment.start_ms, segment.end_ms,
                    segment.representative_path, segment.image_hash,
                    segment.preprocessing_version,
                )
                for segment in segments
            )
            segment_roles = {
                saved.id: original_roles.get(original.id, "unknown")
                for original, saved in zip(segments, persisted)
            }
            segment_evidence = {
                saved.id: evidence_by_path.get(original.representative_path)
                for original, saved in zip(segments, persisted)
            }
            self.repository.transition(run_id, "recognizing_ocr")
            runner = self.ocr_runner_factory()
            ocr_fusion_result = None
            if callable(getattr(runner, "fuse", None)):
                from .conflicts import classify_conflicts

                frame_fusions = tuple(runner.fuse(segment) for segment in persisted)
                evidence_decisions = {
                    frame.frame_id: evaluate_frame_evidence(
                        frame, segment_roles.get(frame.frame_id, "unknown"),
                        segment_evidence.get(frame.frame_id),
                    )
                    for frame in frame_fusions
                }
                frame_by_id = {item.frame_id: item for item in frame_fusions}
                for frame in frame_fusions:
                    variants = tuple(
                        dict.fromkeys(
                            result.text for result in frame.provider_results
                            if result.status == "completed" and result.text
                        )
                    )
                    if len(variants) > 1:
                        self.repository.save_ocr_conflicts(classify_conflicts(
                            frame.frame_id, frame.fused_text, variants
                        ))
                transcript = merge_fused_frames(
                    frame_fusions,
                    source_video_id=source_video_id,
                    duration_ms=duration_ms,
                    segment_roles=segment_roles,
                    evidence_decisions=evidence_decisions,
                )
                ocr_fusion_result = transcript
                for item in transcript.segments:
                    contributing = tuple(
                        frame_by_id[frame_id]
                        for frame_id in item.frame_ids if frame_id in frame_by_id
                    )
                    sources = tuple(dict.fromkeys(
                        provider for frame in contributing for provider in frame.providers_used
                    ))
                    reliability = (
                        "high" if contributing and all(frame.confidence_level == "HIGH" for frame in contributing)
                        else "low"
                    )
                    self.repository.save_recognition_alignment(
                        run_id,
                        RecognitionAlignment(
                            item.frame_ids[0], item.start_ms, item.end_ms,
                            item.text, reliability, sources,
                        ),
                    )
                recommended_text = transcript.text
            else:
                recommended = []
                previous_text = ""
                for segment in persisted:
                    batch = runner.recognize(segment)
                    decision = choose_ocr_consensus(
                        batch.results, previous_text=previous_text, segment_id=segment.id
                    )
                    if decision.recommended_text:
                        recommended.append(decision.recommended_text)
                        previous_text = decision.recommended_text
                    alignment = RecognitionAlignment(
                        segment.id, segment.start_ms, segment.end_ms,
                        decision.recommended_text, decision.reliability,
                        tuple(result.provider for result in batch.results),
                    )
                    self.repository.save_recognition_alignment(run_id, alignment)
                    self.repository.save_ocr_conflicts(decision.conflicts)
                recommended_text = "\n".join(recommended)
            return LocalPipelineResult(
                "completed", len(persisted), recommended_text,
                ocr_fusion_result=ocr_fusion_result,
            )
        except Exception:
            current = self.repository.get_run(run_id)
            if current and current.current_stage not in {"failed", "completed"}:
                try:
                    self.repository.transition(
                        run_id, "failed", "LOCAL_SUBTITLE_FAILED", "本地字幕识别失败"
                    )
                except Exception:
                    pass
            return LocalPipelineResult("failed", 0, "", "LOCAL_SUBTITLE_FAILED")


class LocalExtractionWorkflow:
    def __init__(self, repository, upload_adapter, subtitle_pipeline):
        self.repository = repository
        self.upload_adapter = upload_adapter
        self.subtitle_pipeline = subtitle_pipeline

    def process_upload(self, run_id):
        run = self.repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.current_stage != "ingesting_upload":
            raise ValueError("run is not ingesting an upload")
        try:
            media = self.upload_adapter.acquire(
                DownloadRequest(
                    run.id, run.crm_job_id, Platform.LOCAL_UPLOAD,
                    upload_path=run.upload_path,
                )
            )
            self.repository.save_media_result(
                run.id, media.video_path, media.file_hash, media.duration_ms
            )
            self.repository.transition(run.id, "downloaded")
            return self.subtitle_pipeline.process(run.id, media.video_path)
        except Exception:
            current = self.repository.get_run(run.id)
            if current and current.current_stage not in {"failed", "completed"}:
                try:
                    self.repository.transition(
                        run.id, "failed", "UPLOAD_PROCESSING_FAILED", "上传视频处理失败"
                    )
                except Exception:
                    pass
            raise
