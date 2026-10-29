"""Canonical ownership boundary for completed external Transfer contracts."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import threading

from application_cycle import record_application_lifecycle_event
from contract_domain import TRANSFER_APPLICATION_CONTRACT, normalize_contract_type
from security import now_iso
from student_case_feature_flags import StudentCaseFeatureFlags
from transfer_case_errors import TransferCaseError
from transfer_case_service import TransferCaseService

from .adapters import ApplicationCycleGateway
from .commands import activate_lifecycle
from . import repository


CURRENT_SCHOOL_ACTION = "确认当前学校后前往 Student Profile"
_LOCK = threading.RLock()
_TERMINAL_CASE_STATUSES = ("completed", "cancelled", "failed", "archived")


@dataclass(frozen=True)
class ExternalTransferContractResult:
    status: str
    contract_id: int
    student_id: str | None
    case_id: int | None = None
    cycle_id: int | None = None
    current_enrolment_id: int | None = None
    final_document_id: int | None = None
    action_label: str | None = None
    message: str = ""


def _decision(
    status: str,
    contract_id: int,
    student_id: str | None,
    message: str,
    *,
    action_label: str | None = None,
) -> ExternalTransferContractResult:
    return ExternalTransferContractResult(
        status=status,
        contract_id=int(contract_id),
        student_id=student_id,
        action_label=action_label,
        message=message,
    )


def can_orchestrate_external_transfer(
    actor_permissions, flags: StudentCaseFeatureFlags,
) -> bool:
    permissions = set(actor_permissions or ())
    return bool(
        flags.read_enabled
        and flags.transfer_write_enabled
        and flags.external_transfer_auto_create_enabled
        and ("*" in permissions or "student_cases.create" in permissions)
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')")
    }


def _school_cycle_filter(connection: sqlite3.Connection) -> str:
    return (
        " AND education_stream='school_education'"
        if "education_stream" in _columns(connection, "application_cycles")
        else ""
    )


def inspect_transfer_contract(
    connection: sqlite3.Connection, contract_id: int,
) -> ExternalTransferContractResult:
    """Inspect eligibility without mutating contract, lifecycle, or Case state."""

    connection.row_factory = sqlite3.Row
    contract = connection.execute(
        "SELECT * FROM contracts WHERE id=?", (int(contract_id),)
    ).fetchone()
    if contract is None:
        return _decision("contract_not_found", contract_id, None, "合同不存在。")
    student_id = str(contract["student_id"] or "").strip()
    if not student_id:
        return _decision(
            "contract_conflict", contract_id, None,
            "转学合同没有唯一 Student 归属。",
        )
    if normalize_contract_type(contract["contract_type"]) != TRANSFER_APPLICATION_CONTRACT:
        return _decision(
            "not_transfer_contract", contract_id, student_id,
            "该合同不是转学申请合同。",
        )
    if str(contract["contract_status"] or "") != "FullyExecuted":
        return _decision(
            "contract_not_completed", contract_id, student_id,
            "转学合同尚未完成双方签署。",
        )
    if contract["deleted_at"] or contract["voided_at"]:
        return _decision(
            "contract_conflict", contract_id, student_id,
            "转学合同已删除或作废。",
        )
    final_path = str(contract["final_signed_file_path"] or "").strip()
    if not final_path:
        return _decision(
            "final_document_conflict", contract_id, student_id,
            "转学合同没有最终 PDF。",
        )
    final_documents = connection.execute(
        """SELECT id FROM student_documents
            WHERE student_id=? AND file_path=? AND is_active=1
              AND folder_category='Applications'
              AND document_type='FinalSignedStudyAbroadContract'
              AND LOWER(file_path) LIKE '%.pdf'
              AND COALESCE(document_status,'active')='active'
            ORDER BY id""",
        (student_id, final_path),
    ).fetchall()
    if len(final_documents) != 1:
        return _decision(
            "final_document_conflict", contract_id, student_id,
            "最终 PDF 无法按精确文件记录确认唯一归属。",
        )
    final_document_id = int(final_documents[0][0])

    # A completed request is immutable orchestration evidence. Validate and
    # replay that graph before evaluating the student's later active/closed
    # lifecycle state.
    request = connection.execute(
        """SELECT request_status,case_id FROM external_transfer_case_requests
            WHERE student_id=? AND contract_id=?""",
        (student_id, int(contract_id)),
    ).fetchone()
    if request is not None and str(request["request_status"]) == "completed":
        graph = connection.execute(
            """SELECT cycles.id,transfer.current_school_enrolment_id
                 FROM student_cases AS cases
                 JOIN application_cycles AS cycles
                   ON cycles.source_student_case_id=cases.id
                 JOIN transfer_cases AS transfer ON transfer.case_id=cases.id
                 JOIN student_school_enrolments AS enrolment
                   ON enrolment.id=transfer.current_school_enrolment_id
                  AND enrolment.student_id=cases.student_id
                 JOIN case_record_links AS link
                   ON link.case_id=cases.id AND link.record_type='student_document'
                  AND link.record_id=? AND link.validity_status='current'
                  AND link.link_role='completion_document'
                WHERE cases.id=? AND cases.student_id=?
                  AND cycles.cycle_type='transfer' AND cycles.id=?
                  AND transfer.source_contract_id=?""",
            (
                final_document_id, int(request["case_id"]), student_id,
                int(contract["application_cycle_id"] or -1), int(contract_id),
            ),
        ).fetchone()
        if graph is None or graph[1] is None:
            return _decision(
                "request_conflict", contract_id, student_id,
                "已完成 request 的 Case/Cycle 图不完整。",
            )
        return ExternalTransferContractResult(
            status="completed", contract_id=int(contract_id),
            student_id=student_id, case_id=int(request["case_id"]),
            cycle_id=int(graph[0]), current_enrolment_id=int(graph[1]),
            final_document_id=final_document_id, message="Transfer Case 已建立。",
        )

    duplicate_contract = connection.execute(
        """SELECT id FROM contracts
            WHERE id<>? AND student_id=? AND final_signed_file_path=?
              AND contract_status='FullyExecuted'
              AND COALESCE(deleted_at,'')='' AND COALESCE(voided_at,'')=''
            LIMIT 1""",
        (int(contract_id), student_id, final_path),
    ).fetchone()
    if duplicate_contract is not None:
        return _decision(
            "final_document_conflict", contract_id, student_id,
            "最终 PDF 已被另一份有效合同引用，无法确认唯一归属。",
        )

    active_cycles = connection.execute(
        f"""SELECT * FROM application_cycles
             WHERE student_id=? AND status='active'{_school_cycle_filter(connection)}
             ORDER BY id""",
        (student_id,),
    ).fetchall()
    if len(active_cycles) > 1:
        return _decision(
            "multiple_active_cycles", contract_id, student_id,
            "存在多个 active Application Cycle，必须先完成核对。",
        )
    active_cycle = active_cycles[0] if active_cycles else None
    assigned_cycle_id = contract["application_cycle_id"]
    if assigned_cycle_id is not None and (
        active_cycle is None or int(active_cycle["id"]) != int(assigned_cycle_id)
    ):
        return _decision(
            "contract_cycle_conflict", contract_id, student_id,
            "合同已属于另一个或已结束的 Application Cycle。",
        )

    open_cases = connection.execute(
        """SELECT cases.id,cases.status,cycles.id AS cycle_id,cycles.cycle_type,
                  transfer.source_contract_id,transfer.current_school_enrolment_id,
                  transfer.current_school_id,transfer.current_school_name_snapshot
             FROM student_cases AS cases
             JOIN transfer_cases AS transfer ON transfer.case_id=cases.id
             LEFT JOIN application_cycles AS cycles
               ON cycles.source_student_case_id=cases.id
            WHERE cases.student_id=? AND cases.case_type='transfer'
              AND cases.status NOT IN ('completed','cancelled','failed','archived')
            ORDER BY cases.id""",
        (student_id,),
    ).fetchall()
    if len(open_cases) > 1:
        return _decision(
            "multiple_transfer_cases", contract_id, student_id,
            "存在多个未完成 Transfer Case，必须先完成核对。",
        )
    existing_case = open_cases[0] if open_cases else None
    if (
        existing_case is not None
        and existing_case["source_contract_id"] is not None
        and int(existing_case["source_contract_id"]) != int(contract_id)
    ):
        return _decision(
            "existing_case_conflict", contract_id, student_id,
            "未完成 Transfer Case 已属于另一份来源合同。",
        )
    completion_owners = connection.execute(
        """SELECT case_id FROM case_record_links
            WHERE record_type='student_document' AND record_id=?
              AND link_role='completion_document' AND validity_status='current'""",
        (final_document_id,),
    ).fetchall()
    if any(
        existing_case is None or int(row[0]) != int(existing_case["id"])
        for row in completion_owners
    ):
        return _decision(
            "final_document_conflict", contract_id, student_id,
            "最终 PDF 已属于另一个 Transfer Case。",
        )

    if active_cycle is not None:
        cycle_type = str(active_cycle["cycle_type"])
        source_case_id = active_cycle["source_student_case_id"]
        if cycle_type == "transfer":
            if (
                source_case_id is None
                or existing_case is None
                or int(existing_case["id"]) != int(source_case_id)
            ):
                return _decision(
                    "active_cycle_conflict", contract_id, student_id,
                    "active Transfer Cycle 与 Transfer Case 不一致。",
                )
        elif cycle_type == "initial":
            if source_case_id is not None or not active_cycle["parent_form_token"]:
                return _decision(
                    "initial_cycle_conflict", contract_id, student_id,
                    "Initial Cycle 不是可确认的 provisional Parent Form Cycle。",
                )
            competing_tables = (
                "offers", "coe_oshc", "visa_cases", "school_plan_reports",
                "public_application_groups", "private_school_applications",
                "higher_education_plans", "higher_education_applications",
            )
            for table in competing_tables:
                if not _table_exists(connection, table):
                    continue
                if "application_cycle_id" not in _columns(connection, table):
                    continue
                if connection.execute(
                    f"SELECT 1 FROM {table} WHERE application_cycle_id=? LIMIT 1",
                    (int(active_cycle["id"]),),
                ).fetchone() is not None:
                    return _decision(
                        "initial_cycle_conflict", contract_id, student_id,
                        "Initial Cycle 已有其他 canonical 业务事实，不能转为 Transfer。",
                    )
            if connection.execute(
                """SELECT 1 FROM contracts
                    WHERE application_cycle_id=? AND id<>?
                      AND COALESCE(deleted_at,'')=''
                      AND COALESCE(voided_at,'')=''
                    LIMIT 1""",
                (int(active_cycle["id"]), int(contract_id)),
            ).fetchone() is not None:
                return _decision(
                    "contract_cycle_conflict", contract_id, student_id,
                    "Initial Cycle 已有关联其他有效合同。",
                )
        else:
            return _decision(
                "active_cycle_conflict", contract_id, student_id,
                f"当前 active {cycle_type} Lifecycle 阻止建立 Transfer。",
            )

    current_enrolments = connection.execute(
        """SELECT * FROM student_school_enrolments
            WHERE student_id=? AND enrolment_role='current' AND status='active'
            ORDER BY id""",
        (student_id,),
    ).fetchall()
    if len(current_enrolments) > 1:
        return _decision(
            "current_enrolment_conflict", contract_id, student_id,
            "存在多个 active Current Enrolment，必须先完成核对。",
        )
    if not current_enrolments:
        return _decision(
            "needs_current_enrolment", contract_id, student_id,
            "请先确认 authoritative Current Enrolment。",
            action_label=CURRENT_SCHOOL_ACTION,
        )
    current_enrolment = current_enrolments[0]
    if existing_case is not None:
        linked_enrolment_id = existing_case["current_school_enrolment_id"]
        if (
            linked_enrolment_id is not None
            and int(linked_enrolment_id) != int(current_enrolment["id"])
        ):
            return _decision(
                "existing_case_conflict", contract_id, student_id,
                "未完成 Transfer Case 已关联另一条 Current Enrolment。",
            )
        case_school_id = existing_case["current_school_id"]
        enrolment_school_id = current_enrolment["school_id"]
        if (
            case_school_id is not None and enrolment_school_id is not None
            and int(case_school_id) != int(enrolment_school_id)
        ):
            return _decision(
                "existing_case_conflict", contract_id, student_id,
                "未完成 Transfer Case 的当前学校与 authoritative Enrolment 不一致。",
            )
        case_school_name = str(
            existing_case["current_school_name_snapshot"] or ""
        ).strip().casefold()
        enrolment_school_name = str(
            current_enrolment["school_name_snapshot"] or ""
        ).strip().casefold()
        if case_school_name != enrolment_school_name:
            return _decision(
                "existing_case_conflict", contract_id, student_id,
                "未完成 Transfer Case 的当前学校与 authoritative Enrolment 不一致。",
            )
    return ExternalTransferContractResult(
        status="ready", contract_id=int(contract_id), student_id=student_id,
        case_id=int(existing_case["id"]) if existing_case else None,
        cycle_id=int(active_cycle["id"]) if active_cycle else None,
        current_enrolment_id=int(current_enrolment["id"]),
        final_document_id=final_document_id,
        message="可以建立或恢复 Transfer Case。",
    )


class ExternalTransferContractOrchestrator:
    def __init__(self, database_path: str | Path, *, clock=now_iso):
        self.database_path = Path(database_path)
        self.clock = clock

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def inspect(self, contract_id: int) -> ExternalTransferContractResult:
        with closing(self._connect()) as connection:
            return inspect_transfer_contract(connection, int(contract_id))

    @staticmethod
    def _allowed(actor_permissions, flags: StudentCaseFeatureFlags) -> bool:
        return can_orchestrate_external_transfer(actor_permissions, flags)

    def _finish(
        self,
        connection: sqlite3.Connection,
        *,
        decision: ExternalTransferContractResult,
        case_id: int,
        actor_user_id: int,
        idempotency_key: str,
    ) -> ExternalTransferContractResult:
        stamp = self.clock()
        student_id = str(decision.student_id)
        cycles = connection.execute(
            f"""SELECT * FROM application_cycles
                 WHERE student_id=? AND status='active'{_school_cycle_filter(connection)}
                 ORDER BY id""",
            (student_id,),
        ).fetchall()
        if len(cycles) > 1:
            raise sqlite3.IntegrityError("multiple active school cycles")
        if cycles and str(cycles[0]["cycle_type"]) == "initial":
            updated = connection.execute(
                """UPDATE application_cycles
                      SET cycle_type='transfer',source_student_case_id=?,
                          updated_by_user_id=?,updated_at=?
                    WHERE id=? AND student_id=? AND status='active'
                      AND cycle_type='initial' AND source_student_case_id IS NULL""",
                (
                    int(case_id), int(actor_user_id), stamp,
                    int(cycles[0]["id"]), student_id,
                ),
            )
            if updated.rowcount != 1:
                raise sqlite3.IntegrityError("provisional initial cycle changed")

        current = connection.execute(
            """SELECT * FROM student_school_enrolments
                WHERE id=? AND student_id=? AND enrolment_role='current'
                  AND status='active'""",
            (int(decision.current_enrolment_id), student_id),
        ).fetchone()
        if current is None:
            raise sqlite3.IntegrityError("current enrolment changed")
        connection.execute(
            """UPDATE transfer_cases
                  SET transfer_origin='external',source_contract_id=?,
                      current_school_enrolment_id=?,
                      relationship_status=CASE
                        WHEN upcoming_school_enrolment_id IS NULL
                         AND relationship_status='needs_repair'
                        THEN 'current_ready'
                        ELSE relationship_status
                      END,
                      updated_at=?
                WHERE case_id=?""",
            (
                int(decision.contract_id), int(current["id"]), stamp,
                int(case_id),
            ),
        )
        case_state = connection.execute(
            "SELECT status,active_slot FROM student_cases WHERE id=?",
            (int(case_id),),
        ).fetchone()
        if case_state is None:
            raise sqlite3.IntegrityError("transfer case disappeared")
        if str(case_state["status"]) == "draft" and int(case_state["active_slot"]) == 0:
            lifecycle = activate_lifecycle(
                connection,
                ApplicationCycleGateway(),
                case_id=int(case_id),
                actor_user_id=int(actor_user_id),
                idempotency_key=f"external-transfer-contract:{int(decision.contract_id)}",
                occurred_at=stamp,
            )
        elif (
            str(case_state["status"]) not in _TERMINAL_CASE_STATUSES
            and int(case_state["active_slot"]) == 1
        ):
            lifecycle = repository.load_result(connection, int(case_id))
            if lifecycle.cycle_id is None or not cycles:
                raise sqlite3.IntegrityError("active transfer graph is incomplete")
            bound_cycle = cycles[0]
            if (
                int(bound_cycle["id"]) != int(lifecycle.cycle_id)
                or str(bound_cycle["cycle_type"]) != "transfer"
                or int(bound_cycle["source_student_case_id"] or 0) != int(case_id)
            ):
                raise sqlite3.IntegrityError("active transfer graph changed")
        else:
            raise sqlite3.IntegrityError("transfer case is not activatable")
        contract = connection.execute(
            "SELECT application_cycle_id FROM contracts WHERE id=? AND student_id=?",
            (int(decision.contract_id), student_id),
        ).fetchone()
        if contract is None or (
            contract[0] is not None and int(contract[0]) != int(lifecycle.cycle_id)
        ):
            raise sqlite3.IntegrityError("contract cycle ownership changed")
        contract_cycle = record_application_lifecycle_event(
            connection,
            "CONTRACT_SIGNED",
            occurred_at=stamp,
            actor_user_id=int(actor_user_id),
            student_id=student_id,
            contract_id=int(decision.contract_id),
        )
        if int(contract_cycle.id) != int(lifecycle.cycle_id):
            raise sqlite3.IntegrityError("canonical contract cycle mismatch")

        link = connection.execute(
            """SELECT id,link_role,validity_status FROM case_record_links
                WHERE case_id=? AND record_type='student_document' AND record_id=?""",
            (int(case_id), int(decision.final_document_id)),
        ).fetchone()
        if link is None:
            connection.execute(
                """INSERT INTO case_record_links(
                       case_id,transfer_application_id,record_type,record_id,
                       document_id,link_role,validity_status,effective_from,
                       superseded_by,created_by,created_at)
                     VALUES(?,NULL,'student_document',?,?,'completion_document',
                            'current',?,NULL,?,?)""",
                (
                    int(case_id), int(decision.final_document_id),
                    int(decision.final_document_id), stamp,
                    int(actor_user_id), stamp,
                ),
            )
        elif (
            str(link["link_role"]) != "completion_document"
            or str(link["validity_status"]) != "current"
        ):
            connection.execute(
                """UPDATE case_record_links
                      SET link_role='completion_document',validity_status='current',
                          superseded_by=NULL
                    WHERE id=?""",
                (int(link["id"]),),
            )

        source_tag = connection.execute(
            """SELECT id FROM student_source_tags
                WHERE student_id=? AND source_tag='external_transfer'
                  AND effective_to IS NULL""",
            (student_id,),
        ).fetchone()
        if source_tag is None:
            connection.execute(
                """INSERT INTO student_source_tags(
                       student_id,source_tag,effective_from,source_case_id,
                       created_by,created_at)
                     VALUES(?,'external_transfer',?,?,?,?)""",
                (student_id, stamp[:10], int(case_id), int(actor_user_id), stamp),
            )

        request = connection.execute(
            """SELECT id FROM external_transfer_case_requests
                WHERE student_id=? AND contract_id=?""",
            (student_id, int(decision.contract_id)),
        ).fetchone()
        if request is None:
            connection.execute(
                """INSERT INTO external_transfer_case_requests(
                       student_id,contract_id,idempotency_key,request_status,case_id,
                       attempt_count,created_at,updated_at,completed_at)
                     VALUES(?,?,?,'completed',?,1,?,?,?)""",
                (
                    student_id, int(decision.contract_id), str(idempotency_key),
                    int(case_id), stamp, stamp, stamp,
                ),
            )
        else:
            connection.execute(
                """UPDATE external_transfer_case_requests
                      SET request_status='completed',case_id=?,
                          attempt_count=attempt_count+1,last_error_code=NULL,
                          last_error_message=NULL,next_retry_at=NULL,
                          updated_at=?,completed_at=?
                    WHERE id=?""",
                (int(case_id), stamp, stamp, int(request[0])),
            )

        if connection.execute(
            """SELECT 1 FROM case_events
                WHERE case_id=? AND event_type='external_transfer_contract_orchestrated'
                  AND request_id=?""",
            (int(case_id), f"contract:{int(decision.contract_id)}"),
        ).fetchone() is None:
            connection.execute(
                """INSERT INTO case_events(
                       case_id,event_type,actor_type,actor_user_id,new_value_json,
                       metadata_json,request_id,created_at)
                     VALUES(?,'external_transfer_contract_orchestrated','user',?,?,?,?,?)""",
                (
                    int(case_id), int(actor_user_id),
                    json.dumps({
                        "contract_id": int(decision.contract_id),
                        "cycle_id": int(lifecycle.cycle_id),
                    }),
                    json.dumps({"source": TRANSFER_APPLICATION_CONTRACT}),
                    f"contract:{int(decision.contract_id)}", stamp,
                ),
            )
        return ExternalTransferContractResult(
            status="completed", contract_id=int(decision.contract_id),
            student_id=student_id, case_id=int(case_id),
            cycle_id=int(lifecycle.cycle_id),
            current_enrolment_id=int(current["id"]),
            final_document_id=int(decision.final_document_id),
            message="Transfer Case 已建立。",
        )

    def orchestrate(
        self,
        *,
        contract_id: int,
        actor_user_id: int,
        actor_permissions,
        flags: StudentCaseFeatureFlags,
        idempotency_key: str,
    ) -> ExternalTransferContractResult:
        with _LOCK:
            decision = self.inspect(int(contract_id))
            if decision.status != "ready":
                return decision
            if not self._allowed(actor_permissions, flags):
                return _decision(
                    "permission_denied", contract_id, decision.student_id,
                    "当前权限或 Transfer 写入开关不允许建立 Case。",
                )
            if decision.case_id is not None:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = inspect_transfer_contract(connection, int(contract_id))
                    if current.status != "ready" or current.case_id != decision.case_id:
                        connection.rollback()
                        return current
                    result = self._finish(
                        connection,
                        decision=current,
                        case_id=int(current.case_id),
                        actor_user_id=int(actor_user_id),
                        idempotency_key=str(idempotency_key),
                    )
                    connection.commit()
                    return result

            completed = None

            def before_commit(connection: sqlite3.Connection, case_id: int):
                nonlocal completed
                current = inspect_transfer_contract(connection, int(contract_id))
                if current.status != "ready" or current.case_id != int(case_id):
                    raise sqlite3.IntegrityError(current.status)
                completed = self._finish(
                    connection,
                    decision=current,
                    case_id=int(case_id),
                    actor_user_id=int(actor_user_id),
                    idempotency_key=str(idempotency_key),
                )

            with closing(self._connect()) as connection:
                current = connection.execute(
                    "SELECT * FROM student_school_enrolments WHERE id=?",
                    (int(decision.current_enrolment_id),),
                ).fetchone()
            try:
                TransferCaseService(
                    self.database_path, clock=self.clock
                ).create_internal_transfer(
                    student_id=str(decision.student_id),
                    actor_user_id=int(actor_user_id),
                    actor_permissions=actor_permissions,
                    flags=flags,
                    owner_user_id=int(actor_user_id),
                    title=f"外部转学 · 合同 #{int(contract_id)}",
                    current_school_name_snapshot=str(current["school_name_snapshot"]),
                    current_school_id=current["school_id"],
                    current_city_key=current["city_key"],
                    current_school_sector=current["school_sector"],
                    initial_note="由完成的转学申请合同建立。",
                    before_commit=before_commit,
                )
            except TransferCaseError:
                # Another CRM process may have completed the same canonical
                # request after our initial inspection. Database constraints
                # remain authoritative; only replay a fully verified graph.
                replay = self.inspect(int(contract_id))
                if replay.status == "completed":
                    return replay
                raise
            if completed is None:
                raise sqlite3.IntegrityError("orchestration callback did not complete")
            return completed
