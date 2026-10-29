"""Read-only view models for the Mobile CRM home page."""

from dataclasses import dataclass
from typing import Callable, Iterable

from student_application_stage_distribution import (
    APPLICATION_STAGE_DEFINITIONS,
    application_stage_definitions_for_connection,
)
from workflow_projection import load_current_workflow_projection


@dataclass(frozen=True)
class ReminderSummary:
    key: str
    label: str
    count: int | None
    href: str
    state: str = "available"


@dataclass(frozen=True)
class StageSummary:
    key: str
    label: str
    count: int
    href: str


@dataclass(frozen=True)
class DocumentShortcut:
    key: str
    label: str
    href: str


@dataclass(frozen=True)
class MobileHomeData:
    reminders: tuple[ReminderSummary, ...]
    stages: tuple[StageSummary, ...]
    documents: tuple[DocumentShortcut, ...]


def load_mobile_workflow_projection(connection):
    """Thin Mobile adapter over the authoritative cycle projection."""
    return load_current_workflow_projection(connection)


def _count(db, sql: str, params: tuple = ()) -> int:
    row = db.one(sql, params)
    if not row:
        return 0
    try:
        return int(row["total"] or 0)
    except (KeyError, TypeError, IndexError):
        return int(row[0] or 0)


def _reminder(
    key: str,
    label: str,
    href: str,
    loader: Callable[[], int],
) -> ReminderSummary:
    try:
        return ReminderSummary(key, label, loader(), href)
    except Exception:
        return ReminderSummary(key, label, None, href, "unavailable")


def _reminders(db, user: dict, can_access: Callable) -> tuple[ReminderSummary, ...]:
    definitions = (
        (("parent_form",),
            "parent_forms",
            "未填写家长表单",
            "/parent-links?status=Pending",
            lambda: _count(
                db,
                """SELECT COUNT(*) AS total
                     FROM parent_form_tokens
                    WHERE submitted_at IS NULL
                      AND invalidated_at IS NULL
                      AND datetime(expires_at) > datetime('now')""",
            )),
        (("contract_management_center",),
            "contracts",
            "未签署家长合同",
            "/contracts?view=list&status=unsigned",
            lambda: _count(
                db,
                """SELECT COUNT(*) AS total
                     FROM contracts
                    WHERE contract_status IN ('Draft','Sent')
                      AND signed_at IS NULL
                      AND deleted_at IS NULL
                      AND voided_at IS NULL""",
            )),
        (("students", "school_plan_reports"),
            "school_plans",
            "未制作学校方案",
            "/students?alert=missing_school_plan",
            lambda: _count(
                db,
                """SELECT COUNT(*) AS total
                     FROM students s
                    WHERE s.deleted_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1
                            FROM school_plan_reports r
                           WHERE r.student_id=s.student_id
                             AND r.is_current=1
                             AND r.invalidated_at IS NULL
                      )""",
            )),
    )
    return tuple(
        _reminder(key, label, href, loader)
        for module_keys, key, label, href, loader in definitions
        if all(can_access(user, module_key) for module_key in module_keys)
    )


def _stage_summaries(db, load_stages: Callable) -> tuple[StageSummary, ...]:
    connection = db.connect()
    try:
        definitions = application_stage_definitions_for_connection(connection)
        students: Iterable = load_stages(connection)
    except Exception:
        return ()
    finally:
        release = getattr(db, "_release_connection", None) or getattr(db, "release", None)
        if release:
            release(connection)
    counts = {key: 0 for key, _label in definitions}
    for student in students:
        key = str(student.stage.key or "")
        if key in counts:
            counts[key] += 1
    return tuple(
        StageSummary(key, label, counts[key], f"/students?stage={key}")
        for key, label in definitions
    )


def _document_shortcuts(user: dict, can_access: Callable) -> tuple[DocumentShortcut, ...]:
    definitions = (
        ("files", "文件管理", "/file-manager", "file_manager"),
        ("templates", "模板管理", "/templates", "template_management"),
        ("content", "内容资料", "/content", "content_center"),
        ("schools", "学校资料库", "/schools/library", "school_library"),
    )
    return tuple(
        DocumentShortcut(key, label, href)
        for key, label, href, module_key in definitions
        if can_access(user, module_key)
    )


def load_mobile_home_data(db, user: dict, can_access: Callable, load_stages: Callable) -> MobileHomeData:
    return MobileHomeData(
        reminders=_reminders(db, user, can_access),
        stages=_stage_summaries(db, load_stages) if can_access(user, "students") else (),
        documents=_document_shortcuts(user, can_access),
    )
