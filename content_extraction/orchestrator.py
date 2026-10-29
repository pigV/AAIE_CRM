from .repository import ContentExtractionRepository, InvalidTransition
from .errors import ExtractionErrorCode


class Phase2Orchestrator:
    def __init__(self, repository: ContentExtractionRepository):
        self.repository = repository

    def create_run(self, crm_job_id: int, raw_input: str, upload_path: str = ""):
        return self.repository.create_run(crm_job_id, raw_input, upload_path)

    def advance_run(self, run_id: str):
        run = self.repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.current_stage != "created":
            raise InvalidTransition(
                f"advance_run requires created stage, got {run.current_stage}"
            )
        next_stage = "ingesting_upload" if run.upload_path else "parsing_link"
        return self.repository.transition(run.id, next_stage)

    def retry_run(self, run_id: str):
        run = self.repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.current_stage not in {"failed", "needs_upload"}:
            raise InvalidTransition(
                f"retry_run requires failed or needs_upload stage, got {run.current_stage}"
            )
        next_stage = "ingesting_upload" if run.upload_path else "parsing_link"
        return self.repository.transition(run.id, next_stage)

    def handle_acquisition_error(self, run_id: str, error):
        fallback_codes = {
            ExtractionErrorCode.LOGIN_EXPIRED,
            ExtractionErrorCode.VIDEO_RESOLVE_FAILED,
            ExtractionErrorCode.VIDEO_DOWNLOAD_FAILED,
            ExtractionErrorCode.VIDEO_UNAVAILABLE,
        }
        next_stage = "needs_upload" if error.code in fallback_codes else "failed"
        return self.repository.transition(
            run_id,
            next_stage,
            error_code=error.code.value,
            error_message=error.user_message,
        )
