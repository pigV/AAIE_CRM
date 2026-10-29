"""Read-only presentation contract for Student Lifecycle UI consumers."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import sqlite3
from typing import Iterable

from student_workflow_projection import load_student_workflow_projection_map

from .enrolment import get_student_enrolment_context
from .queries import list_student_lifecycles


LIFECYCLE_LABELS = {
    "initial": "首次申请",
    "transfer": "转学",
    "progression": "升学",
}

STATUS_LABELS = {
    "active": "进行中",
    "closed": "已关闭",
    "draft": "草稿",
    "completed": "已完成",
    "cancelled": "已取消",
    "failed": "失败",
    "archived": "已归档",
}

RECORD_CONTEXT_LABELS = {
    "CURRENT": "当前业务",
    "HISTORICAL": "历史业务",
    "PENDING": "待建立正式周期",
    "LEGACY_UNCONFIRMED": "历史记录 · 办理状态待确认",
}


def _display_business_text(value: str | None, fallback: str = "待确认") -> str:
    """Translate presentation text without changing stored workflow values."""
    text = str(value or "").strip()
    if not text:
        return fallback
    for source, target in (
        ("等待 Offer", "等待录取通知"),
        ("等待 COE", "等待入学确认"),
        ("Offer", "录取通知"),
        ("COE", "入学确认"),
        ("OSHC", "学生保险"),
        ("Payment", "付款"),
        ("Visa", "签证"),
        ("VISA", "签证"),
    ):
        text = text.replace(source, target)
    return text


@dataclass(frozen=True)
class LifecycleViewItem:
    student_id: str
    lifecycle_type: str
    case_id: int | None
    case_status: str | None
    cycle_id: int | None
    cycle_uid: str | None
    cycle_sequence: int | None
    cycle_status: str | None
    cycle_outcome: str | None
    opened_at: str | None
    closed_at: str | None
    workflow_stage: str | None = None
    target_context: str | None = None
    href: str | None = None


@dataclass(frozen=True)
class StudentLifecycleView:
    student_id: str
    current: LifecycleViewItem | None
    drafts: tuple[LifecycleViewItem, ...]
    history: tuple[LifecycleViewItem, ...]
    current_school: dict | None
    upcoming_school: dict | None
    education_history: tuple[dict, ...]


@dataclass(frozen=True)
class RecordLifecycleContext:
    record_type: str
    record_id: int
    lifecycle_type: str | None = None
    cycle_id: int | None = None
    cycle_uid: str | None = None
    cycle_sequence: int | None = None
    cycle_status: str | None = None
    case_id: int | None = None
    record_context: str = "LEGACY_UNCONFIRMED"
    target_context: str | None = None
    component_order: int | None = None
    component_role: str | None = None
    document_role: str | None = None


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')")}


def _target_for_case(connection: sqlite3.Connection, case_id: int | None) -> str | None:
    if case_id is None:
        return None
    case = connection.execute(
        "SELECT case_type FROM student_cases WHERE id=?", (int(case_id),)
    ).fetchone()
    if case is None:
        return None
    if str(case[0]) == "progression" and _table_exists(connection, "progression_target_applications"):
        row = connection.execute(
            """SELECT institution_name_snapshot,course_name
                 FROM progression_target_applications
                WHERE case_id=? ORDER BY is_final DESC,id LIMIT 1""",
            (int(case_id),),
        ).fetchone()
        if row:
            return " · ".join(str(value) for value in row if value)
    if str(case[0]) == "transfer" and _table_exists(connection, "transfer_school_applications"):
        row = connection.execute(
            """SELECT school_name_snapshot FROM transfer_school_applications
                WHERE case_id=? ORDER BY is_final DESC,preference_order,id LIMIT 1""",
            (int(case_id),),
        ).fetchone()
        if row:
            return str(row[0])
    return None


def _targets_for_cases(
    connection: sqlite3.Connection, case_ids: Iterable[int]
) -> dict[int, str]:
    ids = tuple(dict.fromkeys(int(value) for value in case_ids))
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    case_types = {
        int(row[0]): str(row[1])
        for row in connection.execute(
            f"SELECT id,case_type FROM student_cases WHERE id IN ({marks})", ids
        ).fetchall()
    }
    targets: dict[int, str] = {}
    progression_ids = tuple(
        case_id for case_id in ids if case_types.get(case_id) == "progression"
    )
    if progression_ids and _table_exists(connection, "progression_target_applications"):
        target_marks = ",".join("?" for _ in progression_ids)
        for row in connection.execute(
            f"""SELECT case_id,institution_name_snapshot,course_name
                    FROM progression_target_applications
                   WHERE case_id IN ({target_marks})
                   ORDER BY case_id,is_final DESC,id""",
            progression_ids,
        ).fetchall():
            targets.setdefault(
                int(row[0]), " · ".join(str(value) for value in row[1:] if value)
            )
    transfer_ids = tuple(
        case_id for case_id in ids if case_types.get(case_id) == "transfer"
    )
    if transfer_ids and _table_exists(connection, "transfer_school_applications"):
        target_marks = ",".join("?" for _ in transfer_ids)
        for row in connection.execute(
            f"""SELECT case_id,school_name_snapshot
                    FROM transfer_school_applications
                   WHERE case_id IN ({target_marks})
                   ORDER BY case_id,is_final DESC,preference_order,id""",
            transfer_ids,
        ).fetchall():
            targets.setdefault(int(row[0]), str(row[1]))
    return targets


def load_student_lifecycle_view(
    connection: sqlite3.Connection,
    student_id: str,
    *,
    workflow_projection=None,
) -> StudentLifecycleView:
    projection = workflow_projection
    if projection is None:
        projection = load_student_workflow_projection_map(
            connection, (student_id,)
        ).get(student_id)
    results = list_student_lifecycles(connection, student_id)
    case_ids = tuple(result.case_id for result in results if result.case_id is not None)
    targets = _targets_for_cases(connection, case_ids)
    created_at = {}
    if case_ids:
        marks = ",".join("?" for _ in case_ids)
        created_at = {
            int(row[0]): str(row[1]) if row[1] else None
            for row in connection.execute(
                f"SELECT id,created_at FROM student_cases WHERE id IN ({marks})",
                case_ids,
            ).fetchall()
        }
    items = []
    for result in results:
        opened_at = result.opened_at
        if opened_at is None and result.case_id is not None:
            opened_at = created_at.get(int(result.case_id))
        workflow_stage = None
        if (
            projection is not None
            and projection.state == "active"
            and result.cycle_id == projection.cycle_id
        ):
            workflow_stage = projection.stage_label
        items.append(LifecycleViewItem(
            student_id=result.student_id,
            lifecycle_type=result.lifecycle_type,
            case_id=result.case_id,
            case_status=result.case_status,
            cycle_id=result.cycle_id,
            cycle_uid=result.cycle_uid,
            cycle_sequence=result.cycle_sequence_no,
            cycle_status=result.cycle_status,
            cycle_outcome=result.cycle_outcome,
            opened_at=opened_at,
            closed_at=result.closed_at,
            workflow_stage=workflow_stage,
            target_context=targets.get(int(result.case_id)) if result.case_id else None,
            href=(
                f"/students/{student_id}/{result.lifecycle_type}-cases/{result.case_id}"
                if result.case_id is not None
                and result.lifecycle_type in {"transfer", "progression"}
                else None
            ),
        ))
    current = next((item for item in items if item.cycle_status == "active"), None)
    drafts = tuple(item for item in items if item.cycle_id is None and item.case_status == "draft")
    enrolments = get_student_enrolment_context(connection, student_id)
    history = tuple(sorted(
        items,
        key=lambda item: (item.opened_at or "", item.cycle_sequence or 10**9, item.case_id or 0),
        reverse=True,
    ))
    return StudentLifecycleView(
        student_id=student_id,
        current=current,
        drafts=drafts,
        history=history,
        current_school=enrolments["current"],
        upcoming_school=enrolments["upcoming"],
        education_history=tuple(enrolments["history"]),
    )


def _base_record_context(
    connection: sqlite3.Connection, record_type: str, record_id: int,
    *, student_id: str, cycle_id: int | None = None, case_id: int | None = None,
    target_context: str | None = None, component_order: int | None = None,
    component_role: str | None = None, document_role: str | None = None,
) -> RecordLifecycleContext:
    if cycle_id is None and case_id is None:
        return RecordLifecycleContext(record_type, record_id)
    cycle = None
    if cycle_id is not None:
        cycle = connection.execute(
            """SELECT id,cycle_uid,cycle_type,sequence_no,status,source_student_case_id
                 FROM application_cycles WHERE id=? AND student_id=?""",
            (int(cycle_id), student_id),
        ).fetchone()
        if cycle is None:
            return RecordLifecycleContext(record_type, record_id)
    elif case_id is not None:
        cycle = connection.execute(
            """SELECT id,cycle_uid,cycle_type,sequence_no,status,source_student_case_id
                 FROM application_cycles
                WHERE source_student_case_id=? AND student_id=?""",
            (int(case_id), student_id),
        ).fetchone()
    if cycle is None:
        case_type = connection.execute(
            "SELECT case_type FROM student_cases WHERE id=? AND student_id=?",
            (int(case_id), student_id),
        ).fetchone() if case_id is not None else None
        if case_type is None:
            return RecordLifecycleContext(record_type, record_id)
        return RecordLifecycleContext(
            record_type, record_id,
            lifecycle_type=str(case_type[0]) if case_type else None,
            case_id=case_id, record_context="PENDING",
            target_context=target_context, component_order=component_order,
            component_role=component_role, document_role=document_role,
        )
    return RecordLifecycleContext(
        record_type, record_id, lifecycle_type=str(cycle[2]),
        cycle_id=int(cycle[0]), cycle_uid=str(cycle[1]),
        cycle_sequence=int(cycle[3]), cycle_status=str(cycle[4]),
        case_id=int(cycle[5]) if cycle[5] is not None else case_id,
        record_context="CURRENT" if str(cycle[4]) == "active" else "HISTORICAL",
        target_context=target_context, component_order=component_order,
        component_role=component_role, document_role=document_role,
    )


def _progression_record_link(
    connection: sqlite3.Connection, record_type: str, record_id: int
) -> dict | None:
    if not _table_exists(connection, "progression_target_applications"):
        return None
    if record_type == "offer" and _table_exists(connection, "progression_case_offers"):
        columns = _columns(connection, "progression_case_offers")
        case_column = "case_id" if "case_id" in columns else "progression_case_id"
        target_sql = "offer.target_application_id" if "target_application_id" in columns else "NULL"
        row = connection.execute(
            f"""SELECT offer.{case_column},{target_sql},target.institution_name_snapshot,
                       target.course_name,NULL,NULL
                  FROM progression_case_offers offer
                  LEFT JOIN progression_target_applications target ON target.id={target_sql}
                 WHERE offer.existing_offer_id=? ORDER BY offer.id DESC LIMIT 1""",
            (record_id,),
        ).fetchone()
    elif record_type == "coe" and _table_exists(connection, "progression_pathway_components"):
        component_columns = _columns(connection, "progression_pathway_components")
        oshc_match = " OR component.oshc_record_id=?" if "oshc_record_id" in component_columns else ""
        parameters = (record_id, record_id) if oshc_match else (record_id,)
        row = connection.execute(
            f"""SELECT target.case_id,target.id,target.institution_name_snapshot,
                      target.course_name,component.component_order,component.component_role
                 FROM progression_pathway_components component
                 JOIN progression_target_applications target
                   ON target.id=component.progression_target_application_id
                WHERE component.coe_record_id=?{oshc_match}
                ORDER BY component.id DESC LIMIT 1""", parameters
        ).fetchone()
    elif record_type == "document" and _table_exists(connection, "progression_document_links"):
        row = connection.execute(
            """SELECT link.case_id,target.id,target.institution_name_snapshot,
                      target.course_name,NULL,NULL,link.document_role
                 FROM progression_document_links link
                 LEFT JOIN progression_target_applications target
                   ON target.id=link.target_application_id
                WHERE link.student_document_id=?
                ORDER BY link.is_current DESC,link.id DESC LIMIT 1""", (record_id,)
        ).fetchone()
    else:
        return None
    if row is None:
        return None
    return {
        "case_id": int(row[0]),
        "target_context": " · ".join(str(value) for value in (row[2], row[3]) if value),
        "component_order": int(row[4]) if row[4] is not None else None,
        "component_role": str(row[5]) if row[5] is not None else None,
        "document_role": str(row[6]) if len(row) > 6 and row[6] is not None else None,
    }


def _transfer_record_link(
    connection: sqlite3.Connection, record_type: str, record_id: int
) -> dict | None:
    if record_type == "document" and _table_exists(connection, "case_material_items"):
        material = connection.execute(
            """SELECT material.case_id,material.material_key
                 FROM case_material_items material
                 JOIN student_cases cases ON cases.id=material.case_id
                WHERE material.document_id=? AND cases.case_type='transfer'
                ORDER BY material.id DESC LIMIT 1""", (record_id,)
        ).fetchone()
        if material is not None:
            return {
                "case_id": int(material[0]),
                "target_context": _target_for_case(connection, int(material[0])),
                "document_role": str(material[1]),
            }
    if not _table_exists(connection, "case_record_links"):
        return None
    mapped_type = "student_document" if record_type == "document" else record_type
    row = connection.execute(
        """SELECT link.case_id,application.school_name_snapshot,link.link_role
             FROM case_record_links link
             LEFT JOIN transfer_school_applications application
               ON application.id=link.transfer_application_id
            WHERE link.record_type=? AND link.record_id=?
            ORDER BY CASE link.validity_status WHEN 'current' THEN 0 ELSE 1 END,
                     link.id DESC LIMIT 1""", (mapped_type, record_id)
    ).fetchone()
    if row is None:
        return None
    return {
        "case_id": int(row[0]), "target_context": str(row[1]) if row[1] else None,
        "document_role": str(row[2]) if record_type == "document" and row[2] else None,
    }


def load_record_contexts(
    connection: sqlite3.Connection, student_id: str, record_type: str,
    record_ids: Iterable[int],
) -> dict[int, RecordLifecycleContext]:
    ids = tuple(dict.fromkeys(int(value) for value in record_ids))
    if record_type not in {"offer", "coe", "contract", "document"}:
        raise ValueError("unsupported lifecycle record type")
    table = {"offer": "offers", "coe": "coe_oshc", "contract": "contracts", "document": "student_documents"}[record_type]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    columns = _columns(connection, table)
    cycle_select = "application_cycle_id" if "application_cycle_id" in columns else "NULL"
    record_rows = connection.execute(
        f"SELECT id,{cycle_select} FROM {table} WHERE student_id=? AND id IN ({marks})",
        (student_id, *ids),
    ).fetchall()
    present_ids = tuple(int(row[0]) for row in record_rows)
    contexts = {
        record_id: RecordLifecycleContext(record_type, record_id)
        for record_id in present_ids
    }
    direct_cycle = {
        int(row[0]): int(row[1])
        for row in record_rows if row[1] is not None
    }
    links: dict[int, dict[str, object]] = {}
    invalid_payment_documents: set[int] = set()
    if present_ids:
        record_marks = ",".join("?" for _ in present_ids)
        if record_type == "offer" and _table_exists(connection, "progression_case_offers"):
            offer_columns = _columns(connection, "progression_case_offers")
            target_column = (
                "offer.target_application_id"
                if "target_application_id" in offer_columns
                else "NULL"
            )
            case_column = "case_id" if "case_id" in offer_columns else "progression_case_id"
            for row in connection.execute(
                f"""SELECT offer.existing_offer_id,offer.{case_column},
                           target.institution_name_snapshot,target.course_name
                      FROM progression_case_offers AS offer
                      LEFT JOIN progression_target_applications AS target
                        ON target.id={target_column}
                     WHERE offer.existing_offer_id IN ({record_marks})
                     ORDER BY offer.id""",
                present_ids,
            ).fetchall():
                links[int(row[0])] = {
                    "case_id": int(row[1]),
                    "target_context": " · ".join(str(value) for value in row[2:] if value),
                }
        elif record_type == "coe" and _table_exists(connection, "progression_pathway_components"):
            component_columns = _columns(connection, "progression_pathway_components")
            oshc = " OR component.oshc_record_id IN ({0})".format(record_marks) if "oshc_record_id" in component_columns else ""
            parameters = present_ids + present_ids if oshc else present_ids
            record_expression = (
                "COALESCE(component.coe_record_id,component.oshc_record_id)"
                if "oshc_record_id" in component_columns
                else "component.coe_record_id"
            )
            for row in connection.execute(
                f"""SELECT {record_expression},
                           target.case_id,target.institution_name_snapshot,target.course_name,
                           component.component_order,component.component_role
                      FROM progression_pathway_components AS component
                      JOIN progression_target_applications AS target
                        ON target.id=component.progression_target_application_id
                     WHERE component.coe_record_id IN ({record_marks}){oshc}
                     ORDER BY component.id""",
                parameters,
            ).fetchall():
                links[int(row[0])] = {
                    "case_id": int(row[1]),
                    "target_context": " · ".join(str(value) for value in row[2:4] if value),
                    "component_order": int(row[4]) if row[4] is not None else None,
                    "component_role": str(row[5]) if row[5] is not None else None,
                }
        elif record_type == "document" and _table_exists(connection, "progression_document_links"):
            for row in connection.execute(
                f"""SELECT link.student_document_id,link.case_id,
                           target.institution_name_snapshot,target.course_name,
                           link.document_role
                      FROM progression_document_links AS link
                      LEFT JOIN progression_target_applications AS target
                        ON target.id=link.target_application_id
                     WHERE link.student_document_id IN ({record_marks})
                     ORDER BY link.is_current,link.id""",
                present_ids,
            ).fetchall():
                links[int(row[0])] = {
                    "case_id": int(row[1]),
                    "target_context": " · ".join(str(value) for value in row[2:4] if value),
                    "document_role": str(row[4]) if row[4] is not None else None,
                }
        if _table_exists(connection, "case_record_links"):
            mapped_type = "student_document" if record_type == "document" else record_type
            for row in connection.execute(
                f"""SELECT link.record_id,link.case_id,application.school_name_snapshot,
                           link.link_role
                      FROM case_record_links AS link
                      LEFT JOIN transfer_school_applications AS application
                        ON application.id=link.transfer_application_id
                     WHERE link.record_type=? AND link.record_id IN ({record_marks})
                     ORDER BY link.id""",
                (mapped_type, *present_ids),
            ).fetchall():
                links.setdefault(int(row[0]), {
                    "case_id": int(row[1]),
                    "target_context": str(row[2]) if row[2] else None,
                    "document_role": str(row[3]) if record_type == "document" and row[3] else None,
                })
            if (
                record_type == "document"
                and _table_exists(connection, "payments")
                and _table_exists(connection, "transfer_school_applications")
                and {"document_id", "transfer_application_id"}.issubset(
                    _columns(connection, "case_record_links")
                )
                and {"case_id", "application_cycle_id"}.issubset(
                    _columns(connection, "transfer_school_applications")
                )
            ):
                link_columns = _columns(connection, "case_record_links")
                validity_select = (
                    "link.validity_status"
                    if "validity_status" in link_columns else "NULL"
                )
                payment_claims: dict[int, list[sqlite3.Row]] = {}
                for row in connection.execute(
                    f"""SELECT link.document_id,link.case_id,payment.student_id,
                               cases.student_id,cases.case_type,application.case_id,
                               application.application_cycle_id,cycle.student_id,
                               cycle.source_student_case_id,
                               application.school_name_snapshot,link.link_role,
                               {validity_select}
                          FROM case_record_links AS link
                          LEFT JOIN payments AS payment ON payment.id=link.record_id
                          LEFT JOIN student_cases AS cases ON cases.id=link.case_id
                          LEFT JOIN transfer_school_applications AS application
                            ON application.id=link.transfer_application_id
                          LEFT JOIN application_cycles AS cycle
                            ON cycle.id=application.application_cycle_id
                         WHERE link.record_type='payment'
                           AND link.document_id IN ({record_marks})
                         ORDER BY link.id""",
                    present_ids,
                ).fetchall():
                    payment_claims.setdefault(int(row[0]), []).append(row)
                for record_id, claims in payment_claims.items():
                    if len(claims) != 1:
                        links.pop(record_id, None)
                        direct_cycle.pop(record_id, None)
                        invalid_payment_documents.add(record_id)
                        continue
                    claim = claims[0]
                    if not (
                        claim[1] is not None
                        and claim[2] == claim[3] == claim[7] == student_id
                        and claim[4] == "transfer"
                        and claim[5] == claim[8] == claim[1]
                        and claim[6] is not None
                        and (
                            (
                                "validity_status" not in link_columns
                                and claim[10] == "current"
                            )
                            or claim[11] in ("current", "carried_forward")
                        )
                        and (
                            record_id not in direct_cycle
                            or direct_cycle[record_id] == int(claim[6])
                        )
                    ):
                        links.pop(record_id, None)
                        direct_cycle.pop(record_id, None)
                        invalid_payment_documents.add(record_id)
                        continue
                    prior = links.get(record_id)
                    if prior is not None and prior.get("case_id") != int(claim[1]):
                        links.pop(record_id, None)
                        direct_cycle.pop(record_id, None)
                        invalid_payment_documents.add(record_id)
                        continue
                    links.setdefault(record_id, {
                        "case_id": int(claim[1]),
                        "target_context": str(claim[9]) if claim[9] else None,
                        "document_role": str(claim[10]) if claim[10] else None,
                    })
                    direct_cycle[record_id] = int(claim[6])
        if record_type == "document" and _table_exists(connection, "case_material_items"):
            for row in connection.execute(
                f"""SELECT material.document_id,material.case_id,material.material_key
                      FROM case_material_items AS material
                      JOIN student_cases AS cases ON cases.id=material.case_id
                     WHERE material.document_id IN ({record_marks})
                       AND cases.case_type='transfer' ORDER BY material.id""",
                present_ids,
            ).fetchall():
                links.setdefault(int(row[0]), {
                    "case_id": int(row[1]), "document_role": str(row[2]),
                })

    case_ids = tuple(dict.fromkeys(
        int(link["case_id"]) for link in links.values() if link.get("case_id") is not None
    ))
    case_rows = {}
    cycle_by_case = {}
    if case_ids:
        case_marks = ",".join("?" for _ in case_ids)
        case_rows = {
            int(row[0]): str(row[1])
            for row in connection.execute(
                f"SELECT id,case_type FROM student_cases WHERE student_id=? AND id IN ({case_marks})",
                (student_id, *case_ids),
            ).fetchall()
        }
        cycle_by_case = {
            int(row[5]): row
            for row in connection.execute(
                f"""SELECT id,cycle_uid,cycle_type,sequence_no,status,source_student_case_id
                       FROM application_cycles
                      WHERE student_id=? AND source_student_case_id IN ({case_marks})""",
                (student_id, *case_ids),
            ).fetchall()
        }
    cycle_ids = tuple(dict.fromkeys(direct_cycle.values()))
    cycle_rows = {}
    if cycle_ids:
        cycle_marks = ",".join("?" for _ in cycle_ids)
        cycle_rows = {
            int(row[0]): row
            for row in connection.execute(
                f"""SELECT id,cycle_uid,cycle_type,sequence_no,status,source_student_case_id
                       FROM application_cycles WHERE student_id=? AND id IN ({cycle_marks})""",
                (student_id, *cycle_ids),
            ).fetchall()
        }
    for record_id in present_ids:
        if record_id in invalid_payment_documents:
            continue
        link = links.get(record_id, {})
        case_id = int(link["case_id"]) if link.get("case_id") is not None else None
        if record_id in direct_cycle:
            cycle = cycle_rows.get(direct_cycle[record_id])
            if cycle is None:
                # An explicit cross-student or missing Cycle relation is invalid.
                # Never replace it with a Case-derived guess.
                continue
        else:
            cycle = cycle_by_case.get(case_id)
        if cycle is None:
            if case_id is None or case_id not in case_rows:
                continue
            contexts[record_id] = RecordLifecycleContext(
                record_type, record_id, lifecycle_type=case_rows[case_id],
                case_id=case_id, record_context="PENDING",
                target_context=link.get("target_context"),
                component_order=link.get("component_order"),
                component_role=link.get("component_role"),
                document_role=link.get("document_role"),
            )
            continue
        contexts[record_id] = RecordLifecycleContext(
            record_type, record_id, lifecycle_type=str(cycle[2]),
            cycle_id=int(cycle[0]), cycle_uid=str(cycle[1]),
            cycle_sequence=int(cycle[3]), cycle_status=str(cycle[4]),
            case_id=int(cycle[5]) if cycle[5] is not None else case_id,
            record_context="CURRENT" if str(cycle[4]) == "active" else "HISTORICAL",
            target_context=link.get("target_context"),
            component_order=link.get("component_order"),
            component_role=link.get("component_role"),
            document_role=link.get("document_role"),
        )
    return contexts


def render_record_context(context: RecordLifecycleContext | None) -> str:
    if context is None or context.record_context == "LEGACY_UNCONFIRMED":
        return "历史记录 · 办理状态待确认"
    label = LIFECYCLE_LABELS.get(context.lifecycle_type or "", "学生业务")
    parts = [label]
    if context.cycle_sequence is not None:
        parts.append(f"业务周期 #{context.cycle_sequence}")
    if context.case_id is not None:
        parts.append(f"记录 #{context.case_id}")
    if context.target_context:
        parts.append(f"目标：{context.target_context}")
    if context.component_order is not None:
        parts.append(f"课程组成 #{context.component_order}")
    if context.component_role:
        parts.append({"main": "主课程", "package": "组合课程"}.get(context.component_role, "课程组成"))
    if context.document_role:
        parts.append({
            "academic_transcript": "成绩单",
            "offer": "录取通知文件",
            "signed_offer": "已签署录取通知",
            "coe": "入学确认文件",
        }.get(context.document_role, "相关文件"))
    parts.append(RECORD_CONTEXT_LABELS.get(context.record_context, "已关联"))
    return " · ".join(parts)


def _school_card(title: str, school: dict | None) -> str:
    if school is None:
        return f'<article class="lifecycle-school-card"><h4>{escape(title)}</h4><p>未确认</p></article>'
    course = " · ".join(str(value) for value in (
        school.get("course_name_snapshot"), school.get("year_level_snapshot")
    ) if value)
    dates = " → ".join(str(value) for value in (school.get("start_date"), school.get("end_date")) if value)
    return (
        f'<article class="lifecycle-school-card"><h4>{escape(title)}</h4>'
        f'<strong>{escape(str(school.get("school_name_snapshot") or ""))}</strong>'
        f'<p>{escape(course)}</p><small>{escape(dates)}</small></article>'
    )


def render_student_lifecycle_sections(
    view: StudentLifecycleView, *, confirm_current_href: str | None = None,
    manage_enrolments_href: str | None = None,
) -> str:
    if view.current is None:
        current = '<h3>当前没有正在办理的业务</h3>'
    else:
        item = view.current
        label = LIFECYCLE_LABELS.get(item.lifecycle_type, "学生业务")
        heading = f'{escape(label)} · 业务周期 #{item.cycle_sequence}'
        summary_link = (
            f'<a class="secondary button-link compact" href="{escape(item.href, quote=True)}">查看办理摘要</a>'
            if item.href else ""
        )
        operations = "".join(
            f'<a class="secondary button-link compact" href="/students/{escape(view.student_id, quote=True)}?tab={tab}">{escape(text)}</a>'
            for tab, text in (
                ("applications", "申请材料"), ("contracts", "合同"),
                ("school_plans", "学校方案"), ("offer", "录取通知"),
                ("payments", "付款"), ("coe", "入学确认与学生保险"),
                ("visa", "签证"), ("homestay", "住宿与接机"),
                ("history", "历史记录"), ("reminders", "留言与提醒"),
            )
        )
        current = (
            '<div class="lifecycle-current-facts">'
            f'<div><span>当前业务</span><strong>{heading}</strong></div>'
            f'<div><span>当前阶段</span><strong class="lifecycle-current-stage">{escape(_display_business_text(item.workflow_stage))}</strong></div>'
            '</div>'
            f'<small>开始时间 {escape(item.opened_at or "未记录")}'
            f'{" · 记录 #" + str(item.case_id) if item.case_id is not None else ""}</small>'
            f'<div class="row-actions lifecycle-profile-operations">{operations}{summary_link}</div>'
        )
    draft_rows = []
    for item in view.drafts:
        text = (
            f'{escape(LIFECYCLE_LABELS.get(item.lifecycle_type, "学生业务"))}'
            f'草稿 · 记录 #{item.case_id} · 创建于 {escape(item.opened_at or "未记录")}'
        )
        if item.href:
            text = f'<a href="{escape(item.href, quote=True)}">{text}</a>'
        draft_rows.append(f'<li>{text}</li>')
    drafts = "".join(draft_rows) or '<li>当前没有业务草稿</li>'
    history_rows = []
    for item in view.history:
        label = escape(
            LIFECYCLE_LABELS.get(
                item.lifecycle_type, "学生业务"
            )
        )
        if item.cycle_sequence is not None:
            title = f'{label} · 业务周期 #{item.cycle_sequence}'
        elif item.case_status == "draft":
            title = f'{label}草稿'
        else:
            title = f'{label} · 历史记录 · 办理状态待确认'
        if item.href:
            title = (
                f'<a href="{escape(item.href, quote=True)}">{title}</a>'
            )
        history_rows.append(
            '<li>'
            f'<strong>{title}</strong>'
            f'<span>{escape(STATUS_LABELS.get(item.cycle_status or item.case_status or "", item.cycle_status or item.case_status or "未记录"))}</span>'
            f'<small>{escape(item.opened_at or "开始时间未记录")}'
            f'{" · 记录 #" + str(item.case_id) if item.case_id is not None else ""}'
            f'{escape(" · " + item.target_context) if item.target_context else ""}'
            f'{escape(" · 结果：" + STATUS_LABELS.get(item.cycle_outcome, "已记录")) if item.cycle_outcome else ""}</small>'
            '</li>'
        )
    history = "".join(history_rows) or '<li>暂无业务历史</li>'
    confirm = ""
    if view.current_school is None and confirm_current_href:
        confirm = f'<a class="secondary button-link" href="{escape(confirm_current_href, quote=True)}">确认当前学校</a>'
    manage = (
        f'<a class="secondary button-link" href="{escape(manage_enrolments_href, quote=True)}">管理入读学校</a>'
        if manage_enrolments_href else ""
    )
    education = "".join(
        f'<li><strong>{escape(str(row.get("school_name_snapshot") or ""))}</strong>'
        f'<span>{escape(str(row.get("course_name_snapshot") or row.get("year_level_snapshot") or ""))}</span>'
        f'<small>{escape(str(row.get("start_date") or ""))} → {escape(str(row.get("end_date") or ""))}'
        f' · {escape(STATUS_LABELS.get(str(row.get("status") or ""), "历史记录"))}'
        f' · {escape(LIFECYCLE_LABELS.get(str(row.get("lifecycle_context") or ""), "历史记录"))}</small></li>'
        for row in view.education_history
    ) or '<li>暂无历史学校记录；当前学校请见上方。</li>'
    return f'''
      <section class="panel student-lifecycle-current" data-student-lifecycle-current>
        <div class="panel-head"><div><h2>当前办理业务</h2><p>来自当前正式申请周期与申请流程。</p></div></div>
        {current}<ul class="lifecycle-drafts">{drafts}</ul>
      </section>
      <section class="panel student-lifecycle-schools" data-student-lifecycle-schools>
        <div class="panel-head"><div><h2>当前 / 待入读 / 教育历史</h2><p>与学生正式入读记录保持一致。</p></div><div class="row-actions">{confirm}{manage}</div></div>
        <div class="lifecycle-school-grid">{_school_card("当前学校", view.current_school)}{_school_card("待入读学校", view.upcoming_school)}</div>
        <h3>教育历史</h3><ul>{education}</ul>
      </section>
      <section class="panel student-lifecycle-history" id="student-lifecycle-history" data-student-lifecycle-history>
        <div class="panel-head"><div><h2>业务历史</h2><p>保留首次申请、转学与升学记录。</p></div></div>
        <ul>{history}</ul>
      </section>'''


def filter_stage_students(stage_students: Iterable[object], lifecycle_type: str):
    requested = str(lifecycle_type or "all").strip().casefold()
    rows = tuple(stage_students)
    if requested == "all":
        return rows
    if requested not in LIFECYCLE_LABELS:
        raise ValueError("unsupported lifecycle filter")
    return tuple(row for row in rows if str(getattr(row, "cycle_type", "")).casefold() == requested)
