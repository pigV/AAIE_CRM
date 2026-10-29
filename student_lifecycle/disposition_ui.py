"""Business-facing adapter for canonical Lifecycle module dispositions.

The underlying disposition event and policy remain authoritative.  This
module only discovers policy-valid historical records, translates progress
into staff-facing language, and resolves staff choices without asking for
database identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import sqlite3
from typing import Mapping
from urllib.parse import quote
from uuid import uuid4

from student_lifecycle.module_dispositions import evaluate_module_policy


@dataclass(frozen=True)
class ExistingRecordCandidate:
    choice_key: str
    source_record_type: str
    source_record_id: int
    label: str


@dataclass(frozen=True)
class ResolvedBusinessDisposition:
    disposition: str
    reason: str | None
    source_record_type: str | None
    source_record_id: int | None


class BusinessDispositionSelectionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    try:
        return frozenset(
            str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')")
        )
    except sqlite3.OperationalError:
        return frozenset()


def _text(row: sqlite3.Row, key: str) -> str:
    try:
        return str(row[key] or "").strip()
    except (IndexError, KeyError):
        return ""


def _candidate(
    source_type: str, source_id: int, label: str
) -> ExistingRecordCandidate:
    return ExistingRecordCandidate(
        choice_key=f"{source_type}:{int(source_id)}",
        source_record_type=source_type,
        source_record_id=int(source_id),
        label=str(label or "").strip(),
    )


def _raw_candidates(
    connection: sqlite3.Connection, student_id: str, module_key: str
) -> list[tuple[str, ExistingRecordCandidate]]:
    found: list[tuple[str, ExistingRecordCandidate]] = []
    visa_columns = _columns(connection, "visas")
    if module_key == "visa" and visa_columns:
        def visa_field(name: str) -> str:
            return name if name in visa_columns else f"NULL AS {name}"

        rows = connection.execute(
            f"""SELECT id,{visa_field('visa_type')},{visa_field('visa_status')},
                       {visa_field('visa_grant_date')},
                       {visa_field('visa_expiry_date')},{visa_field('created_at')}
                  FROM visas WHERE student_id=? ORDER BY id DESC""",
            (student_id,),
        ).fetchall()
        for row in rows:
            grant_date = _text(row, "visa_grant_date")
            status = _text(row, "visa_status")
            visa_type = _text(row, "visa_type") or "Visa"
            headline = f"Visa Grant {grant_date}" if grant_date else (
                status or "Visa 记录"
            )
            label = f"{headline} · {visa_type}" if visa_type else headline
            sort_value = grant_date or _text(row, "created_at")
            found.append((sort_value, _candidate("visa", row["id"], label)))

    coe_columns = _columns(connection, "coe_oshc")
    if module_key == "coe_oshc" and coe_columns:
        def coe_field(name: str) -> str:
            return name if name in coe_columns else f"NULL AS {name}"

        rows = connection.execute(
            f"""SELECT id,{coe_field('oshc_provider')},
                       {coe_field('oshc_company_name')},
                       {coe_field('oshc_expiry_date')},
                       {coe_field('oshc_end_date')},{coe_field('created_at')}
                  FROM coe_oshc
                 WHERE student_id=? AND upper(document_kind)='OSHC'
                 ORDER BY id DESC""",
            (student_id,),
        ).fetchall()
        for row in rows:
            provider = (
                _text(row, "oshc_provider")
                or _text(row, "oshc_company_name")
                or "保险记录"
            )
            expiry = _text(row, "oshc_expiry_date") or _text(row, "oshc_end_date")
            label = f"OSHC · {provider}" + (f" · 至 {expiry}" if expiry else "")
            found.append((expiry or _text(row, "created_at"), _candidate("oshc", row["id"], label)))

    payment_columns = _columns(connection, "payments")
    if module_key == "payments" and "application_cycle_id" in payment_columns:
        def payment_field(name: str) -> str:
            return name if name in payment_columns else f"NULL AS {name}"

        rows = connection.execute(
            f"""SELECT id,{payment_field('payment_category')},
                       {payment_field('amount')},{payment_field('currency')},
                       {payment_field('paid_date')},{payment_field('created_at')}
                  FROM payments WHERE student_id=? ORDER BY id DESC""",
            (student_id,),
        ).fetchall()
        for row in rows:
            category = _text(row, "payment_category") or "付款记录"
            amount = row["amount"]
            currency = _text(row, "currency")
            amount_label = (
                f"{currency} {amount:g}" if amount is not None and currency
                else (f"{amount:g}" if amount is not None else "")
            )
            paid_date = _text(row, "paid_date")
            label = " · ".join(
                part for part in (category, amount_label, paid_date) if part
            )
            found.append((paid_date or _text(row, "created_at"), _candidate("payment", row["id"], label)))

    document_columns = _columns(connection, "student_documents")
    if (
        module_key == "homestay_airport"
        and "application_cycle_id" in document_columns
    ):
        def document_field(name: str) -> str:
            return name if name in document_columns else f"NULL AS {name}"

        rows = connection.execute(
            f"""SELECT id,{document_field('original_name')},
                       {document_field('document_type')},
                       {document_field('uploaded_at')}
                  FROM student_documents
                 WHERE student_id=?
                   AND folder_category='Homestay + Airport Pickup'
                 ORDER BY id DESC""",
            (student_id,),
        ).fetchall()
        for row in rows:
            name = _text(row, "original_name") or _text(row, "document_type")
            uploaded = _text(row, "uploaded_at")[:10]
            label = f"住宿 / 接机 · {name}" + (f" · {uploaded}" if uploaded else "")
            found.append((uploaded, _candidate("student_document", row["id"], label)))

    if module_key == "applications":
        if all(
            _columns(connection, table)
            for table in (
                "transfer_school_applications", "transfer_cases",
                "student_cases", "application_cycles",
            )
        ):
            rows = connection.execute(
                """SELECT application.id,application.school_name_snapshot,
                          application.application_status,application.created_at
                     FROM transfer_school_applications AS application
                     JOIN transfer_cases AS transfer
                       ON transfer.case_id=application.case_id
                     JOIN student_cases AS student_case
                       ON student_case.id=transfer.case_id
                    WHERE student_case.student_id=? ORDER BY application.id DESC""",
                (student_id,),
            ).fetchall()
            for row in rows:
                school = _text(row, "school_name_snapshot") or "学校申请"
                status = _text(row, "application_status")
                label = f"转学申请 · {school}" + (f" · {status}" if status else "")
                found.append((_text(row, "created_at"), _candidate("transfer_school_application", row["id"], label)))
        if all(
            _columns(connection, table)
            for table in (
                "progression_target_applications", "progression_cases",
                "student_cases", "application_cycles",
            )
        ):
            rows = connection.execute(
                """SELECT application.id,
                          application.institution_name_snapshot,
                          application.course_name,
                          application.application_status,
                          application.created_at
                     FROM progression_target_applications AS application
                     JOIN progression_cases AS progression
                       ON progression.case_id=application.case_id
                     JOIN student_cases AS student_case
                       ON student_case.id=progression.case_id
                    WHERE student_case.student_id=? ORDER BY application.id DESC""",
                (student_id,),
            ).fetchall()
            for row in rows:
                institution = _text(row, "institution_name_snapshot") or "院校申请"
                course = _text(row, "course_name")
                status = _text(row, "application_status")
                label = " · ".join(
                    part for part in ("升学申请", institution, course, status) if part
                )
                found.append((_text(row, "created_at"), _candidate("progression_school_application", row["id"], label)))
        if all(
            _columns(connection, table)
            for table in ("higher_education_applications", "application_cycles")
        ):
            rows = connection.execute(
                """SELECT application.id,application.course_name,
                          application.status,application.created_at
                     FROM higher_education_applications AS application
                     JOIN application_cycles AS cycle
                       ON cycle.id=application.application_cycle_id
                    WHERE cycle.student_id=? ORDER BY application.id DESC""",
                (student_id,),
            ).fetchall()
            for row in rows:
                course = _text(row, "course_name") or "高等教育申请"
                status = _text(row, "status")
                label = f"高等教育申请 · {course}" + (f" · {status}" if status else "")
                found.append((_text(row, "created_at"), _candidate("higher_education_application", row["id"], label)))
    return found


def load_existing_candidates(
    connection: sqlite3.Connection, application_cycle_id: int, module_key: str
) -> tuple[ExistingRecordCandidate, ...]:
    cycle = connection.execute(
        "SELECT student_id FROM application_cycles WHERE id=?",
        (int(application_cycle_id),),
    ).fetchone()
    if cycle is None or module_key == "offer":
        return ()
    valid: list[tuple[str, ExistingRecordCandidate]] = []
    for sort_value, candidate in _raw_candidates(
        connection, str(cycle[0]), str(module_key)
    ):
        policy = evaluate_module_policy(
            connection,
            application_cycle_id=int(application_cycle_id),
            module_key=str(module_key),
            disposition="existing",
            reason="候选记录验证",
            source_record_type=candidate.source_record_type,
            source_record_id=candidate.source_record_id,
        )
        if policy.allowed:
            valid.append((sort_value, candidate))
    if module_key == "visa":
        selected = connection.execute(
            """SELECT source_record_id
                 FROM lifecycle_module_disposition_events
                WHERE application_cycle_id=? AND module_key='visa'
                  AND disposition='existing'
                  AND source_record_type='visa'
                  AND reason LIKE '系统登记 Existing Visa：%'
                ORDER BY occurred_at DESC,id DESC LIMIT 1""",
            (int(application_cycle_id),),
        ).fetchone()
        if selected is not None:
            visa_columns = _columns(connection, "visas")
            visa_id = int(selected[0])
            row = connection.execute(
                f"""SELECT id,
                           {'visa_type' if 'visa_type' in visa_columns else 'NULL AS visa_type'},
                           {'visa_status' if 'visa_status' in visa_columns else 'NULL AS visa_status'},
                           {'visa_grant_date' if 'visa_grant_date' in visa_columns else 'NULL AS visa_grant_date'},
                           {'created_at' if 'created_at' in visa_columns else 'NULL AS created_at'}
                      FROM visas WHERE id=? AND student_id=?""",
                (visa_id, str(cycle[0])),
            ).fetchone()
            if row is not None:
                grant_date = _text(row, "visa_grant_date")
                visa_type = _text(row, "visa_type") or "Visa"
                headline = (
                    f"Visa Grant {grant_date}"
                    if grant_date else (_text(row, "visa_status") or "Visa 记录")
                )
                candidate = _candidate(
                    "visa", visa_id,
                    f"{headline} · {visa_type}",
                )
                valid.append((_text(row, "created_at") or grant_date, candidate))
    valid.sort(key=lambda item: (item[0], item[1].source_record_id), reverse=True)
    return tuple(candidate for _sort, candidate in valid)


def resolve_business_submission(
    connection: sqlite3.Connection,
    *,
    application_cycle_id: int,
    module_key: str,
    handling: str,
    existing_choice: str | None,
    reason: str | None,
) -> ResolvedBusinessDisposition:
    handling = str(handling or "").strip()
    if handling == "new":
        return ResolvedBusinessDisposition("required", None, None, None)
    if handling == "skip":
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise BusinessDispositionSelectionError("reason_required")
        return ResolvedBusinessDisposition(
            "not_applicable", clean_reason, None, None
        )
    if handling != "reuse":
        raise BusinessDispositionSelectionError("unknown_handling")

    candidates = load_existing_candidates(
        connection, int(application_cycle_id), str(module_key)
    )
    selected = None
    clean_choice = str(existing_choice or "").strip()
    if clean_choice:
        selected = next(
            (item for item in candidates if item.choice_key == clean_choice), None
        )
        if selected is None:
            raise BusinessDispositionSelectionError("existing_choice_invalid")
    elif len(candidates) == 1:
        selected = candidates[0]
    elif len(candidates) > 1:
        raise BusinessDispositionSelectionError("existing_choice_required")
    else:
        raise BusinessDispositionSelectionError("existing_source_required")
    return ResolvedBusinessDisposition(
        "existing",
        f"系统沿用：{selected.label}",
        selected.source_record_type,
        selected.source_record_id,
    )


def business_status_label(progress, source_label: str | None = None) -> str:
    state = str(progress.state)
    module_key = str(progress.module_key)
    components = set(progress.satisfied_components or ())
    if state == "blocked_invalid":
        return "处理方式需要检查"
    if state == "not_applicable_audited":
        return "无需处理"
    if state == "existing_validated":
        if module_key == "coe_oshc":
            base = (
                "沿用现有学生保险 · 等待新入学确认"
                if "coe" not in components else "已完成"
            )
        else:
            base = {
                "applications": "沿用现有申请资料",
                "visa": "沿用现有签证",
                "payments": "沿用现有付款记录",
                "homestay_airport": "沿用现有住宿 / 接机记录",
            }.get(module_key, "沿用现有记录")
        return f"{base} · {source_label}" if source_label and base != "已完成" else base
    if state == "required_satisfied":
        return "已完成"
    if module_key == "coe_oshc":
        if "oshc" in components:
            return "等待新入学确认"
        if "coe" in components:
            return "等待学生保险"
        return "等待新入学确认 / 学生保险"
    return {
        "applications": "待补充",
        "offer": "等待新录取通知",
        "visa": "需要办理",
        "payments": "待处理",
        "homestay_airport": "待处理",
    }.get(module_key, "待处理")


def render_business_disposition_control(
    module: Mapping[str, object],
    management: Mapping[str, object] | None,
    *,
    event_uid: str | None = None,
) -> str:
    if not management:
        return ""
    module_key = str(module["key"])
    candidates = tuple(module.get("existing_candidates") or ())
    current_disposition = str(module.get("disposition") or "required")
    can_reuse = bool(candidates) or current_disposition == "existing"
    can_skip = module_key in {"payments", "homestay_airport"}
    if not can_reuse and not can_skip and current_disposition == "required":
        return ""

    selected_handling = {
        "required": "new", "existing": "reuse", "not_applicable": "skip",
    }.get(current_disposition, "new")
    handling_options = [("new", "本次需要")]
    if can_reuse:
        handling_options.append(("reuse", "沿用现有"))
    if can_skip:
        handling_options.append(("skip", "无需处理"))
    if len(candidates) == 1:
        handling_options = [
            (value, f"{label} — {candidates[0].label}" if value == "reuse" else label)
            for value, label in handling_options
        ]
    options_html = "".join(
        f'<option value="{escape(value)}"'
        f'{" selected" if value == selected_handling else ""}>'
        f'{escape(label)}</option>'
        for value, label in handling_options
    )
    current_choice = (
        f'{module.get("source_record_type")}:{module.get("source_record_id")}'
        if module.get("source_record_type") and module.get("source_record_id")
        else ""
    )
    choice_html = ""
    if len(candidates) > 1:
        candidate_options = '<option value="">请选择沿用记录</option>' + "".join(
            f'<option value="{escape(candidate.choice_key)}"'
            f'{" selected" if candidate.choice_key == current_choice else ""}>'
            f'{escape(candidate.label)}</option>'
            for candidate in candidates
        )
        choice_html = (
            '<label data-existing-choice'
            f'{"" if selected_handling == "reuse" else " hidden"}>'
            f'沿用记录<select name="existing_choice">{candidate_options}</select>'
            '</label>'
        )
    reason_value = str(module.get("reason") or "") if selected_handling == "skip" else ""
    reason_html = ""
    if can_skip:
        reason_html = (
            '<label data-na-reason'
            f'{"" if selected_handling == "skip" else " hidden"}>'
            '说明<textarea name="reason" placeholder="请简要说明本次为何无需处理">'
            f'{escape(reason_value)}</textarea></label>'
        )
    student_id = quote(str(management["student_id"]))
    cycle_id = int(management["cycle_id"])
    action = (
        f"/students/{student_id}/lifecycle-cycles/{cycle_id}/modules/"
        f"{quote(module_key)}/disposition"
    )
    return (
        '<details class="lifecycle-module-business-control">'
        '<summary>调整本次处理</summary>'
        f'<form method="post" action="{action}" data-lifecycle-business-disposition>'
        f'<input type="hidden" name="csrf" value="{escape(str(management["csrf"]))}">'
        f'<input type="hidden" name="event_uid" value="{escape(event_uid or f"cycle-{cycle_id}-{module_key}-{uuid4().hex}")}">'
        f'<label>本轮处理<select name="handling" data-handling-select>{options_html}</select></label>'
        f'{choice_html}{reason_html}'
        '<button class="secondary compact" type="submit">保存</button>'
        '</form></details>'
    )
