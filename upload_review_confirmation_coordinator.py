"""Post-confirmation coordination for the shared Upload Review workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Literal, Mapping

import record_picker_and_uploader
from student_case_feature_flags import StudentCaseFeatureFlags


def pending_review_files(connection, metadata):
    """Return unresolved entries, preserving their original manifest indexes.

    This is a read-only projection; never rewrite a sealed manifest after a
    partial confirmation. Keep the original manifest for command verification.
    """
    pending=[]
    for item in metadata['files']:
        row=connection.execute('''SELECT status FROM upload_review_items
            WHERE id=? AND upload_session_id=? AND student_id=? AND module_key=?''',
            (item.get('item_id'),metadata.get('session_id'),str(metadata['student_id']),metadata['module'])).fetchone()
        if row is None:
            raise ValueError('材料审核清单与原记录不匹配，请核对原上传。')
        if row[0]=='NeedsReview':pending.append(item)
        elif row[0] not in {'Confirmed','Cancelled'}:
            raise ValueError('材料审核状态未知，请联系管理员核对。')
    return pending


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
    caller_transaction=None,
) -> ConfirmationCoordinationResult:
    context = record_picker_and_uploader.upload_context_from_form(stored_form)
    if context is None:
        return ConfirmationCoordinationResult("none", False)
    progression = isinstance(context, record_picker_and_uploader.ProgressionUploadContext)
    if not progression and confirmed_upload.business_record_id is None:
        raise ValueError("Transfer上传未生成可关联的正式业务记录")
    def coordinate(transaction):
        transaction_options = {'caller_transaction': transaction} if transaction is not None else {}
        record_picker_and_uploader.complete_upload_and_link(
            database_path,
            stored_form=stored_form,
            canonical_record_id=None if progression else confirmed_upload.business_record_id,
            canonical_document_id=confirmed_upload.document_id,
            actor_user_id=actor_user_id,
            actor_permissions=actor_permissions,
            flags=flags,
            **transaction_options,
        )
        return ConfirmationCoordinationResult('progression' if progression else 'transfer', True)
    if caller_transaction is None:
        return coordinate(None)
    # A coordinator may call more than one retained service (document + OSHC
    # arrangement). Its own savepoint prevents a caught second failure from
    # committing the first service's link inside the caller's transaction.
    from business_changes.service_connection import ServiceConnection
    with ServiceConnection(caller_transaction, database_path) as transaction:
        transaction.execute('BEGIN IMMEDIATE')
        return coordinate(transaction)
