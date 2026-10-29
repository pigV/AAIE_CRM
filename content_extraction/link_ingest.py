"""Same-run link normalization and acquisition using existing adapters."""

from dataclasses import dataclass

from .errors import ExtractionError
from .link_cleaner import normalize_shared_link
from .models import DownloadRequest, Platform
from .orchestrator import Phase2Orchestrator


@dataclass(frozen=True)
class MediaStageResult:
    video_path: str
    file_hash: str
    duration_ms: int
    platform: str
    source_url: str
    resolved_url: str


class LinkIngestStage:
    def __init__(self, repository, redirect_transport, resolver, adapter_registry):
        self.repository = repository
        self.redirect_transport = redirect_transport
        self.resolver = resolver
        self.adapter_registry = adapter_registry
        self.orchestrator = Phase2Orchestrator(repository)

    def run(self, run):
        if run.current_stage != "parsing_link":
            raise ValueError("link_ingest_requires_parsing_link")
        self.repository.transition(run.id, "resolving_platform")
        try:
            normalized = normalize_shared_link(
                run.raw_input, self.redirect_transport, self.resolver,
            )
            self.repository.save_link_result(run.id, normalized)
            self.repository.record_redirect_hops(run.id, normalized.redirect_hops)
            self.repository.transition(run.id, "downloading_video")
            adapter = self.adapter_registry.get(normalized.platform)
            media = adapter.acquire(DownloadRequest(
                run.id, run.crm_job_id, normalized.platform,
                source_url=normalized.extracted_url,
                resolved_url=normalized.resolved_url,
            ))
            self.repository.save_media_result(
                run.id, media.video_path, media.file_hash, media.duration_ms,
            )
            self.repository.transition(run.id, "downloaded")
            return MediaStageResult(
                media.video_path, media.file_hash, media.duration_ms,
                media.platform.value, media.source_url, media.resolved_url,
            )
        except ExtractionError as error:
            diagnostic = getattr(error, "redirect_failure_diagnostic", None)
            if isinstance(diagnostic, dict):
                try:
                    self.repository.record_redirect_failure(
                        run.id,
                        diagnostic,
                        getattr(error, "redirect_hops", ()),
                    )
                except Exception:
                    # Diagnostics must never replace the original safe failure path.
                    pass
            self.orchestrator.handle_acquisition_error(run.id, error)
            return None


class UploadIngestStage:
    def __init__(self, repository, upload_adapter):
        self.repository = repository
        self.upload_adapter = upload_adapter

    def run(self, run):
        if run.current_stage != "ingesting_upload":
            raise ValueError("upload_ingest_requires_ingesting_upload")
        media = self.upload_adapter.acquire(DownloadRequest(
            run.id, run.crm_job_id, Platform.LOCAL_UPLOAD,
            upload_path=run.upload_path,
        ))
        self.repository.save_media_result(
            run.id, media.video_path, media.file_hash, media.duration_ms,
        )
        self.repository.transition(run.id, "downloaded")
        return MediaStageResult(
            media.video_path, media.file_hash, media.duration_ms,
            media.platform.value, media.source_url, media.resolved_url,
        )
