"""Post-confirmation coordination for the shared Upload Review workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Literal, Mapping

import record_picker_and_uploader
from student_case_feature_flags import StudentCaseFeatureFlags


@dataclass(frozen=True)
class ConfirmedUpload:
    module: str
    document_id: int
    business_record_id: int | None = None

    def __post_init__(self):
        if not str(self.module or "").strip():
            raise ValueError("Upload Review模块不能为空")
        if type(self.document_id) is not int or self.document_id < 1:
            raise ValueError("Upload Review正式文件编号无效")
        if self.business_record_id is not None and (
            type(self.business_record_id) is not int
            or self.business_record_id < 1
        ):
            raise ValueError("Upload Review正式业务记录编号无效")


@dataclass(frozen=True)
class ConfirmationCoordinationResult:
    context_kind: Literal["none", "transfer", "progression"]
    linked: bool


def coordinate_confirmed_upload(
    database_path: str | Path,
    *,
    stored_form: Mapping[str, Any],
    confirmed_upload: ConfirmedUpload,
    actor_user_id: int,
    actor_permissions: Collection[str],
    flags: StudentCaseFeatureFlags,
) -> ConfirmationCoordinationResult:
    context = record_picker_and_uploader.upload_context_from_form(stored_form)
    if context is None:
        return ConfirmationCoordinationResult("none", False)
    if isinstance(context, record_picker_and_uploader.ProgressionUploadContext):
        record_picker_and_uploader.complete_upload_and_link(
            database_path,
            stored_form=stored_form,
            canonical_record_id=None,
            canonical_document_id=confirmed_upload.document_id,
            actor_user_id=actor_user_id,
            actor_permissions=actor_permissions,
            flags=flags,
        )
        return ConfirmationCoordinationResult("progression", True)
    if confirmed_upload.business_record_id is None:
        raise ValueError("Transfer上传未生成可关联的正式业务记录")
    record_picker_and_uploader.complete_upload_and_link(
        database_path,
        stored_form=stored_form,
        canonical_record_id=confirmed_upload.business_record_id,
        canonical_document_id=confirmed_upload.document_id,
        actor_user_id=actor_user_id,
        actor_permissions=actor_permissions,
        flags=flags,
    )
    return ConfirmationCoordinationResult("transfer", True)
