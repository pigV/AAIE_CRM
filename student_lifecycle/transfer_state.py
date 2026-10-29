"""Shared authoritative Student Transfer state and create-gate resolver.

This module deliberately reads existing facts only.  It owns neither schema nor
history repair; the only write helper closes one fact-empty Initial Cycle inside
the caller's existing Transfer-create transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Iterable

from student_lifecycle.transfer_v2_projection import load_transfer_v2_projection


PENDING_LABEL = "等待转学合同签署"
READY_LABEL = "转学合同已完成 · 待建立转学记录"
ACTIVE_PREFIX = "转学中 · "
COMPLETED_PREFIX = "转学完成 · "


def _signed_artifact_exists(value: object) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        return Path(raw).is_file()
    except OSError:
        return False


@dataclass(frozen=True)
class StudentTransferState:
    student_id: str
    state: str
    label: str
    stage_key: str
    stage_order: int
    contract_id: int | None = None
    cycle_id: int | None = None
    case_id: int | None = None
    final_school_name: str | None = None
    can_create: bool = False


@dataclass(frozen=True)
class TransferCreationState:
    allowed: bool
    contract_id: int | None
    reason: str
    label: str
    takeover_cycle_id: int | None = None
    blocking_cycle_id: int | None = None


APPLICATION_FACT_TABLES = (
    "school_plan_reports",
    "private_school_applications",
    "public_application_groups",
    "higher_education_plans",
    "higher_education_applications",
    "offers",
    "higher_education_offers",
    "offer_received_events",
    "cycle_final_acceptances",
    "final_school_submission_events",
    "coe_oshc",
    "visa_cases",
    "visa_decision_events",
    "progression_pathway_acceptances",
)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')")
    }


def _current_contract_rows(
    connection: sqlite3.Connection, student_ids: tuple[str, ...]
) -> dict[str, sqlite3.Row]:
    if not student_ids or not _table_exists(connection, "contracts"):
        return {}
    columns = _columns(connection, "contracts")
    required = {"id", "student_id", "contract_type", "contract_status"}
    if not required.issubset(columns):
        return {}
    marks = ",".join("?" for _ in student_ids)
    deleted = "contract.deleted_at IS NULL" if "deleted_at" in columns else "1=1"
    voided = "contract.voided_at IS NULL" if "voided_at" in columns else "1=1"
    application_cycle_id = (
        "contract.application_cycle_id"
        if "application_cycle_id" in columns
        else "NULL"
    )
    final_path = (
        "contract.final_signed_file_path"
        if "final_signed_file_path" in columns
        else "NULL"
    )
    fully_executed_at = (
        "contract.fully_executed_at" if "fully_executed_at" in columns else "NULL"
    )
    updated_at = "contract.updated_at" if "updated_at" in columns else "NULL"
    transfer_case_join = ""
    consumed_case_id = "NULL"
    if _table_exists(connection, "transfer_cases") and "source_contract_id" in _columns(
        connection, "transfer_cases"
    ):
        transfer_case_join = (
            "LEFT JOIN transfer_cases AS consumed "
            "ON consumed.source_contract_id=contract.id"
        )
        consumed_case_id = "consumed.case_id"
    rows = connection.execute(
        f"""SELECT contract.id,contract.student_id,contract.contract_status,
                   {final_path} AS final_signed_file_path,
                   {fully_executed_at} AS fully_executed_at,
                   {updated_at} AS updated_at,
                   {application_cycle_id} AS application_cycle_id,
                   {consumed_case_id} AS consumed_case_id
              FROM contracts AS contract
              {transfer_case_join}
             WHERE contract.student_id IN ({marks})
               AND contract.contract_type='TransferApplicationContract'
               AND {deleted} AND {voided}
             ORDER BY contract.student_id,
                      COALESCE({fully_executed_at},{updated_at},'') DESC,
                      contract.id DESC""",
        student_ids,
    ).fetchall()
    result: dict[str, sqlite3.Row | None] = {}
    for row in rows:
        student_id = str(row[1])
        if student_id in result:
            continue
        # Only the newest valid Transfer contract can be the current contract.
        # An older unbound Draft must not overtake a later consumed completed
        # contract and resurrect a cancelled/completed lifecycle as pending.
        result[student_id] = row if row[6] is None and row[7] is None else None
    return {student_id: row for student_id, row in result.items() if row is not None}


def _active_transfer_rows(
    connection: sqlite3.Connection, student_ids: tuple[str, ...]
) -> dict[str, sqlite3.Row]:
    if not student_ids or not _table_exists(connection, "application_cycles"):
        return {}
    marks = ",".join("?" for _ in student_ids)
    rows = connection.execute(
        f"""SELECT id,student_id,source_student_case_id
              FROM application_cycles
             WHERE student_id IN ({marks}) AND cycle_type='transfer'
               AND status='active'
             ORDER BY id DESC""",
        student_ids,
    ).fetchall()
    result: dict[str, sqlite3.Row] = {}
    for row in rows:
        result.setdefault(str(row[1]), row)
    return result


def _completed_transfer_rows(
    connection: sqlite3.Connection, student_ids: tuple[str, ...]
) -> dict[str, sqlite3.Row]:
    required_tables = {
        "application_cycles",
        "transfer_cases",
        "transfer_completion_events",
    }
    if not student_ids or not all(_table_exists(connection, table) for table in required_tables):
        return {}
    marks = ",".join("?" for _ in student_ids)
    rows = connection.execute(
        f"""SELECT cycle.id,cycle.student_id,cycle.source_student_case_id,
                   completion.final_school_name_snapshot
              FROM application_cycles AS cycle
              JOIN transfer_cases AS transfer
                ON transfer.case_id=cycle.source_student_case_id
              JOIN transfer_completion_events AS completion
                ON completion.application_cycle_id=cycle.id
             WHERE cycle.student_id IN ({marks})
               AND cycle.cycle_type='transfer'
               AND cycle.status<>'active' AND cycle.outcome='completed'
               AND COALESCE(transfer.v2_version,0)=2
             ORDER BY COALESCE(cycle.closed_at,cycle.updated_at,cycle.created_at) DESC,
                      cycle.id DESC""",
        student_ids,
    ).fetchall()
    result: dict[str, sqlite3.Row] = {}
    for row in rows:
        result.setdefault(str(row[1]), row)
    return result


def load_student_transfer_state_map(
    connection: sqlite3.Connection, student_ids: Iterable[str]
) -> dict[str, StudentTransferState]:
    """Resolve Transfer state in one consistent authority order.

    Cancelled Transfer history is intentionally not projected as a current
    state.  Its immutable history remains in the existing Transfer history UI.
    """

    ids = tuple(dict.fromkeys(str(value) for value in student_ids if value))
    if not ids:
        return {}
    active = _active_transfer_rows(connection, ids)
    contracts = _current_contract_rows(connection, ids)
    completed = _completed_transfer_rows(connection, ids)
    results: dict[str, StudentTransferState] = {}
    for student_id in ids:
        active_row = active.get(student_id)
        if active_row is not None:
            cycle_id = int(active_row[0])
            try:
                projection = load_transfer_v2_projection(connection, cycle_id)
                stage_key = projection.stage_key
                stage_order = projection.stage_order
                stage_label = projection.stage_label
                active_label = (
                    "Legacy Active Compatibility"
                    if projection.is_legacy
                    else f"{ACTIVE_PREFIX}{stage_label}"
                )
            except Exception:
                stage_key = "transfer_projection_error"
                stage_order = 999
                stage_label = "阶段读取异常"
                active_label = f"{ACTIVE_PREFIX}{stage_label}"
            results[student_id] = StudentTransferState(
                student_id=student_id,
                state="active",
                label=active_label,
                stage_key=stage_key,
                stage_order=stage_order,
                cycle_id=cycle_id,
                case_id=(
                    int(active_row[2]) if active_row[2] is not None else None
                ),
            )
            continue
        contract = contracts.get(student_id)
        is_ready = bool(
            contract is not None
            and str(contract[2] or "") == "FullyExecuted"
            and _signed_artifact_exists(contract[3])
        )
        if contract is not None and is_ready:
            contract_id = int(contract[0])
            results[student_id] = StudentTransferState(
                student_id=student_id,
                state="ready",
                label=READY_LABEL,
                stage_key="transfer_contract_ready",
                stage_order=18,
                contract_id=contract_id,
                can_create=True,
            )
            continue
        completed_row = completed.get(student_id)
        if completed_row is not None:
            school_name = str(completed_row[3] or "").strip()
            results[student_id] = StudentTransferState(
                student_id=student_id,
                state="completed",
                label=f"{COMPLETED_PREFIX}{school_name}",
                stage_key="transfer_completed",
                stage_order=100,
                cycle_id=int(completed_row[0]),
                case_id=(
                    int(completed_row[2]) if completed_row[2] is not None else None
                ),
                final_school_name=school_name,
            )
            continue
        if contract is not None:
            results[student_id] = StudentTransferState(
                student_id=student_id,
                state="contract_pending",
                label=PENDING_LABEL,
                stage_key="transfer_contract_pending",
                stage_order=15,
                contract_id=int(contract[0]),
            )
            continue
        results[student_id] = StudentTransferState(
            student_id=student_id,
            state="ordinary",
            label="",
            stage_key="ordinary",
            stage_order=0,
        )
    return results


def cycle_has_application_facts(
    connection: sqlite3.Connection, cycle_id: int
) -> bool:
    for table in APPLICATION_FACT_TABLES:
        if "application_cycle_id" not in _columns(connection, table):
            continue
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE application_cycle_id=? LIMIT 1",
            (int(cycle_id),),
        ).fetchone() is not None:
            return True
    return False


def _active_non_transfer_cycle(connection: sqlite3.Connection, student_id: str):
    if not _table_exists(connection, "application_cycles"):
        return None
    return connection.execute(
        """SELECT id,cycle_type,source_student_case_id
             FROM application_cycles
            WHERE student_id=? AND status='active' AND cycle_type<>'transfer'
            ORDER BY id DESC LIMIT 1""",
        (student_id,),
    ).fetchone()


def _safe_initial_takeover(
    connection: sqlite3.Connection, cycle: sqlite3.Row | tuple
) -> bool:
    return (
        str(cycle[1] or "") == "initial"
        and cycle[2] is None
        and not cycle_has_application_facts(connection, int(cycle[0]))
    )


def evaluate_transfer_creation_state(
    connection: sqlite3.Connection, student_id: str
) -> TransferCreationState:
    normalized = str(student_id or "").strip()
    if not normalized:
        return TransferCreationState(False, None, "student_missing", "学生不存在")
    student_columns = _columns(connection, "students")
    deleted = " AND deleted_at IS NULL" if "deleted_at" in student_columns else ""
    if connection.execute(
        f"SELECT 1 FROM students WHERE student_id=?{deleted}", (normalized,)
    ).fetchone() is None:
        return TransferCreationState(False, None, "student_missing", "学生不存在")
    active_transfer = _active_transfer_rows(connection, (normalized,)).get(normalized)
    if active_transfer is not None:
        return TransferCreationState(
            False,
            None,
            "active_transfer_exists",
            "已有进行中的转学记录，不能重复建立。",
            blocking_cycle_id=int(active_transfer[0]),
        )
    contract = _current_contract_rows(connection, (normalized,)).get(normalized)
    contract_id = int(contract[0]) if contract is not None else None
    contract_ready = bool(
        contract is not None
        and str(contract[2] or "") == "FullyExecuted"
        and _signed_artifact_exists(contract[3])
    )
    active_other = _active_non_transfer_cycle(connection, normalized)
    takeover_cycle_id = None
    if active_other is not None:
        if _safe_initial_takeover(connection, active_other):
            takeover_cycle_id = int(active_other[0])
        else:
            return TransferCreationState(
                False,
                contract_id,
                "active_school_cycle_has_business_facts",
                "已有进行中的申请周期，不能直接建立转学。",
                blocking_cycle_id=int(active_other[0]),
            )
    if not contract_ready:
        return TransferCreationState(
            False,
            contract_id,
            "contract_required",
            PENDING_LABEL,
            takeover_cycle_id=takeover_cycle_id,
        )
    return TransferCreationState(
        True,
        contract_id,
        "ready",
        READY_LABEL,
        takeover_cycle_id=takeover_cycle_id,
    )


def supersede_safe_initial_for_transfer(
    connection: sqlite3.Connection,
    *,
    student_id: str,
    cycle_id: int,
    actor_user_id: int,
    occurred_at: str,
) -> None:
    row = connection.execute(
        """SELECT id,cycle_type,source_student_case_id
             FROM application_cycles
            WHERE id=? AND student_id=? AND status='active'""",
        (int(cycle_id), str(student_id)),
    ).fetchone()
    if row is None or not _safe_initial_takeover(connection, row):
        raise RuntimeError("active lifecycle is not a safe Initial Cycle")
    updated = connection.execute(
        """UPDATE application_cycles
              SET status='closed',outcome='superseded_by_transfer',closed_at=?,
                  updated_by_user_id=?,updated_at=?
            WHERE id=? AND student_id=? AND status='active'
              AND cycle_type='initial' AND source_student_case_id IS NULL""",
        (
            str(occurred_at),
            int(actor_user_id),
            str(occurred_at),
            int(cycle_id),
            str(student_id),
        ),
    )
    if updated.rowcount != 1:
        raise RuntimeError("safe Initial Cycle changed concurrently")
