"""Single production orchestration path for Content Extraction V2."""

from dataclasses import dataclass
import hashlib
from pathlib import Path

from .orchestrator import Phase2Orchestrator
from .pipeline_state import (
    PipelineStateStore, decode_pipeline_value, encode_pipeline_value,
)
from .raw_provider_artifacts import build_raw_provider_artifacts


def _fingerprint(*values):
    digest = hashlib.sha256()
    for value in values:
        digest.update(str(value or "").encode("utf-8"))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


@dataclass
class ServicePipelineContext:
    run_id: str
    crm_job_id: int
    source_video_id: str
    video_path: str = ""
    video_hash: str = ""
    duration_ms: int = 0
    audio: object | None = None
    ocr_fusion_result: object | None = None
    asr_fusion_result: object | None = None
    cross_modal_result: object | None = None
    evidence_bundle: object | None = None
    correction_result: object | None = None


@dataclass(frozen=True)
class ServicePipelineResult:
    status: str
    current_stage: str
    accurate_status: str
    accounting: dict
    error_code: str = ""


class ServicePipelineRunner:
    def __init__(
        self, repository, runtime_root, config_fingerprint, *,
        link_stage, upload_stage, audio_stage, ocr_stage, asr_stage,
        cross_modal_stage, accurate_stage, review_stage,
    ):
        self.repository = repository
        self.runtime_root = Path(runtime_root).resolve()
        self.config_fingerprint = str(config_fingerprint)
        self.link_stage = link_stage
        self.upload_stage = upload_stage
        self.audio_stage = audio_stage
        self.ocr_stage = ocr_stage
        self.asr_stage = asr_stage
        self.cross_modal_stage = cross_modal_stage
        self.accurate_stage = accurate_stage
        self.review_stage = review_stage
        self.orchestrator = Phase2Orchestrator(repository)

    def _store(self, run):
        return PipelineStateStore(
            self.runtime_root / "jobs" / f"job_{run.crm_job_id}",
            run.crm_job_id, run.id,
        )

    def raw_provider_artifacts_for_run(self, run_id):
        run = self.repository.get_run(run_id)
        if run is None:
            return {}
        store = self._store(run)
        ocr_checkpoint = store.load_saved_stage(
            "recognizing_ocr", self.config_fingerprint,
        )
        asr_checkpoint = store.load_saved_stage(
            "fusing_asr", self.config_fingerprint,
        )
        if ocr_checkpoint is None and asr_checkpoint is None:
            return {}
        ocr = (
            decode_pipeline_value(ocr_checkpoint.payload.get("value"))
            if ocr_checkpoint is not None else None
        )
        asr = (
            decode_pipeline_value(asr_checkpoint.payload.get("value"))
            if asr_checkpoint is not None else None
        )
        return build_raw_provider_artifacts(run.id, ocr, asr)

    def _load(self, store, stage, input_fingerprint):
        checkpoint = store.load_stage(
            stage, input_fingerprint, self.config_fingerprint,
        )
        if checkpoint is None:
            return None, {}
        value = decode_pipeline_value(checkpoint.payload["value"])
        resumed = {}
        for provider, counters in checkpoint.accounting.items():
            resumed[provider] = {
                "current_run_delta": 0,
                "checkpoint_accumulated": int(counters.get("checkpoint_accumulated", 0))
                + int(counters.get("current_run_delta", 0)),
                "diagnostic_rerun_total": int(counters.get("diagnostic_rerun_total", 0)),
            }
        return value, resumed

    def _save(self, store, stage, input_fingerprint, artifact, value, accounting=None):
        store.save_stage(
            stage, input_fingerprint, self.config_fingerprint, artifact,
            {"value": encode_pipeline_value(value)}, dict(accounting or {}),
        )

    @staticmethod
    def _merge_accounting(total, update):
        for provider, counters in dict(update or {}).items():
            target = total.setdefault(provider, {
                "current_run_delta": 0, "checkpoint_accumulated": 0,
                "diagnostic_rerun_total": 0,
            })
            for key in target:
                target[key] += int(counters.get(key, 0))

    def _advance_downloaded(self, run):
        current = self.repository.get_run(run.id)
        if current.current_stage == "parsing_link":
            self.repository.transition(run.id, "resolving_platform")
            self.repository.transition(run.id, "downloading_video")
            self.repository.transition(run.id, "downloaded")
        elif current.current_stage == "ingesting_upload":
            self.repository.transition(run.id, "downloaded")

    def _advance_ocr(self, run):
        current = self.repository.get_run(run.id)
        stages = ("extracting_frames", "segmenting_subtitles", "recognizing_ocr")
        start = {
            "downloaded": 0, "extracting_frames": 1,
            "segmenting_subtitles": 2, "recognizing_ocr": 3,
        }.get(current.current_stage, 3)
        for stage in stages[start:]:
            self.repository.transition(run.id, stage)

    def _safe_fail(self, run_id, code):
        current = self.repository.get_run(run_id)
        if current and current.current_stage not in {"failed", "completed", "needs_upload"}:
            try:
                self.repository.transition(
                    run_id, "failed", error_code=code,
                    error_message="提取任务执行失败",
                )
            except Exception:
                pass

    def run(self, run_id):
        run = self.repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        accounting = {}
        accurate_status = "ACCURATE_TRANSCRIPT_INCOMPLETE"
        try:
            if run.current_stage == "created":
                run = self.orchestrator.advance_run(run.id)
            store = self._store(run)
            terminal_input = _fingerprint(
                run.file_hash, run.duration_ms, run.crm_job_id,
            )
            if run.current_stage == "completed":
                terminal, resumed = self._load(
                    store, "completed", terminal_input,
                )
                self._merge_accounting(accounting, resumed)
                if isinstance(terminal, dict):
                    candidate = str(terminal.get("accurate_status") or "")
                    if candidate in self._allowed_review_statuses():
                        accurate_status = candidate
                return ServicePipelineResult(
                    run.status, run.current_stage, accurate_status, accounting,
                )
            if run.current_stage == "needs_upload":
                return ServicePipelineResult(
                    run.status, run.current_stage, accurate_status, accounting,
                )
            context = ServicePipelineContext(
                run.id, run.crm_job_id, f"job_{run.crm_job_id}",
            )

            media_input = _fingerprint(run.raw_input, run.upload_path)
            media, resumed = self._load(store, "downloaded", media_input)
            self._merge_accounting(accounting, resumed)
            if media is None:
                current = self.repository.get_run(run.id)
                stage = self.upload_stage if current.current_stage == "ingesting_upload" else self.link_stage
                if stage is None:
                    raise RuntimeError("media_stage_unavailable")
                media = stage.run(current)
                if media is None:
                    current = self.repository.get_run(run.id)
                    return ServicePipelineResult(
                        current.status, current.current_stage, accurate_status, accounting,
                    )
                self._save(store, "downloaded", media_input, "media.json", media)
            else:
                self.repository.save_media_result(
                    run.id, media.video_path, media.file_hash, media.duration_ms,
                )
                self._advance_downloaded(run)
            context.video_path = media.video_path
            context.video_hash = media.file_hash
            context.duration_ms = media.duration_ms

            audio_input = _fingerprint(context.video_hash)
            audio, resumed = self._load(store, "audio_normalized", audio_input)
            self._merge_accounting(accounting, resumed)
            if audio is None:
                audio = self.audio_stage.run(context.video_path, context.crm_job_id)
                self._save(store, "audio_normalized", audio_input, "audio.json", audio)
            context.audio = audio

            ocr_input = _fingerprint(context.video_hash, context.duration_ms)
            ocr, resumed = self._load(store, "recognizing_ocr", ocr_input)
            self._merge_accounting(accounting, resumed)
            if ocr is None:
                ocr = self.ocr_stage.run(context)
                self._save(store, "recognizing_ocr", ocr_input, "ocr-result.json", ocr)
            else:
                self._advance_ocr(run)
            context.ocr_fusion_result = ocr.fusion_result

            current = self.repository.get_run(run.id)
            if current.current_stage == "recognizing_ocr":
                self.repository.transition(run.id, "recognizing_asr")
            asr_input = _fingerprint(context.audio.sha256)
            asr, resumed = self._load(store, "fusing_asr", asr_input)
            self._merge_accounting(accounting, resumed)
            if asr is None:
                asr = self.asr_stage.run(context)
                self._save(
                    store, "fusing_asr", asr_input, "asr-result.json", asr,
                    asr.accounting,
                )
                self._merge_accounting(accounting, asr.accounting)
            if self.repository.get_run(run.id).current_stage == "recognizing_asr":
                self.repository.transition(run.id, "fusing_asr")
            context.asr_fusion_result = asr.fusion_result

            if self.repository.get_run(run.id).current_stage == "fusing_asr":
                self.repository.transition(run.id, "fusing_cross_modal")
            cross_input = _fingerprint(ocr_input, asr_input)
            cross, resumed = self._load(store, "fusing_cross_modal", cross_input)
            self._merge_accounting(accounting, resumed)
            if cross is None:
                cross = self.cross_modal_stage.run(context)
                self._save(
                    store, "fusing_cross_modal", cross_input,
                    "cross-modal-result.json", cross,
                )
            context.cross_modal_result = cross

            if self.repository.get_run(run.id).current_stage == "fusing_cross_modal":
                self.repository.transition(run.id, "correcting_transcript")
            accurate_input = _fingerprint(cross_input)
            accurate, resumed = self._load(store, "correcting_transcript", accurate_input)
            self._merge_accounting(accounting, resumed)
            if accurate is None:
                accurate = self.accurate_stage.run(context)
                self._save(
                    store, "correcting_transcript", accurate_input,
                    "accurate-result.json", accurate, accurate.accounting,
                )
                self._merge_accounting(accounting, accurate.accounting)
            context.evidence_bundle = accurate.evidence_bundle
            context.correction_result = accurate.correction_result

            if self.repository.get_run(run.id).current_stage == "correcting_transcript":
                self.repository.transition(run.id, "preparing_review")
            review_input = _fingerprint(accurate_input)
            review, resumed = self._load(store, "preparing_review", review_input)
            self._merge_accounting(accounting, resumed)
            if review is None:
                review = self.review_stage.run(
                    context.crm_job_id, context.evidence_bundle,
                    context.correction_result, context.video_path,
                )
                self._validate_review(review)
                self._save(
                    store, "preparing_review", review_input,
                    "review-result.json", review,
                    review.accounting,
                )
                self._merge_accounting(accounting, review.accounting)
            else:
                self._validate_review(review)
            accurate_status = review.status
            if self.repository.get_run(run.id).current_stage == "preparing_review":
                current = self.repository.get_run(run.id)
                terminal_input = _fingerprint(
                    current.file_hash, current.duration_ms, current.crm_job_id,
                )
                self._save(
                    store, "completed", terminal_input, "completed.json",
                    {"accurate_status": accurate_status}, accounting,
                )
                self.repository.transition(run.id, "completed")
            current = self.repository.get_run(run.id)
            return ServicePipelineResult(
                current.status, current.current_stage, accurate_status, accounting,
            )
        except Exception as error:
            code = (
                "REVIEW_WORKLOAD_INCOMPLETE"
                if str(error) == "review_workload_incomplete"
                else "PIPELINE_STAGE_FAILED"
            )
            self._safe_fail(run.id, code)
            current = self.repository.get_run(run.id)
            return ServicePipelineResult(
                "failed", current.current_stage, accurate_status, accounting, code,
            )

    @staticmethod
    def _allowed_review_statuses():
        return {
            "TRANSCRIPT_READY",
            "ACCURATE_TRANSCRIPT_COMPLETE", "AUTO_COMPLETE", "REVIEW_REQUIRED", "EXCESSIVE_REVIEW_REQUIRED",
            "ACCURATE_TRANSCRIPT_INCOMPLETE", "HUMAN_REVIEW_COMPLETE",
        }

    @classmethod
    def _validate_review(cls, review):
        if review.status not in cls._allowed_review_statuses() or not Path(review.carrier_path).is_file():
            raise RuntimeError("review_workload_incomplete")
        if review.status in {"TRANSCRIPT_READY", "REVIEW_REQUIRED", "EXCESSIVE_REVIEW_REQUIRED"}:
            if int((review.metrics or {}).get("review_items", 0)) == 0:
                return
            if not review.queue_path or not Path(review.queue_path).is_file():
                raise RuntimeError("review_workload_incomplete")
