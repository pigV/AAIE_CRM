from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import uuid4

from student_lifecycle.enrolment import (
    SchoolEnrolmentConflictError,
    apply_transition_in_transaction,
    register_upcoming_transition,
)
from student_lifecycle.transfer_v2_cutover import (
    cutover_applied_at,
    is_legacy_transfer,
    is_new_transfer_cycle,
)


class TransferV2ProjectionError(RuntimeError):
    pass


class TransferV2CompletionValidationError(TransferV2ProjectionError):
    pass


@dataclass(frozen=True)
class TransferV2Projection:
    cycle_id: int
    cycle_uid: str
    student_id: str
    case_id: int
    cycle_status: str
    cycle_outcome: str | None
    stage_key: str
    stage_order: int
    stage_label: str
    blocker: str
    next_action: str | None
    current_branch_id: int | None
    current_school_name: str | None
    current_acceptance_id: int | None
    current_offer_event_id: int | None
    payment_id: int | None
    coe_confirmation_id: int | None
    is_legacy: bool
    ready_to_complete: bool


STAGES = {
    "legacy_unconfirmed": (0, "Legacy Transfer · Lifecycle unconfirmed"),
    "materials_preparation": (20, "资料准备"),
    "school_plan": (25, "学校方案"),
    "school_application": (30, "学校申请"),
    "offer_received": (40, "已收到 Offer"),
    "acceptance_required": (45, "等待选择已接受学校"),
    "payment_required": (55, "等待付款确认"),
    "accepted_and_paid": (50, "已接受并付款"),
    "coe_update": (60, "更新 COE"),
    "pending_completion": (90, "待结束"),
    "offer_accepted": (50, "Offer已接受"),
    "paid": (60, "已付款"),
    "waiting_coe": (70, "等待COE"),
    "coe_received": (80, "COE已获得"),
    "application_complete": (90, "待完成确认"),
    "transfer_completed": (100, "已完成"),
    "cancelled": (100, "已取消"),
    "closed": (100, "已关闭"),
}


NEXT_ACTIONS = {
    "materials_preparation": "准备材料并开始学校申请",
    "school_plan": "确认目标学校并递交申请",
    "school_application": "等待并确认有效 Offer",
    "offer_received": "接受具体 Offer Version",
    "acceptance_required": "选择当前接受的 Offer 与学校分支",
    "payment_required": "确认已接受学校分支的付款",
    "offer_accepted": "确认付款",
    "paid": "进入等待 COE",
    "waiting_coe": "上传并确认 COE",
    "coe_received": "确认完成申请",
    "application_complete": "完成转学",
    "accepted_and_paid": "选择当前接受学校",
    "coe_update": "核实并确认 COE",
    "pending_completion": "确认结束转学",
}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _authoritative_payment(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    branch_id: int,
) -> sqlite3.Row | None:
    if not (
        _table_exists(connection, "case_record_links")
        and _table_exists(connection, "payments")
    ):
        return None
    return connection.execute(
        """SELECT payment.id,
                  CASE WHEN length(trim(COALESCE(payment.receipt_file_path,'')))>0
                       THEN 'provided' ELSE 'missing' END AS payment_proof_status
             FROM case_record_links AS link
             JOIN payments AS payment ON payment.id=link.record_id
            WHERE link.case_id=? AND link.transfer_application_id=?
              AND link.record_type='payment'
              AND link.validity_status IN ('current','carried_forward')
            ORDER BY COALESCE(NULLIF(payment.paid_date,''),payment.created_at) DESC,
                     payment.id DESC LIMIT 1""",
        (int(case_id), int(branch_id)),
    ).fetchone()


def _result(
    row: sqlite3.Row,
    *,
    stage_key: str,
    blocker: str,
    branch: sqlite3.Row | None = None,
    acceptance: sqlite3.Row | None = None,
    payment_id: int | None = None,
    coe_id: int | None = None,
    is_legacy: bool = False,
    next_action: str | None = None,
) -> TransferV2Projection:
    order, label = STAGES[stage_key]
    if stage_key == "paid" and payment_id is not None:
        proof = row["payment_proof_status"] if "payment_proof_status" in row.keys() else None
        if proof == "missing":
            label = "已付款 · 付款凭证待补"
    return TransferV2Projection(
        cycle_id=int(row["cycle_id"]),
        cycle_uid=str(row["cycle_uid"]),
        student_id=str(row["student_id"]),
        case_id=int(row["case_id"]),
        cycle_status=str(row["cycle_status"]),
        cycle_outcome=str(row["cycle_outcome"]) if row["cycle_outcome"] else None,
        stage_key=stage_key,
        stage_order=order,
        stage_label=label,
        blocker=blocker,
        next_action=next_action or NEXT_ACTIONS.get(stage_key),
        current_branch_id=(
            int(branch["id"]) if branch is not None else (
                int(acceptance["transfer_branch_id"])
                if acceptance is not None and acceptance["transfer_branch_id"] is not None
                else None
            )
        ),
        current_school_name=(
            str(branch["school_name_snapshot"]) if branch is not None else None
        ),
        current_acceptance_id=int(acceptance["id"]) if acceptance is not None else None,
        current_offer_event_id=(
            int(acceptance["offer_received_event_id"])
            if acceptance is not None else None
        ),
        payment_id=payment_id,
        coe_confirmation_id=coe_id,
        is_legacy=is_legacy,
        ready_to_complete=stage_key in {"application_complete", "pending_completion"},
    )


