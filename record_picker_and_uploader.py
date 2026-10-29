"""Thin Transfer record selection and upload-review orchestration boundary.

This module does not save files or create canonical student records.  Those
operations remain owned by the existing Upload Review and module-specific
services.  It supplies one candidate policy and delegates Transfer link writes
to the existing case/arrangement services.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Collection, Mapping
from urllib.parse import quote

from student_case_feature_flags import StudentCaseFeatureFlags
from transfer_arrangement_service import TransferArrangementService
from transfer_case_service import TransferCaseService
from progression_case_service import ProgressionCaseService
from security import now_iso


@dataclass(frozen=True)
class RecordCandidate:
    record_type: str
    record_id: int
    student_id: str
    business_date: str
    created_at: str
    status: str
    display_name: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class TransferUploadContext:
    case_id: int
    record_type: str
    application_id: int | None = None
    payment_requirement_id: int | None = None
    return_section: str = "application"

    def form_values(self) -> dict[str, str]:
        return {
            "transfer_link_after_review": "1",
            "transfer_case_id": str(self.case_id),
            "transfer_record_type": self.record_type,
            "transfer_application_id": (
                "" if self.application_id is None else str(self.application_id)
            ),
            "transfer_payment_requirement_id": (
                ""
                if self.payment_requirement_id is None
                else str(self.payment_requirement_id)
            ),
            "transfer_return_section": self.return_section,
        }


@dataclass(frozen=True)
class ProgressionUploadContext:
    case_id: int
    document_role: str
    target_application_id: int | None = None
    return_section: str = "materials"

    def form_values(self) -> dict[str, str]:
        return {
            "progression_link_after_review": "1",
            "progression_case_id": str(self.case_id),
            "progression_document_role": self.document_role,
            "progression_target_application_id": (
                "" if self.target_application_id is None
                else str(self.target_application_id)
            ),
            "progression_return_section": self.return_section,
        }


@dataclass(frozen=True)
class AutoMatchResult:
    case_id: int
    suggestions: Mapping[str, RecordCandidate | None]


_POLICIES = {
    "offer": {
        "table": "offers",
        "kind_where": "",
        "date_columns": ("offer_date", "uploaded_at", "created_at"),
        "status_columns": ("offer_status",),
        "name_columns": ("school_name", "document_type"),
        "invalid_statuses": frozenset(
            {"cancelled", "canceled", "rejected", "withdrawn", "expired", "void"}
        ),
    },
    "coe": {
        "table": "coe_oshc",
        "kind_where": "document_kind IN ('COE','COE_CAAW')",
        "date_columns": ("coe_start_date", "created_at"),
        "status_columns": ("status",),
        "name_columns": ("school_name", "coe_provider", "document_kind"),
        "invalid_statuses": frozenset({"cancelled", "canceled", "expired", "void"}),
    },
    "oshc": {
        "table": "coe_oshc",
        "kind_where": "document_kind IN ('OSHC','OSHCReceipt')",
        "date_columns": (
            "oshc_expiry_date", "oshc_end_date", "oshc_commencement_date",
            "oshc_start_date", "created_at",
        ),
        "status_columns": ("status",),
        "name_columns": ("oshc_company_name", "oshc_provider", "document_kind"),
        "invalid_statuses": frozenset({"cancelled", "canceled", "expired", "void"}),
    },
    "visa": {
        "table": "visas",
        "kind_where": "",
        "date_columns": ("visa_expiry_date", "visa_grant_date", "created_at"),
        "status_columns": ("visa_status",),
        "name_columns": ("visa_type", "visa_sector"),
        "invalid_statuses": frozenset(
            {"cancelled", "canceled", "expired", "refused", "withdrawn", "void"}
        ),
    },
    "payment": {
        "table": "payments",
        "kind_where": "",
        "date_columns": ("paid_date", "created_at"),
        "status_columns": ("status",),
        "name_columns": ("payment_category", "receipt_file_path"),
        "invalid_statuses": frozenset(
            {"cancelled", "canceled", "refunded", "reversed", "void"}
        ),
    },
}

_TRANSFER_RETURN_SECTIONS = frozenset({"application", "payment", "follow_up"})
_PROGRESSION_DOCUMENT_ROLES = frozenset({
    "application_material", "offer", "signed_offer", "payment_receipt", "coe",
    "latest_transcript", "oshc", "visa",
})
_PROGRESSION_TARGET_ROLES = frozenset({
    "offer", "signed_offer", "payment_receipt", "coe",
})
_PROGRESSION_RETURN_SECTIONS = frozenset({
    "materials", "application", "arrangements",
})


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')")}


def _first(row: Mapping[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def _record_has_confirmed_review(
    database_path: str | Path,
    *,
    student_id: str,
    module: str,
    final_path: str,
) -> bool:
    connection = sqlite3.connect(Path(database_path).resolve())
    try:
        if not _columns(connection, "upload_review_items"):
            return False
        return connection.execute(
            """SELECT 1 FROM upload_review_items
               WHERE student_id=? AND module_key=? AND status='Confirmed'
                 AND confirmed_at IS NOT NULL AND final_file_path=?
               LIMIT 1""",
            (str(student_id), module, str(final_path)),
        ).fetchone() is not None
    finally:
        connection.close()


def list_record_candidates(
    database_path: str | Path,
    student_id: str,
    record_type: str,
) -> tuple[RecordCandidate, ...]:
    """Return valid student records in deterministic latest-business-first order."""
    policy = _POLICIES.get(str(record_type or "").strip().lower())
    if policy is None:
        raise ValueError("unsupported Transfer record type")
    connection = sqlite3.connect(Path(database_path).resolve())
    connection.row_factory = sqlite3.Row
    try:
        columns = _columns(connection, policy["table"])
        selected = ["id", "student_id"]
        for name in (
            *policy["date_columns"], *policy["status_columns"],
            *policy["name_columns"], "document_kind", "receipt_file_path",
        ):
            if name in columns and name not in selected:
                selected.append(name)
        where = "student_id=?"
        if policy["kind_where"] and "document_kind" in columns:
            where += f" AND {policy['kind_where']}"
        rows = [
            dict(row)
            for row in connection.execute(
                f"SELECT {','.join(selected)} FROM {policy['table']} WHERE {where}",
                (str(student_id),),
            )
        ]
    finally:
        connection.close()
    candidates = []
    for row in rows:
        status = _first(row, policy["status_columns"]).lower()
        if status in policy["invalid_statuses"]:
            continue
        if record_type == "payment" and "receipt_file_path" in row:
            if not str(row.get("receipt_file_path") or "").strip():
                continue
        business_date = _first(row, policy["date_columns"])
        created_at = str(row.get("created_at") or "").strip()
        candidates.append(
            RecordCandidate(
                record_type=record_type,
                record_id=int(row["id"]),
                student_id=str(row["student_id"]),
                business_date=business_date,
                created_at=created_at,
                status=status,
                display_name=_first(row, policy["name_columns"]),
                metadata=row,
            )
        )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.business_date,
                item.created_at,
                item.record_id,
            ),
            reverse=True,
        )
    )


def begin_record_upload(
    case_id: int,
    record_type: str,
    *,
    application_id: int | None = None,
    payment_requirement_id: int | None = None,
    return_section: str | None = None,
) -> TransferUploadContext:
    normalized = str(record_type or "").strip().lower()
    if normalized not in _POLICIES:
        raise ValueError("unsupported Transfer record type")
    if type(case_id) is not int or case_id < 1:
        raise ValueError("invalid Transfer Case")
    if normalized in {"offer", "coe", "payment"} and not application_id:
        raise ValueError("目标学校申请不能为空")
    if normalized == "payment" and not payment_requirement_id:
        raise ValueError("付款要求不能为空")
    section = str(return_section or "").strip().lower()
    if not section:
        section = (
            "payment" if normalized == "payment"
            else "application" if normalized in {"offer", "coe"}
            else "follow_up"
        )
    if section not in _TRANSFER_RETURN_SECTIONS:
        raise ValueError("Transfer上传返回位置无效")
    if section == "application" and not application_id:
        raise ValueError("目标学校申请不能为空")
    if section == "payment" and not payment_requirement_id:
        raise ValueError("付款要求不能为空")
    return TransferUploadContext(
        case_id=case_id,
        record_type=normalized,
        application_id=application_id,
        payment_requirement_id=payment_requirement_id,
        return_section=section,
    )


def begin_progression_upload(
    case_id: int,
    document_role: str,
    *,
    target_application_id: int | None = None,
    return_section: str | None = None,
) -> ProgressionUploadContext:
    role = str(document_role or "").strip().lower()
    if type(case_id) is not int or case_id < 1:
        raise ValueError("升学 Case 无效")
    if role not in _PROGRESSION_DOCUMENT_ROLES:
        raise ValueError("升学文件用途无效")
    if role in _PROGRESSION_TARGET_ROLES and not target_application_id:
        raise ValueError("目标学校申请不能为空")
    if role not in _PROGRESSION_TARGET_ROLES and target_application_id is not None:
        raise ValueError("该升学文件不能关联目标学校")
    section = str(return_section or "").strip().lower()
    if not section:
        section = (
            "application" if role in _PROGRESSION_TARGET_ROLES
            else "arrangements" if role in {"oshc", "visa"}
            else "materials"
        )
    if section not in _PROGRESSION_RETURN_SECTIONS:
        raise ValueError("升学上传返回位置无效")
    if section == "application" and not target_application_id:
        raise ValueError("目标学校申请不能为空")
    return ProgressionUploadContext(
        case_id=case_id,
        document_role=role,
        target_application_id=target_application_id,
        return_section=section,
    )


def upload_context_from_form(
    values: Mapping[str, Any],
) -> TransferUploadContext | ProgressionUploadContext | None:
    def optional_id(name, message):
        raw = str(values.get(name) or "").strip()
        if not raw:
            return None
        if not raw.isdigit() or int(raw) < 1:
            raise ValueError(message)
        return int(raw)

    if str(values.get("progression_link_after_review") or "") == "1":
        case_id = optional_id(
            "progression_case_id", "升学上传上下文无效"
        )
        if case_id is None:
            raise ValueError("升学上传上下文无效")
        return begin_progression_upload(
            case_id,
            str(values.get("progression_document_role") or ""),
            target_application_id=optional_id(
                "progression_target_application_id", "升学上传上下文无效"
            ),
            return_section=(
                str(values.get("progression_return_section") or "") or None
            ),
        )
    if str(values.get("transfer_link_after_review") or "") != "1":
        return None

    case_id = optional_id("transfer_case_id", "Transfer上传上下文无效")
    if case_id is None:
        raise ValueError("Transfer上传上下文无效")
    return begin_record_upload(
        case_id,
        str(values.get("transfer_record_type") or ""),
        application_id=optional_id(
            "transfer_application_id", "Transfer上传上下文无效"
        ),
        payment_requirement_id=optional_id(
            "transfer_payment_requirement_id", "Transfer上传上下文无效"
        ),
        return_section=str(values.get("transfer_return_section") or "") or None,
    )


def resolve_transfer_return_target(
    database_path: str | Path,
    student_id: str,
    context: TransferUploadContext,
) -> str:
    """Resolve an allowlisted, database-validated Transfer return location."""
    connection = sqlite3.connect(Path(database_path).resolve())
    connection.row_factory = sqlite3.Row
    try:
        case = connection.execute(
            "SELECT id,student_id,case_type FROM student_cases WHERE id=?",
            (context.case_id,),
        ).fetchone()
        if (
            case is None
            or str(case["student_id"]) != str(student_id)
            or str(case["case_type"]) != "transfer"
        ):
            raise ValueError("Transfer上传上下文与学生不匹配")
        if context.application_id is not None:
            application = connection.execute(
                """SELECT id FROM transfer_school_applications
                   WHERE id=? AND case_id=?""",
                (context.application_id, context.case_id),
            ).fetchone()
            if application is None:
                raise ValueError("Transfer目标学校申请不匹配")
        if context.payment_requirement_id is not None:
            payment = connection.execute(
                """SELECT id FROM case_payment_requirements
                   WHERE id=? AND case_id=? AND transfer_application_id=?""",
                (
                    context.payment_requirement_id,
                    context.case_id,
                    context.application_id,
                ),
            ).fetchone()
            if payment is None:
                raise ValueError("Transfer付款要求不匹配")
    finally:
        connection.close()
    base = (
        f"/students/{quote(str(student_id), safe='')}"
        f"/transfer-cases/{context.case_id}"
    )
    if context.return_section == "application":
        return f"{base}#transfer-application-{context.application_id}"
    if context.return_section == "payment":
        return f"{base}#transfer-payment-{context.payment_requirement_id}"
    if context.return_section == "follow_up":
        return f"{base}#transfer-arrangements"
    raise ValueError("Transfer上传返回位置无效")


def resolve_progression_return_target(
    database_path: str | Path,
    student_id: str,
    context: ProgressionUploadContext,
) -> str:
    connection = sqlite3.connect(Path(database_path).resolve())
    connection.row_factory = sqlite3.Row
    try:
        case = connection.execute(
            "SELECT id,student_id,case_type FROM student_cases WHERE id=?",
            (context.case_id,),
        ).fetchone()
        if (
            case is None
            or str(case["student_id"]) != str(student_id)
            or str(case["case_type"]) != "progression"
        ):
            raise ValueError("升学上传上下文与学生不匹配")
        if context.target_application_id is not None:
            application = connection.execute(
                """SELECT id FROM progression_target_applications
                   WHERE id=? AND case_id=?""",
                (context.target_application_id, context.case_id),
            ).fetchone()
            if application is None:
                raise ValueError("升学目标学校申请不匹配")
    finally:
        connection.close()
    base = (
        f"/students/{quote(str(student_id), safe='')}"
        f"/progression-cases/{context.case_id}"
    )
    if context.return_section == "application":
        return f"{base}#progression-application-{context.target_application_id}"
    if context.return_section == "arrangements":
        return f"{base}#progression-arrangements"
    if context.return_section == "materials":
        return f"{base}#progression-materials"
    raise ValueError("升学上传返回位置无效")


def resolve_upload_return_target(
    database_path: str | Path,
    student_id: str,
    context: TransferUploadContext | ProgressionUploadContext,
) -> str:
    if isinstance(context, ProgressionUploadContext):
        return resolve_progression_return_target(
            database_path, student_id, context
        )
    return resolve_transfer_return_target(database_path, student_id, context)


def select_existing_record(
    database_path: str | Path,
    *,
    context: TransferUploadContext,
    record_id: int,
    actor_user_id: int,
    actor_permissions: Collection[str],
    flags: StudentCaseFeatureFlags,
    replace: bool = False,
):
    """Validate and persist one canonical-record association.

    This deliberately delegates all writes, ownership checks, history and
    audit events to the existing Transfer services.
    """
    common = {
        "actor_user_id": int(actor_user_id),
        "actor_permissions": actor_permissions,
        "flags": flags,
    }
    if context.record_type in {"offer", "coe"}:
        service = TransferCaseService(database_path)
        method_name = (
            f"{'replace' if replace else 'attach'}_transfer_"
            f"{context.record_type}"
        )
        return getattr(service, method_name)(
            context.case_id,
            int(context.application_id),
            int(record_id),
            **common,
        )
    if context.record_type == "payment":
        candidates = {
            item.record_id: item
            for item in list_record_candidates(
                database_path,
                TransferCaseService(database_path)
                .get_transfer_case(context.case_id).student_id,
                "payment",
            )
        }
        candidate = candidates.get(int(record_id))
        if candidate is None or not _record_has_confirmed_review(
            database_path,
            student_id=candidate.student_id,
            module="Payments",
            final_path=str(candidate.metadata.get("receipt_file_path") or ""),
        ):
            raise ValueError("付款证明尚未通过Upload Review")
        service = TransferCaseService(database_path)
        method = (
            service.replace_transfer_payment_proof
            if replace
            else service.attach_transfer_payment_proof
        )
        return method(
            context.case_id,
            int(context.application_id),
            int(context.payment_requirement_id),
            record_type="payment",
            record_id=int(record_id),
            mark_paid_on_success=True,
            **common,
        )
    arrangements = TransferArrangementService(database_path)
    method_name = f"select_transfer_{context.record_type}_pending"
    return getattr(arrangements, method_name)(
        context.case_id,
        int(record_id),
        **common,
    )


def replace_record_link(database_path: str | Path, **kwargs):
    return select_existing_record(database_path, replace=True, **kwargs)


def create_payment_record_from_document(
    database_path: str | Path,
    *,
    case_id: int,
    document_id: int,
    actor_user_id: int,
) -> int:
    """Create a canonical Payment record from a reviewed formal Payment file."""
    connection = sqlite3.connect(Path(database_path).resolve())
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT sc.student_id,d.folder_category,d.document_type,
                      d.file_path,d.is_active
               FROM student_cases sc
               JOIN student_documents d ON d.student_id=sc.student_id
               WHERE sc.id=? AND sc.case_type='transfer' AND d.id=?""",
            (int(case_id), int(document_id)),
        ).fetchone()
        if (
            row is None
            or row["folder_category"] != "Payments"
            or int(row["is_active"] or 0) != 1
            or not Path(str(row["file_path"] or "")).is_file()
            or not _record_has_confirmed_review(
                database_path,
                student_id=str(row["student_id"]),
                module="Payments",
                final_path=str(row["file_path"]),
            )
        ):
            raise ValueError("付款文件尚未通过正式审核或已失效")
        timestamp = now_iso()
        record_id = connection.execute(
            """INSERT INTO payments(
                   student_id,payment_category,receipt_file_path,created_at
               ) VALUES(?,?,?,?)""",
            (
                str(row["student_id"]),
                str(row["document_type"] or "Payment Receipt"),
                str(row["file_path"]),
                timestamp,
            ),
        ).lastrowid
        connection.execute(
            """INSERT INTO case_events(
                   case_id,event_type,entity_type,entity_id,new_value_json,
                   metadata_json,actor_type,actor_user_id,created_at
               ) VALUES(?, 'transfer_payment_record_created_from_document',
                        'payment',?,?,'{}','user',?,?)""",
            (
                int(case_id),
                str(record_id),
                json.dumps(
                    {"payment_record_id": int(record_id), "document_id": int(document_id)},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                int(actor_user_id),
                timestamp,
            ),
        )
        connection.commit()
        return int(record_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def complete_upload_and_link(
    database_path: str | Path,
    *,
    stored_form: Mapping[str, Any],
    canonical_record_id: int | None,
    canonical_document_id: int | None = None,
    actor_user_id: int,
    actor_permissions: Collection[str],
    flags: StudentCaseFeatureFlags,
):
    context = upload_context_from_form(stored_form)
    if context is None:
        return None
    if isinstance(context, ProgressionUploadContext):
        if canonical_document_id is None:
            raise ValueError("升学上传缺少学生文件编号")
        service = ProgressionCaseService(database_path)
        status = (
            "verified"
            if context.document_role == "latest_transcript"
            else "confirmed"
        )
        result = service.link_progression_document(
            context.case_id,
            document_role=context.document_role,
            student_document_id=int(canonical_document_id),
            target_application_id=context.target_application_id,
            status=status,
            notes="Upload Review 已确认",
            actor_user_id=actor_user_id,
            actor_permissions=actor_permissions,
            flags=flags,
        )
        if context.document_role in {"oshc", "visa"}:
            service.set_progression_arrangement(
                context.case_id,
                arrangement_type=context.document_role,
                status="confirmed",
                reason=None,
                notes="Upload Review 已确认",
                actor_user_id=actor_user_id,
                actor_permissions=actor_permissions,
                flags=flags,
        )
        return result
    if type(canonical_record_id) is not int or canonical_record_id < 1:
        raise ValueError("Transfer上传缺少正式业务记录编号")
    return select_existing_record(
        database_path,
        context=context,
        record_id=int(canonical_record_id),
        actor_user_id=actor_user_id,
        actor_permissions=actor_permissions,
        flags=flags,
        replace=True,
    )


def auto_match_student_records(
    database_path: str | Path,
    *,
    case_id: int,
    student_id: str,
    actor_user_id: int | None = None,
) -> AutoMatchResult:
    """Discover deterministic candidates without confirming them.

    Suggestions intentionally remain read-only until an employee selects one;
    this preserves the mandatory human-confirmation boundary.
    """
    suggestions = {
        record_type: (candidates[0] if candidates else None)
        for record_type in _POLICIES
        for candidates in (
            list_record_candidates(database_path, student_id, record_type),
        )
    }
    result = AutoMatchResult(case_id=int(case_id), suggestions=suggestions)
    if actor_user_id is not None:
        connection = sqlite3.connect(Path(database_path).resolve())
        try:
            row = connection.execute(
                "SELECT student_id FROM student_cases WHERE id=? AND case_type='transfer'",
                (int(case_id),),
            ).fetchone()
            if row is None or str(row[0]) != str(student_id):
                raise ValueError("Transfer Case与学生不匹配")
            payload = {
                key: (
                    None
                    if value is None
                    else {
                        "record_id": value.record_id,
                        "status": "pending_human_confirmation",
                    }
                )
                for key, value in suggestions.items()
            }
            connection.execute(
                """INSERT INTO case_events(
                       case_id,event_type,entity_type,entity_id,
                       new_value_json,metadata_json,actor_type,actor_user_id,
                       created_at
                   ) VALUES(?, 'transfer_record_candidates_suggested',
                            'transfer_case',?,?,?,'user',?,?)""",
                (
                    int(case_id),
                    str(case_id),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        {"confirmation_required": True},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    int(actor_user_id),
                    now_iso(),
                ),
            )
            connection.commit()
        finally:
            connection.close()
    return result
