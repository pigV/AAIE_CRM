"""Read-only, presentation-free student application stage classification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from workflow_projection import load_current_workflow_projection
from workflow_resolver import ACTIVE_WORKFLOW_STAGES
from workflow_cutover import is_workflow_v2_active
from student_workflow_projection import load_student_workflow_projection_map


APPLICATION_STAGE_DEFINITIONS = (
    ("waiting_parent_form", "等待家长填写表单"),
    ("waiting_contract_signature", "等待家长签署合同"),
    ("school_plan", "学校方案阶段"),
    ("waiting_submission", "待递交学校申请"),
    ("waiting_school_offer", "等待学校 Offer"),
    ("waiting_final_school", "等待家长确认最终学校"),
    ("waiting_coe", "等待学校发 COE"),
    ("visa_preparation", "签证材料准备中"),
    ("waiting_visa_result", "等待签证结果"),
)

CANONICAL_APPLICATION_STAGE_DEFINITIONS = tuple(
    (stage.key, stage.title) for stage in ACTIVE_WORKFLOW_STAGES
)


@dataclass(frozen=True)
class ApplicationStageFacts:
    student_id: str
    student_name: str
    parent_form_link: bool = False
    parent_form_submitted: bool = False
    contract_sent: bool = False
    contract_parent_signed: bool = False
    plan_parent_confirmed: bool = False
    confirmed_school_count: int = 0
    submitted_school_count: int = 0
    resolved_school_count: int = 0
    final_school_selected: bool = False
    signed_offer_confirmed: bool = False
    payment_confirmed: bool = False
    final_school_coe_confirmed: bool = False
    visa_application_submitted: bool = False
    visa_actual_lodgement_date: str | None = None
    visa_final_result: bool = False
    deleted: bool = False


@dataclass(frozen=True)
class ApplicationStageResult:
    key: str | None
    label: str | None
    index: int | None
    blocker: str
    pending_count: int
    destination: str
    excluded_reason: str | None = None


@dataclass(frozen=True)
class ApplicationStageStudent:
    student_id: str
    student_name: str
    stage: ApplicationStageResult
    cycle_id: int | None = None
    entity_kind: str = "student"
    source_model: str = "legacy_student"
    cycle_type: str | None = None
    cycle_sequence: int | None = None
    target_context: str | None = None
    opened_at: str | None = None


def _stage(index, key, label, blocker, pending_count=0, destination=""):
    return ApplicationStageResult(key, label, index, blocker, pending_count, destination)


def _excluded(reason):
    return ApplicationStageResult(None, None, None, "", 0, "", reason)


def classify_application_stage(facts: ApplicationStageFacts) -> ApplicationStageResult:
    if facts.deleted:
        return _excluded("学生记录已删除")
    if not facts.parent_form_link:
        return _excluded("尚未生成家长表单 Link")
    if facts.visa_final_result:
        return _excluded("签证案件已结束")
    if facts.visa_application_submitted and facts.visa_actual_lodgement_date:
        return _stage(9, "waiting_visa_result", "等待签证结果", "等待移民局签证结果", destination="visa")
    if facts.final_school_coe_confirmed:
        blocker = "请完成 VISA Application Submitted 并填写实际递交日期"
        return _stage(8, "visa_preparation", "签证材料准备中", blocker, destination="visa")
    if facts.final_school_selected and facts.signed_offer_confirmed and facts.payment_confirmed:
        return _stage(7, "waiting_coe", "等待学校发 COE", "最终学校 COE 尚未上传确认", destination="coe")
    prior_school_gates = (
        facts.plan_parent_confirmed and facts.confirmed_school_count > 0
        and facts.submitted_school_count >= facts.confirmed_school_count
        and facts.resolved_school_count >= facts.confirmed_school_count
    )
    if prior_school_gates:
        missing = []
        if not facts.final_school_selected:
            missing.append("最终学校")
        if not facts.signed_offer_confirmed:
            missing.append("Signed Offer")
        if not facts.payment_confirmed:
            missing.append("付款证明")
        return _stage(6, "waiting_final_school", "等待家长确认最终学校", "缺 " + "、".join(missing), len(missing), "offer")
    if facts.plan_parent_confirmed and facts.confirmed_school_count > 0 and facts.submitted_school_count >= facts.confirmed_school_count:
        pending = max(facts.confirmed_school_count - facts.resolved_school_count, 0)
        return _stage(5, "waiting_school_offer", "等待学校 Offer", f"{pending} 所学校待结果", pending, "school_plans")
    if facts.plan_parent_confirmed:
        pending = max(facts.confirmed_school_count - facts.submitted_school_count, 0)
        blocker = f"{pending} 所学校待递交" if pending else "确认学校资料待核对"
        return _stage(4, "waiting_submission", "待递交学校申请", blocker, pending, "school_plans")
    if facts.contract_parent_signed:
        return _stage(3, "school_plan", "学校方案阶段", "等待家长签名确认学校方案", destination="school_plans")
    if facts.contract_sent:
        return _stage(2, "waiting_contract_signature", "等待家长签署合同", "合同已发送，等待家长签名", destination="contracts")
    blocker = "家长表单已完成，等待发送合同" if facts.parent_form_submitted else "等待家长填写表单"
    return _stage(1, "waiting_parent_form", "等待家长填写表单", blocker, destination="parent_form")


def _table_exists(connection, table: str) -> bool:
    return bool(connection.execute(f"PRAGMA table_info('{table}')").fetchone())


def _columns(connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')")}


def _rows(connection, sql: str, parameters: Iterable[object] = ()) -> list[dict]:
    return [dict(row) for row in connection.execute(sql, tuple(parameters)).fetchall()]


def _normal_school(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def uses_canonical_workflow(connection) -> bool:
    """Return the centralized explicit Workflow V2 read decision."""
    return is_workflow_v2_active(connection)


def application_stage_definitions_for_connection(connection):
    return (
        CANONICAL_APPLICATION_STAGE_DEFINITIONS
        if uses_canonical_workflow(connection)
        else APPLICATION_STAGE_DEFINITIONS
    )


def _canonical_stage_result(row) -> ApplicationStageResult:
    destination = {
        1: "parent_form",
        2: "contracts",
        3: "contracts",
        4: "school_plans",
        5: "school_plans",
        6: "school_plans",
        7: "school_plans",
        8: "offer",
        9: "coe",
        10: "visa",
        11: "visa",
    }.get(row.stage_number, "")
    destination = {
        "lifecycle_materials_plan": "school_plans",
        "lifecycle_plan_confirmation": "school_plans",
        "lifecycle_application": "school_plans",
        "lifecycle_school_result": "school_plans",
        "lifecycle_offer_acceptance": "offer",
        "lifecycle_payment": "payments",
        "lifecycle_coe": "coe",
        "lifecycle_oshc": "coe",
        "lifecycle_visa": "visa",
        "lifecycle_accommodation": "homestay",
    }.get(row.stage_key, destination)
    return ApplicationStageResult(
        row.stage_key,
        row.stage_title,
        row.stage_number,
        str(row.blocker or ""),
        1 if row.pending_action else 0,
        destination,
    )


def _student_names(connection, student_ids: tuple[str, ...]) -> dict[str, str]:
    if not student_ids:
        return {}
    columns = _columns(connection, "students")
    first = "passport_first_name" if "passport_first_name" in columns else "NULL"
    last = "passport_last_name" if "passport_last_name" in columns else "NULL"
    nick = "english_nickname" if "english_nickname" in columns else "NULL"
    marks = ",".join("?" for _ in student_ids)
    names = {}
    for row in connection.execute(
        f"SELECT student_id,{first},{last},{nick} FROM students WHERE student_id IN ({marks})",
        student_ids,
    ).fetchall():
        name = " ".join(str(value or "").strip() for value in (row[1], row[2]) if value)
        names[str(row[0])] = name or str(row[3] or row[0])
    return names


def _canonical_distribution(connection, *, read_context=None) -> tuple[ApplicationStageStudent, ...]:
    projection = (read_context.workflow(connection) if read_context is not None
                  else load_current_workflow_projection(connection))
    transfer_candidates = {
        str(row.student_id) for row in projection if row.student_id
    }
    if _table_exists(connection, "contracts"):
        contract_columns = _columns(connection, "contracts")
        if {"student_id", "contract_type"}.issubset(contract_columns):
            deleted = " AND deleted_at IS NULL" if "deleted_at" in contract_columns else ""
            voided = " AND voided_at IS NULL" if "voided_at" in contract_columns else ""
            transfer_candidates.update(
                str(row[0])
                for row in connection.execute(
                    """SELECT DISTINCT student_id FROM contracts
                         WHERE contract_type='TransferApplicationContract'"""
                    + deleted
                    + voided
                ).fetchall()
                if row[0]
            )
    if _table_exists(connection, "application_cycles"):
        transfer_candidates.update(
            str(row[0])
            for row in connection.execute(
                """SELECT DISTINCT student_id FROM application_cycles
                     WHERE cycle_type='transfer' AND student_id IS NOT NULL"""
            ).fetchall()
        )
    student_ids = tuple(transfer_candidates)
    student_workflow = load_student_workflow_projection_map(connection, student_ids, read_context=read_context)
    names = _student_names(connection, student_ids)
    cycle_ids = tuple(row.cycle_id for row in projection)
    cycle_meta = {}
    if cycle_ids:
        marks = ",".join("?" for _ in cycle_ids)
        cycles = connection.execute(
            f"""SELECT id,cycle_type,sequence_no,source_student_case_id,opened_at
                   FROM application_cycles WHERE id IN ({marks})""", cycle_ids
        ).fetchall()
        case_ids = tuple(int(row[3]) for row in cycles if row[3] is not None)
        case_types = {}
        if case_ids:
            case_marks = ",".join("?" for _ in case_ids)
            case_types = {
                int(row[0]): str(row[1]) for row in connection.execute(
                    f"SELECT id,case_type FROM student_cases WHERE id IN ({case_marks})",
                    case_ids,
                ).fetchall()
            }
        targets = {}
        progression_ids = tuple(case_id for case_id in case_ids if case_types.get(case_id) == "progression")
        if progression_ids and _table_exists(connection, "progression_target_applications"):
            target_marks = ",".join("?" for _ in progression_ids)
            for row in connection.execute(
                f"""SELECT case_id,institution_name_snapshot,course_name
                       FROM progression_target_applications
                      WHERE case_id IN ({target_marks})
                      ORDER BY case_id,is_final DESC,id""", progression_ids
            ).fetchall():
                targets.setdefault(int(row[0]), " · ".join(str(value) for value in row[1:] if value))
        transfer_ids = tuple(case_id for case_id in case_ids if case_types.get(case_id) == "transfer")
        if transfer_ids and _table_exists(connection, "transfer_school_applications"):
            target_marks = ",".join("?" for _ in transfer_ids)
            for row in connection.execute(
                f"""SELECT case_id,school_name_snapshot FROM transfer_school_applications
                      WHERE case_id IN ({target_marks})
                      ORDER BY case_id,is_final DESC,preference_order,id""", transfer_ids
            ).fetchall():
                targets.setdefault(int(row[0]), str(row[1]))
        for cycle in cycles:
            case_id = int(cycle[3]) if cycle[3] is not None else None
            cycle_meta[int(cycle[0])] = {
                "cycle_type": str(cycle[1]),
                "cycle_sequence": int(cycle[2]),
                "target_context": targets.get(case_id),
                "opened_at": str(cycle[4]) if cycle[4] is not None else None,
            }
    results = []
    represented_students = set()
    for row in projection:
        if row.student_id:
            record_id = row.student_id
            name = names.get(row.student_id, row.student_id)
            represented_students.add(str(row.student_id))
        else:
            record_id = row.cycle_uid
            name = f"Workflow Case · {row.cycle_uid}"
        workflow_item = student_workflow.get(str(row.student_id)) if row.student_id else None
        use_transfer_state = (
            workflow_item is not None
            and workflow_item.cycle_type == "transfer"
            and workflow_item.state in {
                "contract_pending", "ready", "active", "completed"
            }
        )
        transfer_source_model = (
            "application_cycle"
            if use_transfer_state and workflow_item.state == "active"
            else "student_transfer_state"
        )
        effective_cycle_id = (
            workflow_item.cycle_id if use_transfer_state else row.cycle_id
        )
        metadata = cycle_meta.get(int(row.cycle_id), {})
        if use_transfer_state:
            stage = ApplicationStageResult(
                workflow_item.stage_key,
                workflow_item.stage_label,
                workflow_item.stage_order,
                workflow_item.diagnostic or "",
                0,
                "contracts" if workflow_item.state == "contract_pending" else "",
            )
        else:
            stage = _canonical_stage_result(row)
        results.append(
            ApplicationStageStudent(
                record_id,
                name,
                stage,
                cycle_id=effective_cycle_id,
                entity_kind=("student" if use_transfer_state else row.entity_kind),
                source_model=(
                    transfer_source_model if use_transfer_state else "application_cycle"
                ),
                cycle_type=("transfer" if use_transfer_state else metadata.get("cycle_type")),
                cycle_sequence=metadata.get("cycle_sequence"),
                target_context=(
                    workflow_item.final_school_name
                    if use_transfer_state and workflow_item.state == "completed"
                    else metadata.get("target_context")
                ),
                opened_at=metadata.get("opened_at"),
            )
        )
    for student_id, workflow_item in student_workflow.items():
        if (
            student_id in represented_students
            or workflow_item.cycle_type != "transfer"
            or workflow_item.state not in {
                "contract_pending", "ready", "active", "completed"
            }
        ):
            continue
        results.append(
            ApplicationStageStudent(
                student_id,
                names.get(student_id, student_id),
                ApplicationStageResult(
                    workflow_item.stage_key,
                    workflow_item.stage_label,
                    workflow_item.stage_order,
                    workflow_item.diagnostic or "",
                    0,
                    "contracts" if workflow_item.state == "contract_pending" else "",
                ),
                cycle_id=workflow_item.cycle_id,
                entity_kind="student",
                source_model="student_transfer_state",
                cycle_type="transfer",
                target_context=workflow_item.final_school_name,
            )
        )
    return tuple(
        sorted(
            results,
            key=lambda item: (
                item.stage.index or 99,
                item.student_name.casefold(),
                item.student_id,
            ),
        )
    )


def _load_application_stage_fact_map(
    connection,
    student_id: str | None = None,
    student_ids: Iterable[str] | None = None,
):
    if student_id is not None and student_ids is not None:
        raise ValueError("student_id and student_ids cannot be used together")
    requested_ids = None if student_ids is None else tuple(dict.fromkeys(str(value) for value in student_ids))
    if student_id is not None:
        where = " WHERE student_id=?"
        parameters = (str(student_id),)
    elif requested_ids is not None:
        if not requested_ids:
            return {}
        where = f" WHERE student_id IN ({','.join('?' for _ in requested_ids)})"
        parameters = requested_ids
    else:
        where = ""
        parameters = ()
    student_rows = _rows(
        connection,
        """SELECT student_id,passport_first_name,passport_last_name,
                  english_nickname,deleted_at
             FROM students""" + where + " ORDER BY student_id",
        parameters,
    )
    if not student_rows:
        return {}
    student_ids = tuple(str(row["student_id"]) for row in student_rows)
    placeholders = ",".join("?" for _ in student_ids)
    facts = {}
    for row in student_rows:
        name = " ".join(
            part for part in (
                str(row.get("passport_first_name") or "").strip(),
                str(row.get("passport_last_name") or "").strip(),
            ) if part
        ) or str(row.get("english_nickname") or row["student_id"])
        facts[str(row["student_id"])] = {
            "student_id": str(row["student_id"]),
            "student_name": name,
            "deleted": bool(row.get("deleted_at")),
        }

    if _table_exists(connection, "parent_form_tokens"):
        for row in _rows(
            connection,
            f"""SELECT student_id,submitted_at,invalidated_at
                  FROM parent_form_tokens
                 WHERE student_id IN ({placeholders})""",
            student_ids,
        ):
            if row.get("invalidated_at"):
                continue
            current = facts[str(row["student_id"])]
            current["parent_form_link"] = True
            current["parent_form_submitted"] = bool(
                current.get("parent_form_submitted") or row.get("submitted_at")
            )

    if _table_exists(connection, "contracts"):
        has_signatures = _table_exists(connection, "contract_signatures")
        signature_sql = (
            "EXISTS(SELECT 1 FROM contract_signatures s WHERE s.contract_id=c.id "
            "AND s.status='signed' AND COALESCE(s.signed_at,'')<>'')"
            if has_signatures else "0"
        )
        contract_columns = _columns(connection, "contracts")
        deleted_clause = "AND c.deleted_at IS NULL" if "deleted_at" in contract_columns else ""
        for row in _rows(
            connection,
            f"""SELECT c.student_id,c.contract_status,c.signed_at,
                       {signature_sql} AS has_signature
                  FROM contracts c
                 WHERE c.student_id IN ({placeholders}) {deleted_clause}""",
            student_ids,
        ):
            current = facts[str(row["student_id"])]
            status = str(row.get("contract_status") or "")
            if status in {"Sent", "ParentSigned", "FullyExecuted"}:
                current["contract_sent"] = True
            if status in {"ParentSigned", "FullyExecuted"} or row.get("signed_at") or row.get("has_signature"):
                current["contract_sent"] = True
                current["contract_parent_signed"] = True

    if _table_exists(connection, "school_plan_reports") and _table_exists(connection, "school_plan_confirmed_schools"):
        school_columns = _columns(connection, "school_plan_confirmed_schools")
        result_sql = "cs.result_status" if "result_status" in school_columns else "'pending'"
        confirmation_sql = (
            "EXISTS(SELECT 1 FROM school_plan_report_confirmations c "
            "WHERE c.report_id=r.id AND c.status='confirmed' "
            "AND COALESCE(c.signature_image_path,'')<>'')"
            if _table_exists(connection, "school_plan_report_confirmations") else "0"
        )
        for row in _rows(
            connection,
            f"""SELECT r.student_id,r.id AS report_id,
                       {confirmation_sql} AS parent_confirmed,
                       cs.id AS confirmed_school_id,cs.submission_status,
                       {result_sql} AS result_status
                  FROM school_plan_reports r
                  LEFT JOIN school_plan_confirmed_schools cs
                    ON cs.report_id=r.id AND cs.is_selected=1
                 WHERE r.student_id IN ({placeholders})
                   AND r.is_current=1 AND r.invalidated_at IS NULL""",
            student_ids,
        ):
            current = facts[str(row["student_id"])]
            confirmed = bool(row.get("parent_confirmed"))
            current["plan_parent_confirmed"] = bool(
                current.get("plan_parent_confirmed") or confirmed
            )
            if not confirmed or row.get("confirmed_school_id") is None:
                continue
            current["confirmed_school_count"] = int(current.get("confirmed_school_count", 0)) + 1
            if row.get("submission_status") == "submitted":
                current["submitted_school_count"] = int(current.get("submitted_school_count", 0)) + 1
            if str(row.get("result_status") or "pending") != "pending":
                current["resolved_school_count"] = int(current.get("resolved_school_count", 0)) + 1

    accepted_offers = {}
    if _table_exists(connection, "student_offer_acceptances") and _table_exists(connection, "offers"):
        offer_columns = _columns(connection, "offers")
        option = lambda name: f"o.{name}" if name in offer_columns else f"NULL AS {name}"
        for row in _rows(
            connection,
            f"""SELECT a.student_id,o.id AS offer_id,{option('school_id')},
                       {option('school_name')},{option('signed_offer_file_path')},
                       {option('payment_receipt_file_path')},{option('deposit_paid_date')}
                  FROM student_offer_acceptances a
                  JOIN offers o ON o.id=a.offer_id
                 WHERE a.student_id IN ({placeholders})""",
            student_ids,
        ):
            sid = str(row["student_id"])
            accepted_offers[sid] = row
            current = facts[sid]
            current["final_school_selected"] = True
            current["signed_offer_confirmed"] = bool(row.get("signed_offer_file_path"))
            current["payment_confirmed"] = bool(
                row.get("payment_receipt_file_path") and row.get("deposit_paid_date")
            )

    if accepted_offers and _table_exists(connection, "coe_oshc"):
        coe_columns = _columns(connection, "coe_oshc")
        school_name_sql = "school_name" if "school_name" in coe_columns else "NULL AS school_name"
        for row in _rows(
            connection,
            f"""SELECT student_id,document_kind,{school_name_sql},file_path
                  FROM coe_oshc
                 WHERE student_id IN ({placeholders})
                   AND document_kind IN ('COE','COE_CAAW')""",
            student_ids,
        ):
            sid = str(row["student_id"])
            accepted = accepted_offers.get(sid)
            if not accepted or not row.get("file_path"):
                continue
            if _normal_school(row.get("school_name")) == _normal_school(accepted.get("school_name")):
                facts[sid]["final_school_coe_confirmed"] = True

    if _table_exists(connection, "visa_cases"):
        visa_columns = _columns(connection, "visa_cases")
        option = lambda name: name if name in visa_columns else f"NULL AS {name}"
        for row in _rows(
            connection,
            f"""SELECT student_id,{option('actual_lodgement_date')},
                       {option('lodged_by_user_id')},{option('lodged_by_name')},
                       {option('lodgement_recorded_at')},{option('result_status')},
                       {option('final_result')},{option('archived_at')}
                  FROM visa_cases
                 WHERE student_id IN ({placeholders})""",
            student_ids,
        ):
            sid = str(row["student_id"])
            current = facts[sid]
            actual_date = str(row.get("actual_lodgement_date") or "").strip()
            evidence = bool(row.get("lodgement_recorded_at") and (row.get("lodged_by_user_id") or row.get("lodged_by_name")))
            if actual_date and evidence:
                current["visa_application_submitted"] = True
                current["visa_actual_lodgement_date"] = actual_date
            terminal = {
                "granted", "refused", "withdrawn", "closed", "cancelled", "canceled"
            }
            if (
                str(row.get("result_status") or "").strip().casefold() in terminal
                or str(row.get("final_result") or "").strip().casefold() in terminal
                or row.get("archived_at")
            ):
                current["visa_final_result"] = True

    return {
        sid: ApplicationStageFacts(**values)
        for sid, values in facts.items()
    }


def load_application_stage_facts(connection, student_id: str) -> ApplicationStageFacts:
    fact = _load_application_stage_fact_map(connection, str(student_id)).get(str(student_id))
    if fact is None:
        raise LookupError("学生记录不存在。")
    return fact


def load_application_stage_result_map(
    connection,
    student_ids: Iterable[str] | None = None,
    *,
    workflow_projection_map: Mapping[str, object] | None = None,
    read_context=None,
) -> dict[str, ApplicationStageResult]:
    if uses_canonical_workflow(connection):
        requested = None if student_ids is None else tuple(
            dict.fromkeys(str(value) for value in student_ids)
        )
        if requested is None:
            columns = _columns(connection, "students")
            deleted = " WHERE coalesce(deleted_at,'')=''" if "deleted_at" in columns else ""
            requested = tuple(
                str(row[0])
                for row in connection.execute(
                    f"SELECT student_id FROM students{deleted} ORDER BY student_id"
                ).fetchall()
            )
        canonical = (
            dict(workflow_projection_map)
            if workflow_projection_map is not None
            else load_student_workflow_projection_map(connection, requested, read_context=read_context)
        )
        return {
            student_id: ApplicationStageResult(
                item.stage_key,
                item.stage_label,
                item.stage_order,
                item.diagnostic or "",
                0,
                "",
                None,
            )
            for student_id, item in canonical.items()
        }
    facts = _load_application_stage_fact_map(connection, student_ids=student_ids)
    return {
        student_id: classify_application_stage(fact)
        for student_id, fact in facts.items()
        if not fact.deleted
    }


def load_application_stage_distribution(connection, *, read_context=None) -> tuple[ApplicationStageStudent, ...]:
    if uses_canonical_workflow(connection):
        return _canonical_distribution(connection, read_context=read_context)
    students = []
    for fact in _load_application_stage_fact_map(connection).values():
        stage = classify_application_stage(fact)
        if stage.key is None:
            continue
        students.append(ApplicationStageStudent(fact.student_id, fact.student_name, stage))
    return tuple(sorted(students, key=lambda item: (item.stage.index or 99, item.student_name.casefold(), item.student_id)))


def active_cycle_type_counts(
    stage_students: Iterable[ApplicationStageStudent],
) -> dict[str, int]:
    """Count active canonical projection rows by Lifecycle type."""

    counts = {"initial": 0, "transfer": 0, "progression": 0}
    for student in stage_students:
        if student.source_model != "application_cycle" or student.cycle_id is None:
            continue
        cycle_type = str(student.cycle_type or "")
        if cycle_type in counts:
            counts[cycle_type] += 1
    return counts