def _load_new_transfer_projection(
    connection: sqlite3.Connection,
    base: sqlite3.Row,
    cycle_id: int,
) -> TransferV2Projection:
    if base["cycle_status"] != "active":
        outcome = str(base["cycle_outcome"] or "")
        completion = connection.execute(
            """SELECT branch.* FROM transfer_completion_events AS completion
                 JOIN transfer_school_applications AS branch
                   ON branch.id=completion.final_branch_id
                WHERE completion.application_cycle_id=?""",
            (int(cycle_id),),
        ).fetchone()
        acceptance = None
        payment = None
        coe = None
        if outcome == "completed" and completion is not None:
            acceptance = connection.execute(
                """SELECT * FROM cycle_final_acceptances
                    WHERE application_cycle_id=? AND transfer_branch_id=?
                      AND acceptance_status='active'
                    ORDER BY selected_at DESC,id DESC LIMIT 1""",
                (int(cycle_id), int(completion["id"])),
            ).fetchone()
            if acceptance is not None:
                payment = connection.execute(
                    """SELECT payment.id
                         FROM transfer_payment_confirmations AS confirmation
                         JOIN case_record_links AS link
                           ON link.id=confirmation.payment_link_id
                         JOIN payments AS payment ON payment.id=link.record_id
                        WHERE link.case_id=? AND link.transfer_application_id=?
                          AND link.record_type='payment'
                          AND link.validity_status IN ('current','carried_forward')
                        ORDER BY confirmation.confirmed_at DESC,confirmation.id DESC
                        LIMIT 1""",
                    (int(base["case_id"]), int(completion["id"])),
                ).fetchone()
                coe = connection.execute(
                    """SELECT id FROM transfer_coe_confirmations
                        WHERE application_cycle_id=? AND branch_id=?
                          AND final_acceptance_id=? AND status='confirmed'
                        ORDER BY confirmed_at DESC,id DESC LIMIT 1""",
                    (int(cycle_id), int(completion["id"]), int(acceptance["id"])),
                ).fetchone()
        return _result(
            base,
            stage_key="transfer_completed" if outcome == "completed" else "cancelled",
            blocker="",
            branch=completion,
            acceptance=acceptance,
            payment_id=int(payment["id"]) if payment is not None else None,
            coe_id=int(coe["id"]) if coe is not None else None,
        )

    acceptance = connection.execute(
        """SELECT acceptance.*,branch.school_name_snapshot
             FROM cycle_final_acceptances AS acceptance
             JOIN transfer_school_applications AS branch
               ON branch.id=acceptance.transfer_branch_id
            WHERE acceptance.application_cycle_id=?
              AND acceptance.acceptance_status='active'
              AND branch.removed_at IS NULL
            ORDER BY acceptance.selected_at DESC,acceptance.id DESC LIMIT 1""",
        (int(cycle_id),),
    ).fetchone()
    if acceptance is not None:
        branch = connection.execute(
            "SELECT * FROM transfer_school_applications WHERE id=? AND removed_at IS NULL",
            (int(acceptance["transfer_branch_id"]),),
        ).fetchone()
        payment = connection.execute(
            """SELECT payment.id
                 FROM transfer_payment_confirmations AS confirmation
                 JOIN case_record_links AS link ON link.id=confirmation.payment_link_id
                 JOIN payments AS payment ON payment.id=link.record_id
                WHERE link.case_id=? AND link.transfer_application_id=?
                  AND link.record_type='payment'
                  AND link.validity_status IN ('current','carried_forward')
                ORDER BY confirmation.confirmed_at DESC,confirmation.id DESC LIMIT 1""",
            (int(base["case_id"]), int(branch["id"])),
        ).fetchone()
        coe = connection.execute(
            """SELECT id FROM transfer_coe_confirmations
                WHERE application_cycle_id=? AND branch_id=?
                  AND final_acceptance_id=? AND status='confirmed'
                ORDER BY confirmed_at DESC,id DESC LIMIT 1""",
            (int(cycle_id), int(branch["id"]), int(acceptance["id"])),
        ).fetchone()
        application_complete = connection.execute(
            """SELECT 1 FROM transfer_v2_lifecycle_events
                WHERE application_cycle_id=? AND branch_id=?
                  AND final_acceptance_id=?
                  AND event_type='application_complete_confirmed' LIMIT 1""",
            (int(cycle_id), int(branch["id"]), int(acceptance["id"])),
        ).fetchone()
        if payment is None:
            return _result(
                base,stage_key="payment_required",blocker="等待已接受学校分支的付款确认",
                branch=branch,acceptance=acceptance,
            )
        if coe is not None or application_complete is not None:
            return _result(
                base,stage_key="pending_completion",blocker="等待确认结束转学",
                branch=branch,acceptance=acceptance,
                payment_id=int(payment["id"]) if payment else None,
                coe_id=int(coe["id"]) if coe else None,
            )
        return _result(
            base,stage_key="coe_update",blocker="等待更新并确认 COE",
            branch=branch,acceptance=acceptance,
            payment_id=int(payment["id"]) if payment else None,
        )

    eligible_offer = connection.execute(
        """SELECT branch.*
             FROM offer_received_events AS offer
             JOIN offers AS formal_offer ON formal_offer.id=offer.legacy_offer_id
             JOIN transfer_school_applications AS branch
               ON branch.id=offer.transfer_branch_id
            WHERE offer.application_cycle_id=? AND branch.removed_at IS NULL
              AND formal_offer.offer_status IN ('signed','signed_received','accepted')
              AND COALESCE((
                SELECT state.state FROM offer_received_event_state_events AS state
                 WHERE state.offer_received_event_id=offer.id
                 ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1
              ),'UNCONFIRMED')='ACTIVE'
            ORDER BY offer.received_at DESC,offer.id DESC LIMIT 1""",
        (int(cycle_id),),
    ).fetchone()
    if eligible_offer is not None:
        return _result(
            base,stage_key="acceptance_required",
            blocker="已签署 Offer；请选择当前接受学校",
            branch=eligible_offer,
        )

    valid_offer = connection.execute(
        """SELECT branch.* FROM offer_received_events AS offer
             JOIN transfer_school_applications AS branch
               ON branch.id=offer.transfer_branch_id
            WHERE offer.application_cycle_id=? AND branch.removed_at IS NULL
              AND COALESCE((
                SELECT state.state FROM offer_received_event_state_events AS state
                 WHERE state.offer_received_event_id=offer.id
                 ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1
              ),'UNCONFIRMED')='ACTIVE'
            ORDER BY offer.received_at DESC,offer.id DESC LIMIT 1""",
        (int(cycle_id),),
    ).fetchone()
    if valid_offer is not None:
        return _result(
            base,stage_key="offer_received",blocker="等待已签署 Offer 与付款确认",
            branch=valid_offer,next_action="签署 Offer 并选择当前接受学校",
        )

    application = connection.execute(
        """SELECT branch.* FROM transfer_target_state_events AS state
             JOIN transfer_school_applications AS branch ON branch.id=state.branch_id
            WHERE state.application_cycle_id=? AND branch.removed_at IS NULL
              AND state.id=(SELECT max(latest.id)
                FROM transfer_target_state_events AS latest
                WHERE latest.branch_id=state.branch_id)
              AND state.new_state IN (
                'application_submitted','awaiting_interview','awaiting_result','offer'
              )
            ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1""",
        (int(cycle_id),),
    ).fetchone()
    if application is not None:
        return _result(
            base,stage_key="school_application",blocker="等待学校处理结果",
            branch=application,
        )
    has_plan = False
    if _table_exists(connection, "school_plan_reports"):
        has_plan = connection.execute(
            "SELECT 1 FROM school_plan_reports WHERE application_cycle_id=? LIMIT 1",
            (int(cycle_id),),
        ).fetchone() is not None
    return _result(
        base,
        stage_key="school_plan" if has_plan else "materials_preparation",
        blocker="确认目标学校并准备递交" if has_plan else "等待准备材料",
    )


