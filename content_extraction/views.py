from dataclasses import dataclass


@dataclass(frozen=True)
class ContentExtractionViewModel:
    crm_job_id: int
    platform: str
    stage: str
    status: str
    error_message: str
    needs_upload: bool
    video_path: str


def content_extraction_pipeline_version(environ) -> str:
    value = str(
        (environ or {}).get("AAIE_CRM_CONTENT_EXTRACTION_PIPELINE", "v1")
    ).strip().lower()
    return "v2" if value == "v2" else "v1"


def accurate_transcript_enabled(environ) -> bool:
    value = str(
        (environ or {}).get("AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED", "false")
    ).strip().lower()
    return value in {"1", "true", "yes", "on"}


def effective_content_extraction_pipeline_version(environ) -> str:
    """Use the Accurate Transcript flag as authority, with legacy fallback."""
    values = environ or {}
    if "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED" in values:
        return "v2" if accurate_transcript_enabled(values) else "v1"
    return content_extraction_pipeline_version(values)


def accurate_transcript_pilot_enabled(environ) -> bool:
    return effective_content_extraction_pipeline_version(environ) == "v2"


def view_model_from_client_result(crm_job_id: int, result):
    if not result.ok:
        return ContentExtractionViewModel(
            crm_job_id=int(crm_job_id),
            platform="",
            stage="service_unavailable",
            status="service_unavailable",
            error_message="提取服务不可用，请稍后重试；CRM 其他功能不受影响。",
            needs_upload=False,
            video_path="",
        )
    payload = result.payload or {}
    stage = str(payload.get("current_stage") or payload.get("status") or "created")
    return ContentExtractionViewModel(
        crm_job_id=int(crm_job_id),
        platform=str(payload.get("platform") or ""),
        stage=stage,
        status=str(payload.get("status") or stage),
        error_message=str(payload.get("user_message") or ""),
        needs_upload=stage == "needs_upload",
        video_path=str(payload.get("video_path") or ""),
    )