def load_transfer_v2_projection(
    connection: sqlite3.Connection, cycle_id: int
) -> TransferV2Projection:
    connection.row_factory = sqlite3.Row
    base = connection.execute(
        """SELECT cycle.id AS cycle_id,cycle.cycle_uid,cycle.student_id,
                  cycle.status AS cycle_status,cycle.outcome AS cycle_outcome,
                  cycle.source_student_case_id AS case_id,transfer.v2_version
             FROM application_cycles AS cycle
             JOIN transfer_cases AS transfer
               ON transfer.case_id=cycle.source_student_case_id
            WHERE cycle.id=? AND cycle.cycle_type='transfer'""",
        (int(cycle_id),),
    ).fetchone()
    if base is None:
        raise TransferV2ProjectionError("Transfer Cycle not found")
    if is_legacy_transfer(
        connection,
        int(cycle_id),
        v2_version=int(base["v2_version"] or 0),
    ):
        return _result(
            base,
            stage_key="legacy_unconfirmed",
            blocker=(
                "历史 Transfer 只读保留，不执行新逻辑或 fallback"
                if cutover_applied_at(connection) is not None
                else "员工需逐项核实后人工切换至 Transfer V2"
            ),
            is_legacy=True,
        )
    cutover = cutover_applied_at(connection)
    if cutover is not None:
        return _load_new_transfer_projection(connection, base, int(cycle_id))
    if base["cycle_status"] != "active":
        outcome = str(base["cycle_outcome"] or "")
        stage = (
            "transfer_completed" if outcome == "completed"
            else "cancelled" if outcome == "cancelled"
            else "closed"
        )
        completion = connection.execute(
            """SELECT branch.* FROM transfer_completion_events AS completion
                JOIN transfer_school_applications AS branch
                  ON branch.id=completion.final_branch_id
                WHERE completion.application_cycle_id=?""",
            (int(cycle_id),),
        ).fetchone()
        acceptance = None
        payment = None
        coe = None
        if stage == "transfer_completed" and completion is not None:
            acceptance = connection.execute(
                """SELECT acceptance.*,branch.school_name_snapshot
                     FROM cycle_final_acceptances AS acceptance
                     JOIN transfer_school_applications AS branch
                       ON branch.id=acceptance.transfer_branch_id
                    WHERE acceptance.application_cycle_id=?
                      AND acceptance.transfer_branch_id=?
                      AND acceptance.acceptance_status='active'
                    ORDER BY acceptance.selected_at DESC,acceptance.id DESC LIMIT 1""",
                (int(cycle_id), int(completion["id"])),
            ).fetchone()
            if acceptance is not None:
                payment = _authoritative_payment(
                    connection, case_id=int(base["case_id"]),
                    branch_id=int(completion["id"]),
                ) or connection.execute(
                    """SELECT id FROM transfer_payment_aggregates
                        WHERE application_cycle_id=? AND branch_id=?
                          AND final_acceptance_id=? AND paid_status='paid'
                        ORDER BY id DESC LIMIT 1""",
                    (
                        int(cycle_id), int(completion["id"]),
                        int(acceptance["id"]),
                    ),
                ).fetchone()
                coe = connection.execute(
                    """SELECT id FROM transfer_coe_confirmations
                        WHERE application_cycle_id=? AND branch_id=?
                          AND final_acceptance_id=? AND status='confirmed'
                        ORDER BY confirmed_at DESC,id DESC LIMIT 1""",
                    (
                        int(cycle_id), int(completion["id"]),
                        int(acceptance["id"]),
                    ),
                ).fetchone()
        return _result(
            base, stage_key=stage, blocker="", branch=completion,
            acceptance=acceptance,
            payment_id=int(payment["id"]) if payment is not None else None,
            coe_id=int(coe["id"]) if coe is not None else None,
        )

    acceptance = connection.execute(
        """SELECT acceptance.*,branch.school_name_snapshot
             FROM cycle_final_acceptances AS acceptance
             JOIN transfer_school_applications AS branch
               ON branch.id=acceptance.transfer_branch_id
            WHERE acceptance.application_cycle_id=?
              AND acceptance.acceptance_status='active'
            ORDER BY acceptance.selected_at DESC,acceptance.id DESC LIMIT 1""",
        (int(cycle_id),),
    ).fetchone()
    if acceptance is not None:
        branch = connection.execute(
            "SELECT * FROM transfer_school_applications WHERE id=?",
            (int(acceptance["transfer_branch_id"]),),
        ).fetchone()
        application_complete = connection.execute(
            """SELECT 1 FROM transfer_v2_lifecycle_events
                WHERE application_cycle_id=? AND branch_id=?
                  AND final_acceptance_id=?
                  AND event_type='application_complete_confirmed' LIMIT 1""",
            (int(cycle_id), int(branch["id"]), int(acceptance["id"])),
        ).fetchone()
        coe = connection.execute(
            """SELECT id FROM transfer_coe_confirmations
                WHERE application_cycle_id=? AND branch_id=?
                  AND final_acceptance_id=? AND status='confirmed'
                ORDER BY confirmed_at DESC,id DESC LIMIT 1""",
            (int(cycle_id), int(branch["id"]), int(acceptance["id"])),
        ).fetchone()
        payment = _authoritative_payment(
            connection, case_id=int(base["case_id"]), branch_id=int(branch["id"])
        ) or connection.execute(
            """SELECT id,payment_proof_status FROM transfer_payment_aggregates
                WHERE application_cycle_id=? AND branch_id=?
                  AND final_acceptance_id=? AND accepted_offer_event_id=?
                  AND paid_status='paid' LIMIT 1""",
            (
                int(cycle_id),int(branch["id"]),int(acceptance["id"]),
                int(acceptance["offer_received_event_id"]),
            ),
        ).fetchone()
        waiting = connection.execute(
            """SELECT 1 FROM transfer_v2_lifecycle_events
                WHERE application_cycle_id=? AND branch_id=?
                  AND final_acceptance_id=? AND event_type='waiting_coe_started'
                LIMIT 1""",
            (int(cycle_id), int(branch["id"]), int(acceptance["id"])),
        ).fetchone()
        if application_complete is not None:
            return _result(
                base, stage_key="application_complete", blocker="等待员工完成转学",
                branch=branch, acceptance=acceptance,
                payment_id=int(payment["id"]) if payment else None,
                coe_id=int(coe["id"]) if coe else None,
            )
        if coe is not None:
            return _result(
                base, stage_key="coe_received", blocker="等待确认完成申请",
                branch=branch, acceptance=acceptance,
                payment_id=int(payment["id"]) if payment else None,
                coe_id=int(coe["id"]),
            )
        if waiting is not None:
            return _result(
                base, stage_key="waiting_coe", blocker="等待当前 Accepted School 的 COE",
                branch=branch, acceptance=acceptance,
                payment_id=int(payment["id"]) if payment else None,
            )
        if payment is not None:
            payment_base = dict(base)
            payment_base["payment_proof_status"] = payment["payment_proof_status"]
            synthetic = _MappingRow(payment_base)
            return _result(
                synthetic, stage_key="paid", blocker="付款已确认；尚未进入等待 COE",
                branch=branch, acceptance=acceptance, payment_id=int(payment["id"]),
            )
        return _result(
            base, stage_key="offer_accepted", blocker="等待确认付款",
            branch=branch, acceptance=acceptance,
        )

    valid_offer = connection.execute(
        """SELECT branch.* FROM offer_received_events AS offer
             JOIN transfer_school_applications AS branch
               ON branch.id=offer.transfer_branch_id
            WHERE offer.application_cycle_id=?
              AND COALESCE((
                SELECT state.state FROM offer_received_event_state_events AS state
                 WHERE state.offer_received_event_id=offer.id
                 ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1
              ),'UNCONFIRMED')='ACTIVE'
            ORDER BY offer.received_at DESC,offer.id DESC LIMIT 1""",
        (int(cycle_id),),
    ).fetchone()
    if valid_offer is not None:
        return _result(
            base, stage_key="offer_received", blocker="等待接受具体 Offer Version",
            branch=valid_offer,
        )
    application = connection.execute(
        """SELECT branch.* FROM transfer_target_state_events AS state
             JOIN transfer_school_applications AS branch ON branch.id=state.branch_id
            WHERE state.application_cycle_id=?
              AND state.id IN (
                SELECT max(id) FROM transfer_target_state_events
                 WHERE application_cycle_id=? GROUP BY branch_id
              )
              AND state.new_state IN (
                'application_in_progress','offer','accepted','paid','coe','completed'
              )
            ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1""",
        (int(cycle_id), int(cycle_id)),
    ).fetchone()
    if application is not None:
        return _result(
            base, stage_key="school_application", blocker="等待员工确认有效 Offer",
            branch=application,
        )
    return _result(
        base, stage_key="materials_preparation", blocker="等待准备材料并开始学校申请",
    )


class _MappingRow(dict):
    def keys(self):
        return super().keys()


def _default_uid(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


class TransferV2CompletionService:
    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Callable[[], str],
        uid_factory: Callable[[str], str] = _default_uid,
    ) -> None:
        self.database_path = Path(database_path)
        self.clock = clock
        self.uid_factory = uid_factory

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _required(value: object, label: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise TransferV2CompletionValidationError(f"{label} is required")
        return normalized

    @staticmethod
    def _actor(connection: sqlite3.Connection, actor_user_id: int) -> None:
        if connection.execute(
            "SELECT 1 FROM users WHERE id=?", (int(actor_user_id),)
        ).fetchone() is None:
            raise TransferV2CompletionValidationError("actor not found")

    def _uid(self, command: str, key: str) -> str:
        return self.uid_factory(f"transfer-v2-{command}-{key}")

    @staticmethod
    def _payload(payload: dict[str, object]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _receipt(
        connection: sqlite3.Connection, event_uid: str, command: str, payload: str
    ) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT * FROM transfer_v2_command_receipts WHERE event_uid=?", (event_uid,)
        ).fetchone()
        if row is not None and (
            row["command_type"] != command or row["request_json"] != payload
        ):
            raise TransferV2CompletionValidationError(
                "idempotency key conflicts with an earlier command"
            )
        return row

    @staticmethod
    def _record_receipt(
        connection: sqlite3.Connection,
        *,
        event_uid: str,
        cycle_id: int,
        command: str,
        aggregate_type: str,
        aggregate_id: int,
        payload: str,
        actor_user_id: int,
        timestamp: str,
    ) -> None:
        connection.execute(
            """INSERT INTO transfer_v2_command_receipts(
                 event_uid,application_cycle_id,command_type,aggregate_type,
                 aggregate_id,request_json,actor_user_id,occurred_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                event_uid,int(cycle_id),command,aggregate_type,int(aggregate_id),
                payload,int(actor_user_id),timestamp,timestamp,
            ),
        )

    @staticmethod
    def _active_acceptance(
        connection: sqlite3.Connection, cycle_id: int, branch_id: int, acceptance_id: int
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT acceptance.*,cycle.source_student_case_id AS case_id,
                      branch.school_id AS branch_school_id,
                      branch.school_name_snapshot AS branch_school_name_snapshot,
                      branch.school_sector,
                      transfer.application_year_level,transfer.transfer_year,
                      transfer.transfer_term
                 FROM cycle_final_acceptances AS acceptance
                 JOIN application_cycles AS cycle ON cycle.id=acceptance.application_cycle_id
                 JOIN transfer_school_applications AS branch
                   ON branch.id=acceptance.transfer_branch_id
                 JOIN transfer_cases AS transfer ON transfer.case_id=cycle.source_student_case_id
                WHERE acceptance.id=? AND acceptance.application_cycle_id=?
                  AND acceptance.transfer_branch_id=?
                  AND acceptance.acceptance_status='active'
                  AND cycle.status='active' AND transfer.v2_version=2""",
            (int(acceptance_id), int(cycle_id), int(branch_id)),
        ).fetchone()
        if row is None:
            raise TransferV2CompletionValidationError("current acceptance not found")
        if cutover_applied_at(connection) is not None and not is_new_transfer_cycle(
            connection, int(cycle_id)
        ):
            raise TransferV2CompletionValidationError("Legacy Transfer is read only")
        return row

    def confirm_application_complete(
        self,
        *,
        cycle_id: int,
        branch_id: int,
        acceptance_id: int,
        actor_user_id: int,
        idempotency_key: str,
    ) -> TransferV2Projection:
        key = self._required(idempotency_key, "idempotency key")
        event_uid = self._uid("application-complete", key)
        timestamp = self.clock()
        payload = self._payload({
            "cycle_id": int(cycle_id),"branch_id": int(branch_id),
            "acceptance_id": int(acceptance_id),
        })
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._actor(connection, actor_user_id)
            receipt = self._receipt(connection, event_uid, "confirm_application_complete", payload)
            if receipt is not None:
                connection.commit()
                return load_transfer_v2_projection(connection, cycle_id)
            self._active_acceptance(connection, cycle_id, branch_id, acceptance_id)
            if is_new_transfer_cycle(connection, int(cycle_id)):
                raise TransferV2CompletionValidationError(
                    "新 Transfer 在 COE 确认后直接进入待结束"
                )
            if connection.execute(
                """SELECT 1 FROM transfer_coe_confirmations
                    WHERE application_cycle_id=? AND branch_id=?
                      AND final_acceptance_id=? AND status='confirmed'""",
                (int(cycle_id),int(branch_id),int(acceptance_id)),
            ).fetchone() is None:
                raise TransferV2CompletionValidationError(
                    "confirmed COE is required before application completion"
                )
            lifecycle_id = int(connection.execute(
                """INSERT INTO transfer_v2_lifecycle_events(
                     event_uid,application_cycle_id,branch_id,final_acceptance_id,
                     event_type,note,actor_user_id,occurred_at,created_at)
                   VALUES(?,?,?,?,'application_complete_confirmed',NULL,?,?,?)""",
                (
                    f"{event_uid}:lifecycle",int(cycle_id),int(branch_id),
                    int(acceptance_id),int(actor_user_id),timestamp,timestamp,
                ),
            ).lastrowid)
            self._record_receipt(
                connection,event_uid=event_uid,cycle_id=cycle_id,
                command="confirm_application_complete",aggregate_type="lifecycle_event",
                aggregate_id=lifecycle_id,payload=payload,actor_user_id=actor_user_id,
                timestamp=timestamp,
            )
            connection.commit()
            return load_transfer_v2_projection(connection, cycle_id)

    def complete_transfer(
        self,
        *,
        cycle_id: int,
        acceptance_id: int,
        update_current_school: bool,
        update_current_year_level: bool,
        close_unfinished_branches: bool,
        actor_user_id: int,
        idempotency_key: str,
    ) -> TransferV2Projection:
        key = self._required(idempotency_key, "idempotency key")
        event_uid = self._uid("complete", key)
        timestamp = self.clock()
        payload = self._payload({
            "cycle_id": int(cycle_id),"acceptance_id": int(acceptance_id),
            "update_current_school": bool(update_current_school),
            "update_current_year_level": bool(update_current_year_level),
            "close_unfinished_branches": bool(close_unfinished_branches),
        })
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._actor(connection, actor_user_id)
            receipt = self._receipt(connection, event_uid, "complete_transfer", payload)
            if receipt is not None:
                connection.commit()
                return load_transfer_v2_projection(connection, cycle_id)
            active = connection.execute(
                """SELECT transfer_branch_id FROM cycle_final_acceptances
                    WHERE id=? AND application_cycle_id=? AND acceptance_status='active'""",
                (int(acceptance_id),int(cycle_id)),
            ).fetchone()
            if active is None or active[0] is None:
                raise TransferV2CompletionValidationError("current acceptance not found")
            branch_id = int(active[0])
            acceptance = self._active_acceptance(
                connection, cycle_id, branch_id, acceptance_id
            )
            new_cycle = is_new_transfer_cycle(connection, int(cycle_id))
            if new_cycle:
                if not update_current_school:
                    raise TransferV2CompletionValidationError(
                        "enrolment transition is required before Transfer completion"
                    )
                if connection.execute(
                    """SELECT 1
                         FROM transfer_payment_confirmations AS confirmation
                         JOIN case_record_links AS link
                           ON link.id=confirmation.payment_link_id
                        WHERE link.case_id=? AND link.transfer_application_id=?
                          AND link.record_type='payment'
                          AND link.validity_status='current'""",
                    (int(acceptance["case_id"]), branch_id),
                ).fetchone() is None:
                    raise TransferV2CompletionValidationError(
                        "confirmed Payment is required before Transfer completion"
                    )
                if connection.execute(
                    """SELECT 1 FROM transfer_coe_confirmations
                        WHERE application_cycle_id=? AND branch_id=?
                          AND final_acceptance_id=? AND status='confirmed'""",
                    (int(cycle_id),branch_id,int(acceptance_id)),
                ).fetchone() is None:
                    raise TransferV2CompletionValidationError(
                        "confirmed COE is required before Transfer completion"
                    )
            elif connection.execute(
                """SELECT 1 FROM transfer_v2_lifecycle_events
                    WHERE application_cycle_id=? AND branch_id=?
                      AND final_acceptance_id=?
                      AND event_type='application_complete_confirmed'""",
                (int(cycle_id),branch_id,int(acceptance_id)),
            ).fetchone() is None:
                raise TransferV2CompletionValidationError(
                    "application completion confirmation is required"
                )
            removal_filter = (
                " AND branch.removed_at IS NULL"
                if new_cycle else ""
            )
            latest_states = connection.execute(
                f"""SELECT branch.id,branch.school_name_snapshot,state.new_state
                     FROM transfer_school_applications AS branch
                     JOIN transfer_target_state_events AS state
                       ON state.id=(SELECT max(inner_state.id)
                         FROM transfer_target_state_events AS inner_state
                         WHERE inner_state.branch_id=branch.id)
                    WHERE branch.application_cycle_id=?{removal_filter}
                    ORDER BY branch.id""",
                (int(cycle_id),),
            ).fetchall()
            unfinished = [
                row for row in latest_states
                if int(row["id"]) != branch_id
                and row["new_state"] not in {
                    "rejected","withdrawn","closed_after_transfer_completion"
                }
            ]
            if unfinished and not close_unfinished_branches:
                raise TransferV2CompletionValidationError(
                    "unfinished branches require explicit closure confirmation"
                )
            for row in latest_states:
                target_id = int(row["id"])
                if new_cycle:
                    new_state = (
                        "closed_after_transfer_completion"
                        if target_id == branch_id or row in unfinished else None
                    )
                else:
                    new_state = (
                        "completed" if target_id == branch_id
                        else "closed_after_transfer_completion"
                        if row in unfinished else None
                    )
                if new_state is None or row["new_state"] == new_state:
                    continue
                connection.execute(
                    """INSERT INTO transfer_target_state_events(
                         event_uid,application_cycle_id,branch_id,previous_state,new_state,
                         event_type,note,actor_user_id,occurred_at,created_at)
                       VALUES(?,?,?,?,?,'transfer_completed',NULL,?,?,?)""",
                    (
                        f"{event_uid}:branch:{target_id}",int(cycle_id),target_id,
                        row["new_state"],new_state,int(actor_user_id),timestamp,timestamp,
                    ),
                )
            if update_current_school:
                self._update_current_school(
                    connection, acceptance=acceptance, actor_user_id=actor_user_id,
                    timestamp=timestamp,
                )
            if update_current_year_level:
                connection.execute(
                    """UPDATE students SET current_year_level=?,updated_at=?
                        WHERE student_id=?""",
                    (
                        acceptance["application_year_level"],timestamp,
                        acceptance["student_id"],
                    ),
                )
            completion_id = int(connection.execute(
                """INSERT INTO transfer_completion_events(
                     event_uid,application_cycle_id,final_branch_id,final_school_id,
                     final_school_name_snapshot,application_year_level,transfer_year,
                     transfer_term,coe_status,update_current_school,
                     update_current_year_level,completed_by,completed_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?, 'confirmed',?,?,?,?,?)""",
                (
                    f"{event_uid}:completion",int(cycle_id),branch_id,
                    acceptance["branch_school_id"],acceptance["branch_school_name_snapshot"],
                    acceptance["application_year_level"],int(acceptance["transfer_year"]),
                    acceptance["transfer_term"],1 if update_current_school else 0,
                    1 if update_current_year_level else 0,int(actor_user_id),timestamp,timestamp,
                ),
            ).lastrowid)
            connection.execute(
                """UPDATE application_cycles
                   SET status='closed',outcome='completed',closed_at=?,
                       updated_by_user_id=?,updated_at=? WHERE id=? AND status='active'""",
                (timestamp,int(actor_user_id),timestamp,int(cycle_id)),
            )
            connection.execute(
                """UPDATE student_cases
                   SET status='completed',terminal_result='completed',active_slot=0,
                       completed_at=?,updated_by=?,updated_at=?
                   WHERE id=? AND active_slot=1""",
                (timestamp,int(actor_user_id),timestamp,int(acceptance["case_id"])),
            )
            connection.execute(
                "UPDATE transfer_cases SET workflow_state='completed',updated_at=? WHERE case_id=?",
                (timestamp,int(acceptance["case_id"])),
            )
            self._record_receipt(
                connection,event_uid=event_uid,cycle_id=cycle_id,
                command="complete_transfer",aggregate_type="completion_event",
                aggregate_id=completion_id,payload=payload,actor_user_id=actor_user_id,
                timestamp=timestamp,
            )
            connection.commit()
            return load_transfer_v2_projection(connection, cycle_id)

    @staticmethod
    def _update_current_school(
        connection: sqlite3.Connection,
        *,
        acceptance: sqlite3.Row,
        actor_user_id: int,
        timestamp: str,
    ) -> None:
        student_id = str(acceptance["student_id"])
        effective_date = timestamp[:10]
        enrolment_sector = {
            "public": "government",
            "private": "independent",
        }.get(str(acceptance["school_sector"] or ""), acceptance["school_sector"])
        current = connection.execute(
            """SELECT * FROM student_school_enrolments
                WHERE student_id=? AND enrolment_role='current'""",
            (student_id,),
        ).fetchone()
        if current is None:
            enrolment_id = int(connection.execute(
                """INSERT INTO student_school_enrolments(
                     student_id,school_id,school_name_snapshot,school_sector,
                     enrolment_role,start_date,source_case_id,status,created_by,
                     created_at,updated_by,updated_at,application_cycle_id,
                     year_level_snapshot,lifecycle_context,confirmation_reason,
                     confirmed_by,confirmed_at)
                   VALUES(?,?,?,?, 'current',?,?,'active',?,?,?,?,?,?,
                          'transfer','Transfer V2 completion',?,?)""",
                (
                    student_id,acceptance["branch_school_id"],
                    acceptance["branch_school_name_snapshot"],
                    enrolment_sector,effective_date,int(acceptance["case_id"]),
                    int(actor_user_id),timestamp,int(actor_user_id),timestamp,
                    int(acceptance["application_cycle_id"]),
                    acceptance["application_year_level"],int(actor_user_id),timestamp,
                ),
            ).lastrowid)
            connection.execute(
                """INSERT INTO school_enrolment_events(
                     event_uid,student_id,source_case_id,application_cycle_id,
                     enrolment_id,event_type,new_value_json,reason,actor_user_id,occurred_at)
                   VALUES(?,?,?,?,?,'current_confirmed',?,'Transfer V2 completion',?,?)""",
                (
                    f"transfer-v2-current:{acceptance['application_cycle_id']}",student_id,
                    int(acceptance["case_id"]),int(acceptance["application_cycle_id"]),
                    enrolment_id,json.dumps({"role":"current"}),int(actor_user_id),timestamp,
                ),
            )
            connection.execute(
                """UPDATE transfer_cases SET current_school_enrolment_id=?,
                     upcoming_school_enrolment_id=NULL,relationship_status='transitioned',
                     updated_at=? WHERE case_id=?""",
                (enrolment_id,timestamp,int(acceptance["case_id"])),
            )
            return
        connection.execute(
            """UPDATE transfer_cases SET current_school_enrolment_id=?,
                 upcoming_school_enrolment_id=NULL,relationship_status='current_ready',
                 updated_at=? WHERE case_id=?
                 AND current_school_enrolment_id IS NULL
                 AND upcoming_school_enrolment_id IS NULL
                 AND relationship_status='needs_repair'""",
            (int(current["id"]),timestamp,int(acceptance["case_id"])),
        )
        upcoming = connection.execute(
            """SELECT * FROM student_school_enrolments
                WHERE student_id=? AND enrolment_role='upcoming' AND school_id IS ?
                ORDER BY id DESC LIMIT 1""",
            (student_id,acceptance["branch_school_id"]),
        ).fetchone()
        if upcoming is None:
            conflicting = connection.execute(
                """SELECT 1 FROM student_school_enrolments
                    WHERE student_id=? AND enrolment_role='upcoming' AND status='active'""",
                (student_id,),
            ).fetchone()
            if conflicting is not None:
                raise TransferV2CompletionValidationError(
                    "current upcoming enrolment does not match the final school"
                )
            upcoming_id = int(connection.execute(
                """INSERT INTO student_school_enrolments(
                     student_id,school_id,school_name_snapshot,school_sector,
                     enrolment_role,start_date,source_case_id,status,created_by,
                     created_at,updated_by,updated_at,application_cycle_id,
                     year_level_snapshot,lifecycle_context,confirmation_reason,
                     confirmed_by,confirmed_at)
                   VALUES(?,?,?,?, 'upcoming',?,?,'active',?,?,?,?,?,?,
                          'transfer','Transfer V2 completion',?,?)""",
                (
                    student_id,acceptance["branch_school_id"],
                    acceptance["branch_school_name_snapshot"],
                    enrolment_sector,effective_date,int(acceptance["case_id"]),
                    int(actor_user_id),timestamp,int(actor_user_id),timestamp,
                    int(acceptance["application_cycle_id"]),
                    acceptance["application_year_level"],int(actor_user_id),timestamp,
                ),
            ).lastrowid)
        else:
            upcoming_id = int(upcoming["id"])
        try:
            transition = register_upcoming_transition(
                connection,
                student_id=student_id,
                current_enrolment_id=int(current["id"]),
                upcoming_enrolment_id=upcoming_id,
                effective_date=effective_date,
                reason="Transfer V2 completion",
                actor_user_id=int(actor_user_id),
                occurred_at=timestamp,
                source_case_id=int(acceptance["case_id"]),
                application_cycle_id=int(acceptance["application_cycle_id"]),
            )
            projected = connection.execute(
                """UPDATE transfer_cases SET upcoming_school_enrolment_id=?,
                     relationship_status='ready',updated_at=?
                   WHERE case_id=? AND current_school_enrolment_id=?
                     AND upcoming_school_enrolment_id IS NULL
                     AND relationship_status='current_ready'""",
                (
                    upcoming_id,timestamp,int(acceptance["case_id"]),int(current["id"]),
                ),
            )
            if projected.rowcount != 1:
                raise TransferV2CompletionValidationError(
                    "Transfer school relationship changed during completion"
                )
            apply_transition_in_transaction(
                connection,
                transition_intent_id=transition.transition_intent_id,
                actor_user_id=int(actor_user_id),
                occurred_at=timestamp,
                as_of_date=effective_date,
            )
        except SchoolEnrolmentConflictError as error:
            raise TransferV2CompletionValidationError(str(error)) from None
